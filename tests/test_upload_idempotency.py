import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import object_store
import state_machine as sm


class TestUploadIdempotency(unittest.TestCase):
    def setUp(self):
        db.reset_pool(os.path.join(tempfile.mkdtemp(), "test.db"))
        db.init_db()
        self._old_upload_dir = object_store.UPLOAD_DIR
        object_store.UPLOAD_DIR = tempfile.mkdtemp()

    def tearDown(self):
        object_store.UPLOAD_DIR = self._old_upload_dir

    def test_reservation_is_persisted_before_object_side_effect(self):
        body = b"\x89PNGdemo"
        h = object_store.hash_bytes(body)

        row, created = db.reserve_upload(1, "idem-1", h)
        self.assertTrue(created)
        self.assertEqual(row["status"], "uploading")
        self.assertEqual(row["file_hash"], h)

        key = object_store.make_object_key(row["id"])
        self.assertFalse(object_store.object_exists(key))

        replay, created_again = db.reserve_upload(1, "idem-1", h)
        self.assertFalse(created_again)
        self.assertEqual(replay["id"], row["id"])

    def test_same_idempotency_key_exposes_payload_mismatch(self):
        h1 = object_store.hash_bytes(b"\x89PNGone")
        h2 = object_store.hash_bytes(b"\x89PNGtwo")

        row, created = db.reserve_upload(1, "idem-2", h1)
        self.assertTrue(created)

        replay, created_again = db.reserve_upload(1, "idem-2", h2)
        self.assertFalse(created_again)
        self.assertEqual(replay["id"], row["id"])
        self.assertEqual(replay["file_hash"], h1)
        self.assertNotEqual(replay["file_hash"], h2)

    def test_only_stale_upload_can_be_reclaimed(self):
        h = object_store.hash_bytes(b"\x89PNGlease")
        row, _ = db.reserve_upload(1, "idem-3", h)

        self.assertFalse(db.claim_stale_upload(row["id"], h))
        self.assertTrue(db.update_task(row["id"], "uploading", {"upload_lease_until": 0}))
        self.assertTrue(db.claim_stale_upload(row["id"], h))
        self.assertFalse(db.claim_stale_upload(row["id"], h))

    def test_retry_reuses_one_stable_object_and_advances_state(self):
        body = b"\x89PNGrecover"
        h = object_store.hash_bytes(body)
        row, _ = db.reserve_upload(1, "idem-4", h)
        sid = row["id"]
        key = object_store.make_object_key(sid)

        # 模拟首次 API 在 DB reservation 之后、对象完成之前崩溃。
        self.assertTrue(db.update_task(sid, "uploading", {"upload_lease_until": 0}))
        self.assertTrue(db.claim_stale_upload(sid, h))

        meta = object_store.put_object(body, "evidence.png", key=key)
        self.assertTrue(
            sm.transition(
                sid,
                "pending",
                object_key=meta["key"],
                object_path=meta["path"],
                file_hash=meta["hash"],
                upload_lease_until=0,
            )
        )

        replay, created_again = db.reserve_upload(1, "idem-4", h)
        self.assertFalse(created_again)
        self.assertEqual(replay["id"], sid)
        self.assertEqual(db.get_task(sid)["status"], "pending")
        self.assertEqual(os.listdir(object_store.UPLOAD_DIR), [key])


if __name__ == "__main__":
    unittest.main(verbosity=2)
