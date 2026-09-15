import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth_token
import db


class TestImmediateTokenRevocation(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "auth.db")
        db.reset_pool(self.path)
        db.init_db()
        self.uid = db.verify_user("demo", "demo123")
        self.assertIsNotNone(self.uid)

    def test_disabling_account_invalidates_existing_token_immediately(self):
        token = auth_token.issue_token(self.uid)
        self.assertEqual(auth_token.verify_token(token), self.uid)

        self.assertTrue(db.set_user_active(self.uid, False))
        with self.assertRaisesRegex(ValueError, "账号已禁用"):
            auth_token.verify_token(token)
        self.assertIsNone(db.verify_user("demo", "demo123"))

    def test_old_token_does_not_revive_after_account_is_reenabled(self):
        old_token = auth_token.issue_token(self.uid)
        old_version = db.get_user_auth_state(self.uid)["token_version"]

        db.set_user_active(self.uid, False)
        db.set_user_active(self.uid, True)
        new_version = db.get_user_auth_state(self.uid)["token_version"]
        self.assertEqual(new_version, old_version + 1)

        with self.assertRaisesRegex(ValueError, "token 已被撤销"):
            auth_token.verify_token(old_token)

        new_token = auth_token.issue_token(self.uid)
        self.assertEqual(auth_token.verify_token(new_token), self.uid)

    def test_token_version_can_revoke_sessions_without_disabling_account(self):
        token = auth_token.issue_token(self.uid)
        self.assertTrue(db.revoke_user_tokens(self.uid))
        self.assertTrue(db.get_user_auth_state(self.uid)["is_active"])

        with self.assertRaisesRegex(ValueError, "token 已被撤销"):
            auth_token.verify_token(token)

        refreshed = auth_token.issue_token(self.uid)
        self.assertEqual(auth_token.verify_token(refreshed), self.uid)

    def test_init_db_migrates_legacy_users_table(self):
        legacy_path = os.path.join(tempfile.mkdtemp(), "legacy.db")
        con = sqlite3.connect(legacy_path)
        con.execute(
            """CREATE TABLE users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   username TEXT UNIQUE NOT NULL,
                   password_hash TEXT NOT NULL)"""
        )
        con.commit()
        con.close()

        db.reset_pool(legacy_path)
        db.init_db()
        con = db.connect()
        try:
            cols = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        finally:
            con.close()
        self.assertIn("is_active", cols)
        self.assertIn("token_version", cols)


if __name__ == "__main__":
    unittest.main(verbosity=2)
