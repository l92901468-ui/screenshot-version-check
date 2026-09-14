import os, sys, time, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db, recognize, billing
import state_machine as sm
from cache import TTLCache


class Base(unittest.TestCase):
    def setUp(self):
        # 每个用例用独立临时库，互不干扰
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
