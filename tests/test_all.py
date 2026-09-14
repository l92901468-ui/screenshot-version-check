import os, sys, time, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db, recognize, billing
import state_machine as sm
from cache import TTLCache


class Base(unittest.TestCase):
    def setUp(self):
        # 每个用例用独立临时库，互不干扰（连接池也要跟着重建，否则连的还是上一个库）
        db.reset_pool(os.path.join(tempfile.mkdtemp(), "test.db"))
        db.init_db()

    def new_task(self):
        return db.insert_task(1, "k-%s" % time.time(),
                              {"key": "k.png", "path": "/tmp/k.png", "hash": "h"},
                              status="pending")


class TestVersion(Base):
    def test_meets_version(self):
        self.assertTrue(recognize.meets_version("10.0.19045", "10.0.19045"))   # 相等算通过
        self.assertTrue(recognize.meets_version("10.0.19046", "10.0.19045"))   # 更高
        self.assertFalse(recognize.meets_version("10.0.19041", "10.0.19045"))  # 更低
        self.assertFalse(recognize.meets_version("9.9.99999", "10.0.19045"))   # 主版本更低

    def test_parse_version(self):
        self.assertEqual(recognize.parse_version("10.0.19045"), (10, 0, 19045))


class TestStateMachine(Base):
    def test_legal_transition(self):
        sid = self.new_task()
        self.assertTrue(sm.transition(sid, "processing"))
        self.assertEqual(db.get_task(sid)["status"], "processing")
        self.assertTrue(sm.transition(sid, "done", result_msg="ok"))
        self.assertEqual(db.get_task(sid)["status"], "done")

    def test_illegal_transition_rejected(self):
        sid = self.new_task()
        self.assertFalse(sm.can_transition("pending", "done"))   # 不允许跳步
        self.assertFalse(sm.transition(sid, "done"))
        self.assertEqual(db.get_task(sid)["status"], "pending")

    def test_terminal_cannot_reprocess(self):
        sid = self.new_task()
        sm.transition(sid, "processing")
        sm.transition(sid, "dlq", retry_count=3, result_msg="失败")
        self.assertFalse(sm.transition(sid, "processing"))       # DLQ 不能再被领取

    def test_arrears_recover(self):
        sid = self.new_task()
        sm.transition(sid, "processing")
        self.assertTrue(sm.transition(sid, "arrears", result_msg="欠费"))
        self.assertTrue(sm.transition(sid, "pending", result_msg="充值"))  # 充值后回队列


class TestCache(unittest.TestCase):
    def test_expire_auto_delete(self):
        c = TTLCache(hard_ttl=0.5, soft_ttl=0.2)
        c.set("a", {"v": 1})
        self.assertEqual(c.size(), 1)
        time.sleep(0.6)
        val, need = c.get("a")
        self.assertIsNone(val)          # 已过期，被自动删除
        self.assertEqual(c.size(), 0)

    def test_soft_ttl_needs_verify(self):
        c = TTLCache(hard_ttl=5.0, soft_ttl=0.3)
        c.set("a", {"v": 1})
        time.sleep(0.4)
        val, need = c.get("a")
        self.assertIsNotNone(val)       # 未硬过期，仍命中
        self.assertTrue(need)           # 但需回源与 DB 核对（冲突以 DB 为准）


class TestBilling(Base):
    def test_charge_and_arrears(self):
        self.assertFalse(billing.is_arrears(1))
        after = billing.charge(1, 1.0)
        self.assertAlmostEqual(after, 99.0)
        con = db.connect()
        con.execute("UPDATE accounts SET balance=0 WHERE user_id=1")
        con.commit()
        con.close()
        self.assertTrue(billing.is_arrears(1))


class TestDb(Base):
    def test_optimistic_lock(self):
        sid = self.new_task()
        self.assertTrue(db.update_task(sid, "pending", {"status": "processing"}))
        self.assertFalse(db.update_task(sid, "pending", {"status": "processing"}))  # 状态已变，写入应失败

    def test_find_pending(self):
        sid = self.new_task()
        self.assertEqual(db.find_pending_id(), sid)

    def test_find_pending_respects_next_attempt_at(self):
        sid = self.new_task()
        db.update_task(sid, "pending", {"next_attempt_at": time.time() + 60})
        self.assertIsNone(db.find_pending_id(), "未到 next_attempt_at 的 retry 不应被 worker 提前领取")
        db.update_task(sid, "pending", {"next_attempt_at": time.time() - 1})
        self.assertEqual(db.find_pending_id(), sid)


