import os, sys, time, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db, recognize, billing
import state_machine as sm
from cache import TTLCache


class Base(unittest.TestCase):
    def setUp(self):
        db.reset_pool(os.path.join(tempfile.mkdtemp(), "test.db"))
        db.init_db()

    def new_task(self):
        return db.insert_task(1, "k-%s" % time.time(),
                              {"key": "k.png", "path": "/tmp/k.png", "hash": "h"},
                              status="pending")

    def claim(self, owner="test-worker", lease=10.0):
        row = db.claim_next_task(owner, lease)
        self.assertIsNotNone(row)
        return row


class TestVersion(Base):
    def test_meets_version(self):
        self.assertTrue(recognize.meets_version("10.0.19045", "10.0.19045"))
        self.assertTrue(recognize.meets_version("10.0.19046", "10.0.19045"))
        self.assertFalse(recognize.meets_version("10.0.19041", "10.0.19045"))
        self.assertFalse(recognize.meets_version("9.9.99999", "10.0.19045"))

    def test_parse_version(self):
        self.assertEqual(recognize.parse_version("10.0.19045"), (10, 0, 19045))


class TestStateMachine(Base):
    def test_legal_processing_finalize_is_fenced_and_billed(self):
        sid = self.new_task()
        row = self.claim()
        self.assertEqual(row["id"], sid)
        self.assertEqual(row["status"], "processing")
        ok, balance = sm.finalize_done_owned(
            sid, "test-worker", row["processing_generation"], 1.0,
            "10.0.19045", "10.0.19045", "ok",
        )
        self.assertTrue(ok)
        self.assertAlmostEqual(balance, 99.0)
        self.assertEqual(db.get_task(sid)["status"], "done")

    def test_plain_transition_cannot_claim_processing(self):
        sid = self.new_task()
        self.assertFalse(sm.transition(sid, "processing"))
        self.assertEqual(db.get_task(sid)["status"], "pending")

    def test_illegal_transition_rejected(self):
        sid = self.new_task()
        self.assertFalse(sm.can_transition("pending", "done"))
        self.assertFalse(sm.transition(sid, "done"))
        self.assertEqual(db.get_task(sid)["status"], "pending")

    def test_terminal_cannot_reprocess(self):
        sid = self.new_task()
        row = self.claim()
        self.assertTrue(sm.transition_owned(
            sid, "dlq", "test-worker", row["processing_generation"], retry_count=3, result_msg="失败"
        ))
        self.assertIsNone(db.claim_next_task("other-worker", 10.0))
        self.assertFalse(sm.transition(sid, "processing"))

    def test_arrears_recover(self):
        sid = self.new_task()
        row = self.claim()
        self.assertTrue(sm.transition_owned(
            sid, "arrears", "test-worker", row["processing_generation"], result_msg="欠费"
        ))
        self.assertTrue(sm.transition(sid, "pending", result_msg="充值"))


class TestCache(unittest.TestCase):
    def test_expire_auto_delete(self):
        c = TTLCache(hard_ttl=0.5, soft_ttl=0.2)
        c.set("a", {"v": 1})
        self.assertEqual(c.size(), 1)
        time.sleep(0.6)
        val, need = c.get("a")
        self.assertIsNone(val)
        self.assertEqual(c.size(), 0)

    def test_soft_ttl_needs_verify(self):
        c = TTLCache(hard_ttl=5.0, soft_ttl=0.3)
        c.set("a", {"v": 1})
        time.sleep(0.4)
        val, need = c.get("a")
        self.assertIsNotNone(val)
        self.assertTrue(need)


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
        self.assertTrue(db.update_task(sid, "pending", {"result_msg": "first"}))
        self.assertFalse(db.update_task(sid, "done", {"result_msg": "second"}))

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
    def test_connection_reused(self):
        before = db.pool_stats()["borrows"]
        db.get_task(1)
        db.get_task(1)
        st = db.pool_stats()
        self.assertEqual(st["borrows"], before + 2)
        self.assertLessEqual(st["created"], 1)

    def test_pool_limit_and_timeout(self):
        path = os.path.join(tempfile.mkdtemp(), "pool.db")
        pool = db.ConnectionPool(path, size=1, timeout=0.3)
        with pool.connection():
            with self.assertRaises(RuntimeError):
                with pool.connection():
                    pass
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
        self.assertIsNone(db.get_task(sid))


class TestStateless(Base):
    def setUp(self):
        super().setUp()
        import app
        self.app = app
        app.CACHE.clear()

    def _finish_task(self, sid, owner="cache-worker"):
        row = db.claim_next_task(owner, 10.0)
        self.assertEqual(row["id"], sid)
        ok, _ = sm.finalize_done_owned(
            sid, owner, row["processing_generation"], 1.0,
            "10.0.19045", "10.0.19045", "done",
        )
        self.assertTrue(ok)

    def test_cache_disabled_hits_db(self):
        self.app.CACHE_ENABLED = False
        sid = self.new_task()
        task, src = self.app.read_task(sid)
        self.assertEqual(src, "db(无缓存模式)")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(self.app.CACHE.size(), 0)

    def test_only_terminal_state_cached(self):
        self.app.CACHE_ENABLED = True
        sid = self.new_task()
        self.app.read_task(sid)
        self.assertEqual(self.app.CACHE.size(), 0)
        self._finish_task(sid)
        self.app.read_task(sid)
        self.assertEqual(self.app.CACHE.size(), 1)

    def test_cache_conflict_falls_back_to_db(self):
        self.app.CACHE_ENABLED = True
        sid = self.new_task()
        self._finish_task(sid)
        task, _ = self.app.read_task(sid)
        stale = dict(task, result_msg="stale", updated_at="2000-01-01 00:00:00")
        with self.app.CACHE._lock:
            self.app.CACHE._data[sid] = (stale, time.time() - 5)
        self.assertTrue(db.update_task(sid, "done", {"result_msg": "fresh"}))
        fresh, src = self.app.read_task(sid)
        self.assertEqual(fresh["result_msg"], "fresh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
