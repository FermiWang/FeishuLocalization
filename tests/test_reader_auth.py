from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from feishu_archive.reader_auth import ReaderSessionManager, SESSION_COOKIE


class ReaderSessionManagerTests(unittest.TestCase):
    def test_secret_permissions_and_session_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secret_path = Path(temp) / "reader.secret"
            manager = ReaderSessionManager(secret_path)

            self.assertTrue(secret_path.is_file())
            self.assertEqual(os.stat(secret_path).st_mode & 0o777, 0o600)
            self.assertIsNone(manager.create_session("wrong"))

            session = manager.create_session(manager.unlock_secret)
            self.assertIsNotNone(session)
            cookie = f"{SESSION_COOKIE}={session}"
            self.assertTrue(manager.validate_cookie(cookie))
            manager.revoke_cookie(cookie)
            self.assertFalse(manager.validate_cookie(cookie))

    def test_existing_secret_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secret_path = Path(temp) / "reader.secret"
            first = ReaderSessionManager(secret_path)
            second = ReaderSessionManager(secret_path)
            self.assertEqual(first.unlock_secret, second.unlock_secret)


if __name__ == "__main__":
    unittest.main()