class TestRetryJitter(unittest.TestCase):
    def test_full_jitter_within_exponential_cap(self):
        import worker
        old_base, old_max = worker.RETRY_BASE_SEC, worker.RETRY_MAX_SEC
        try:
            worker.RETRY_BASE_SEC = 1.0
            worker.RETRY_MAX_SEC = 30.0
            for retry_count, cap in ((1, 1.0), (2, 2.0), (3, 4.0), (10, 30.0)):
                samples = [worker.retry_delay(retry_count) for _ in range(50)]
                self.assertTrue(all(0.0 <= x <= cap for x in samples))
        finally:
            worker.RETRY_BASE_SEC, worker.RETRY_MAX_SEC = old_base, old_max


class TestConnectionPool(Base):
    """连接池：复用、上限、超时、切换。"""

    def test_connection_reused(self):
        before = db.pool_stats()["borrows"]      # setUp 里的 init_db 已经借过一次
        db.get_task(1)
        db.get_task(1)
        st = db.pool_stats()
        self.assertEqual(st["borrows"], before + 2)
        self.assertLessEqual(st["created"], 1, "两次查询应该复用同一个连接，而不是各建一条")

    def test_pool_limit_and_timeout(self):
        path = os.path.join(tempfile.mkdtemp(), "pool.db")
        pool = db.ConnectionPool(path, size=1, timeout=0.3)
        with pool.connection():
            with self.assertRaises(RuntimeError):
                with pool.connection():
                    pass
        # 归还之后可以再借
        with pool.connection():
            pass
        self.assertGreaterEqual(pool.stats()["waits"], 1)

    def test_reset_pool_switch_db(self):
        a = os.path.join(tempfile.mkdtemp(), "a.db")
        b = os.path.join(tempfile.mkdtemp(), "b.db")
        db.reset_pool(a)
        db.init_db()
        sid = db.insert_task(1, "k1", {"key": "k", "path": "/tmp/k", "hash": "h"})
        self.assertIsNotNone(db.get_task(sid))
        db.reset_pool(b)
        db.init_db()
        self.assertIsNone(db.get_task(sid), "切换库后不应再看到上一个库的数据")


class TestStateless(Base):
    """无状态：关掉本地缓存后行为一致；中间态不进缓存。"""

    def setUp(self):
        super().setUp()
        import app
        self.app = app
        app.CACHE.clear()

    def test_cache_disabled_hits_db(self):
        self.app.CACHE_ENABLED = False
        sid = self.new_task()
        task, src = self.app.read_task(sid)
        self.assertEqual(src, "db(无缓存模式)")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(self.app.CACHE.size(), 0, "无缓存模式不该往本地缓存写东西")

    def test_only_terminal_state_cached(self):
        self.app.CACHE_ENABLED = True
        sid = self.new_task()
        _, src = self.app.read_task(sid)
        self.assertEqual(self.app.CACHE.size(), 0, "pending 是中间态，不该进缓存")
        sm.transition(sid, "processing")
        sm.transition(sid, "done", detected_version="10.0.19045")
        _, src2 = self.app.read_task(sid)
        self.assertEqual(self.app.CACHE.size(), 1, "done 是终态，应该进缓存")

    def test_cache_conflict_falls_back_to_db(self):
        self.app.CACHE_ENABLED = True
        sid = self.new_task()
        sm.transition(sid, "processing")
        sm.transition(sid, "done", detected_version="10.0.19045")
        task, _ = self.app.read_task(sid)
        # 手动塞一份"落后于 DB"的缓存，并把它写成 5 秒前（越过 soft_ttl 触发回源核对）
        stale = dict(task, status="pending", updated_at="2000-01-01 00:00:00")
        with self.app.CACHE._lock:
            self.app.CACHE._data[sid] = (stale, time.time() - 5)
        db.update_task(sid, "done", {"status": "dlq"})
        fresh, src = self.app.read_task(sid)
        self.assertEqual(fresh["status"], "dlq", "缓存与 DB 冲突时必须以 DB 为准")


if __name__ == "__main__":
    unittest.main(verbosity=2)
