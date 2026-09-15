import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import state_machine as sm
import worker


class WorkerOwnershipBase(unittest.TestCase):
    def setUp(self):
        db.reset_pool(os.path.join(tempfile.mkdtemp(), "worker.db"))
        db.init_db()

    def new_task(self, key=None):
        return db.insert_task(
            1,
            key or f"worker-{time.time_ns()}",
            {"key": "k.png", "path": "/tmp/k.png", "hash": "h"},
            status="pending",
        )


class TestWorkerOwnership(WorkerOwnershipBase):
    def test_two_workers_cannot_both_claim_same_pending(self):
        sid = self.new_task()
        barrier = threading.Barrier(2)
        claimed = []
        lock = threading.Lock()

        def attempt(owner):
            barrier.wait()
            row = db.claim_next_task(owner, 5.0)
            with lock:
                claimed.append((owner, row))

        a = threading.Thread(target=attempt, args=("worker-a",))
        b = threading.Thread(target=attempt, args=("worker-b",))
        a.start()
        b.start()
        a.join()
        b.join()

        winners = [(owner, row) for owner, row in claimed if row is not None]
        self.assertEqual(len(winners), 1)
        owner, row = winners[0]
        self.assertEqual(row["id"], sid)
        self.assertEqual(row["processing_owner"], owner)
        self.assertEqual(row["processing_generation"], 1)

    def test_expired_processing_lease_is_reclaimed_with_new_generation(self):
        sid = self.new_task()
        first = db.claim_next_task("worker-a", 0.15)
        self.assertEqual(first["id"], sid)
        self.assertEqual(first["processing_generation"], 1)

        time.sleep(0.20)
        second = db.claim_next_task("worker-b", 5.0)
        self.assertIsNotNone(second)
        self.assertEqual(second["id"], sid)
        self.assertEqual(second["processing_owner"], "worker-b")
        self.assertEqual(second["processing_generation"], 2)

    def test_heartbeat_extends_current_owner_lease(self):
        sid = self.new_task()
        first = db.claim_next_task("worker-a", 0.30)
        gen = first["processing_generation"]
        old_until = first["processing_lease_until"]
        time.sleep(0.05)
        self.assertTrue(db.renew_processing_lease(sid, "worker-a", gen, 1.0))
        renewed = db.get_task(sid)
        self.assertGreater(renewed["processing_lease_until"], old_until)
        self.assertFalse(db.renew_processing_lease(sid, "wrong-owner", gen, 1.0))

    def test_stale_worker_cannot_overwrite_new_owner(self):
        sid = self.new_task()
        first = db.claim_next_task("worker-a", 0.15)
        old_gen = first["processing_generation"]
        time.sleep(0.20)
        second = db.claim_next_task("worker-b", 5.0)
        new_gen = second["processing_generation"]

        self.assertFalse(sm.transition_owned(
            sid, "dlq", "worker-a", old_gen, retry_count=3, result_msg="stale"
        ))
        row = db.get_task(sid)
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["processing_owner"], "worker-b")
        self.assertEqual(row["processing_generation"], new_gen)

        self.assertTrue(sm.transition_owned(
            sid, "dlq", "worker-b", new_gen, retry_count=3, result_msg="current"
        ))
        self.assertEqual(db.get_task(sid)["status"], "dlq")

    def test_stale_worker_cannot_charge_and_current_owner_charges_once(self):
        sid = self.new_task()
        first = db.claim_next_task("worker-a", 0.15)
        old_gen = first["processing_generation"]
        time.sleep(0.20)
        second = db.claim_next_task("worker-b", 5.0)
        new_gen = second["processing_generation"]

        before = db.connect()
        try:
            balance_before = before.execute(
                "SELECT balance FROM accounts WHERE user_id=1"
            ).fetchone()["balance"]
        finally:
            before.close()

        stale_ok, stale_balance = sm.finalize_done_owned(
            sid, "worker-a", old_gen, 1.0,
            "10.0.19045", "10.0.19045", "stale-result",
        )
        self.assertFalse(stale_ok)
        self.assertIsNone(stale_balance)

        con = db.connect()
        try:
            after_stale = con.execute(
                "SELECT balance FROM accounts WHERE user_id=1"
            ).fetchone()["balance"]
        finally:
            con.close()
        self.assertEqual(after_stale, balance_before)

        ok, final_balance = sm.finalize_done_owned(
            sid, "worker-b", new_gen, 1.0,
            "10.0.19045", "10.0.19045", "current-result",
        )
        self.assertTrue(ok)
        self.assertEqual(final_balance, balance_before - 1.0)
        self.assertEqual(db.get_task(sid)["status"], "done")

        # 同一个 generation 再 finalize 也不能重复扣费。
        again, _ = sm.finalize_done_owned(
            sid, "worker-b", new_gen, 1.0,
            "10.0.19045", "10.0.19045", "duplicate",
        )
        self.assertFalse(again)
        con = db.connect()
        try:
            balance_after_again = con.execute(
                "SELECT balance FROM accounts WHERE user_id=1"
            ).fetchone()["balance"]
        finally:
            con.close()
        self.assertEqual(balance_after_again, balance_before - 1.0)

    def test_legacy_schema_gets_processing_ownership_columns(self):
        legacy_path = os.path.join(tempfile.mkdtemp(), "legacy.db")
        import sqlite3
        con = sqlite3.connect(legacy_path)
        con.execute("""CREATE TABLE submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            upload_lease_until REAL NOT NULL DEFAULT 0,
            object_key TEXT, object_path TEXT, file_hash TEXT,
            idempotency_key TEXT, result_msg TEXT,
            detected_version TEXT, required_version TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        con.commit()
        con.close()

        db.reset_pool(legacy_path)
        db.init_db()
        con = db.connect()
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(submissions)").fetchall()}
        finally:
            con.close()
        self.assertIn("processing_owner", cols)
        self.assertIn("processing_generation", cols)
        self.assertIn("processing_lease_until", cols)


class TestHeartbeatOwnershipUncertainty(WorkerOwnershipBase):
    def _claimed_heartbeat(self):
        sid = self.new_task()
        row = db.claim_next_task("worker-a", 5.0)
        return sid, row["processing_generation"], worker.ProcessingLeaseHeartbeat(
            sid, "worker-a", row["processing_generation"]
        )

    def test_db_error_marks_uncertain_but_not_lost(self):
        _, _, heartbeat = self._claimed_heartbeat()
        with mock.patch.object(
            db, "renew_processing_lease", side_effect=RuntimeError("temporary db outage")
        ):
            self.assertEqual(heartbeat.renew_once(), "uncertain")
        self.assertTrue(heartbeat.uncertain.is_set())
        self.assertFalse(heartbeat.lost.is_set())

    def test_later_success_clears_uncertainty(self):
        _, _, heartbeat = self._claimed_heartbeat()
        with mock.patch.object(
            db,
            "renew_processing_lease",
            side_effect=[RuntimeError("temporary db outage"), True],
        ):
            self.assertEqual(heartbeat.renew_once(), "uncertain")
            self.assertTrue(heartbeat.uncertain.is_set())
            self.assertEqual(heartbeat.renew_once(), "renewed")
        self.assertFalse(heartbeat.uncertain.is_set())
        self.assertFalse(heartbeat.lost.is_set())

    def test_authoritative_renewal_rejection_marks_lost(self):
        _, _, heartbeat = self._claimed_heartbeat()
        with mock.patch.object(db, "renew_processing_lease", return_value=False):
            self.assertEqual(heartbeat.renew_once(), "lost")
        self.assertTrue(heartbeat.lost.is_set())
        self.assertFalse(heartbeat.uncertain.is_set())

    def test_uncertain_worker_still_attempts_fenced_finalize(self):
        sid = self.new_task()

        class UncertainHeartbeat:
            def __init__(self, *_args, **_kwargs):
                self.lost = threading.Event()
                self.uncertain = threading.Event()
                self.uncertain.set()

            def start(self):
                pass

            def stop(self):
                pass

        with mock.patch.object(worker, "ProcessingLeaseHeartbeat", UncertainHeartbeat), \
             mock.patch.object(
                 worker.recognize,
                 "call_vision_model",
                 return_value=(True, "10.0.19045", "ok"),
             ):
            self.assertTrue(worker.handle_one("worker-a"))

        row = db.get_task(sid)
        self.assertEqual(row["status"], "done")
        con = db.connect()
        try:
            balance = con.execute(
                "SELECT balance FROM accounts WHERE user_id=1"
            ).fetchone()["balance"]
        finally:
            con.close()
        self.assertEqual(balance, 99.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
