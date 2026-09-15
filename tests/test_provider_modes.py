import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import external_provider
import provider_control
import recognize
import state_machine as sm


class ProviderModeBase(unittest.TestCase):
    def setUp(self):
        db.reset_pool(os.path.join(tempfile.mkdtemp(), "provider.db"))
        db.init_db()
        provider_control.ensure_schema()

    def new_task(self, key):
        return db.insert_task(
            1,
            key,
            {"key": "k.png", "path": "/tmp/k.png", "hash": "h"},
            status="pending",
        )


class TestProviderModes(ProviderModeBase):
    def test_internal_backend_requires_no_external_token(self):
        with mock.patch.dict(os.environ, {"VISION_BACKEND": "internal"}, clear=False):
            os.environ.pop("EXTERNAL_API_TOKEN", None)
            ok, version, msg = recognize.call_vision_model(1, 0)
        self.assertTrue(ok)
        self.assertEqual(version, "10.0.19045")
        self.assertIn("internal", msg)

    def test_external_backend_requires_token(self):
        with mock.patch.dict(os.environ, {
            "VISION_BACKEND": "external",
            "EXTERNAL_API_CREDENTIAL_VERSION": "1",
        }, clear=False):
            os.environ.pop("EXTERNAL_API_TOKEN", None)
            ok, version, msg = recognize.call_vision_model(1, 0)
        self.assertFalse(ok)
        self.assertIsNone(version)
        self.assertIn("token missing", msg)
        self.assertTrue(recognize.is_external_control_block(msg))

    def test_control_block_classifier_does_not_hide_vendor_runtime_failures(self):
        self.assertTrue(recognize.is_external_control_block("external provider paused: incident"))
        self.assertTrue(recognize.is_external_control_block("external provider 401: stale credential generation"))
        self.assertFalse(recognize.is_external_control_block("external provider timeout"))
        self.assertFalse(recognize.is_external_control_block("external provider 503"))

    def test_token_incident_pauses_provider_and_rotates_generation(self):
        before = provider_control.get_state()
        incident, contained = provider_control.begin_token_exposure_incident(
            "token pasted into public log",
            suspected_since="2026-09-15T09:00:00Z",
        )
        self.assertEqual(contained["enabled"], 0)
        self.assertEqual(contained["token_status"], "revoked")
        self.assertEqual(contained["credential_version"], before["credential_version"] + 1)
        self.assertEqual(contained["active_incident_id"], incident)

        with self.assertRaises(RuntimeError):
            provider_control.begin_token_exposure_incident("second overlapping leak")

        with mock.patch.dict(os.environ, {
            "EXTERNAL_API_TOKEN": "do-not-log-me",
            "EXTERNAL_API_CREDENTIAL_VERSION": str(before["credential_version"]),
        }, clear=False):
            ok, _, msg = external_provider.call_vision_model(1, 0)
        self.assertFalse(ok)
        self.assertIn("paused", msg)
        self.assertTrue(recognize.is_external_control_block(msg))

        stages = [e["stage"] for e in provider_control.list_events(incident)]
        for stage in (
            "detected",
            "evidence_snapshot",
            "credential_revoked_rotated",
            "provider_paused",
            "initial_escalation",
        ):
            self.assertIn(stage, stages)

    def test_close_requires_human_vendor_and_rotated_credential(self):
        incident, _ = provider_control.begin_token_exposure_incident("suspected leak")
        with self.assertRaises(ValueError):
            provider_control.close_incident(
                incident,
                human_approved=False,
                vendor_confirmed=True,
                credential_deployed=True,
            )
        with self.assertRaises(ValueError):
            provider_control.close_incident(
                incident,
                human_approved=True,
                vendor_confirmed=False,
                credential_deployed=True,
            )
        with self.assertRaises(ValueError):
            provider_control.close_incident(
                incident,
                human_approved=True,
                vendor_confirmed=True,
                credential_deployed=False,
            )
        with self.assertRaises(RuntimeError):
            provider_control.close_incident(
                "INC-wrong-id",
                human_approved=True,
                vendor_confirmed=True,
                credential_deployed=True,
            )

        closed = provider_control.close_incident(
            incident,
            human_approved=True,
            vendor_confirmed=True,
            credential_deployed=True,
        )
        self.assertEqual(closed["enabled"], 1)
        self.assertEqual(closed["token_status"], "active")
        self.assertEqual(closed["incident_status"], "closed")
        self.assertIsNone(closed["active_incident_id"])

        old_generation = closed["credential_version"] - 1
        with mock.patch.dict(os.environ, {
            "EXTERNAL_API_TOKEN": "rotated-secret",
            "EXTERNAL_API_CREDENTIAL_VERSION": str(old_generation),
        }, clear=False):
            ok, _, msg = external_provider.call_vision_model(1, 0)
        self.assertFalse(ok)
        self.assertIn("401", msg)
        self.assertTrue(recognize.is_external_control_block(msg))

        with mock.patch.dict(os.environ, {
            "EXTERNAL_API_TOKEN": "rotated-secret",
            "EXTERNAL_API_CREDENTIAL_VERSION": str(closed["credential_version"]),
        }, clear=False):
            ok, version, _ = external_provider.call_vision_model(1, 0)
        self.assertTrue(ok)
        self.assertEqual(version, "10.0.19045")

    def test_internal_finalize_does_not_depend_on_credit_account(self):
        sid = self.new_task("internal-no-credit")
        row = db.claim_next_task("worker-internal", 5.0)
        self.assertEqual(row["id"], sid)

        con = db.connect()
        con.execute("DELETE FROM accounts WHERE user_id=1")
        con.commit()
        con.close()

        ok, balance = sm.finalize_done_owned(
            sid,
            "worker-internal",
            row["processing_generation"],
            0.0,
            "10.0.19045",
            "10.0.19045",
            "internal ok",
        )
        self.assertTrue(ok)
        self.assertIsNone(balance)
        self.assertEqual(db.get_task(sid)["status"], "done")

    def test_two_tasks_cannot_spend_the_last_credit_twice(self):
        sid1 = self.new_task("credit-race-1")
        sid2 = self.new_task("credit-race-2")
        first = db.claim_next_task("worker-a", 5.0)
        second = db.claim_next_task("worker-b", 5.0)
        self.assertEqual({first["id"], second["id"]}, {sid1, sid2})

        con = db.connect()
        con.execute("UPDATE accounts SET balance=1 WHERE user_id=1")
        con.commit()
        con.close()

        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def finalize(row, owner):
            barrier.wait()
            result = sm.finalize_done_owned(
                row["id"],
                owner,
                row["processing_generation"],
                1.0,
                "10.0.19045",
                "10.0.19045",
                "external ok",
            )
            with lock:
                results.append((row["id"], result))

        t1 = threading.Thread(target=finalize, args=(first, "worker-a"))
        t2 = threading.Thread(target=finalize, args=(second, "worker-b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(sum(1 for _, (ok, _) in results if ok), 1)
        con = db.connect()
        balance = con.execute("SELECT balance FROM accounts WHERE user_id=1").fetchone()["balance"]
        con.close()
        self.assertEqual(balance, 0.0)
        self.assertGreaterEqual(balance, 0.0)
        statuses = {db.get_task(sid1)["status"], db.get_task(sid2)["status"]}
        self.assertEqual(statuses, {"done", "processing"})

    def test_db_rejects_negative_credit_even_for_raw_update(self):
        con = db.connect()
        try:
            with self.assertRaises(Exception):
                con.execute("UPDATE accounts SET balance=-1 WHERE user_id=1")
                con.commit()
            con.rollback()
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
