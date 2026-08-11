from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import feishu_archive.reader_auth as reader_auth
from feishu_archive.reader_auth import (
    PERMANENT_UNLOCK_MARKER,
    PermanentUnlockPolicyError,
    ReaderSessionManager,
    SESSION_COOKIE,
    disable_permanent_unlock,
    enable_permanent_unlock,
    permanent_unlock_enabled,
)


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

    def test_permanent_unlock_persists_and_relock_revokes_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret_path = root / "reader.secret"
            marker_path = root / "mail-reader.always-unlocked"
            manager = ReaderSessionManager(
                secret_path,
                permanent_unlock_path=marker_path,
            )
            token = manager.create_session(manager.unlock_secret)
            self.assertIsNotNone(token)
            cookie = f"{SESSION_COOKIE}={token}"
            self.assertTrue(manager.validate_cookie(cookie))
            self.assertFalse(manager.allows_request(None))

            self.assertTrue(enable_permanent_unlock(marker_path))
            self.assertFalse(enable_permanent_unlock(marker_path))
            self.assertTrue(permanent_unlock_enabled(marker_path))
            self.assertEqual(marker_path.read_bytes(), PERMANENT_UNLOCK_MARKER)
            self.assertEqual(marker_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            generation = root / "mail-reader.policy-generation"
            self.assertTrue(generation.is_file())
            self.assertEqual(generation.stat().st_mode & 0o777, 0o600)
            self.assertTrue(manager.allows_request(None))
            self.assertFalse(manager.validate_cookie(cookie))

            restarted = ReaderSessionManager(
                secret_path,
                permanent_unlock_path=marker_path,
            )
            self.assertTrue(restarted.allows_request(None))
            self.assertTrue(disable_permanent_unlock(marker_path))
            self.assertFalse(disable_permanent_unlock(marker_path))
            self.assertFalse(restarted.allows_request(cookie))
            self.assertTrue(secret_path.is_file())

    def test_unobserved_policy_toggle_and_lock_while_absent_revoke_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker_path = root / "mail-reader.always-unlocked"
            manager = ReaderSessionManager(
                root / "reader.secret",
                permanent_unlock_path=marker_path,
            )
            token = manager.create_session(manager.unlock_secret)
            self.assertIsNotNone(token)
            cookie = f"{SESSION_COOKIE}={token}"
            self.assertTrue(manager.allows_request(cookie))

            enable_permanent_unlock(marker_path)
            disable_permanent_unlock(marker_path)
            self.assertFalse(manager.allows_request(cookie))

            replacement = manager.create_session(manager.unlock_secret)
            self.assertIsNotNone(replacement)
            replacement_cookie = f"{SESSION_COOKIE}={replacement}"
            self.assertTrue(manager.allows_request(replacement_cookie))
            self.assertFalse(disable_permanent_unlock(marker_path))
            self.assertFalse(manager.allows_request(replacement_cookie))
            fresh = manager.create_session(manager.unlock_secret)
            self.assertIsNotNone(fresh)
            self.assertTrue(manager.allows_request(f"{SESSION_COOKIE}={fresh}"))

    def test_relock_recovers_partial_or_symlink_policy_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker_path = root / "mail-reader.always-unlocked"
            marker_path.write_bytes(b"partial")
            marker_path.chmod(0o400)
            self.assertTrue(disable_permanent_unlock(marker_path))
            self.assertFalse(marker_path.exists())

            target = root / "unrelated"
            target.write_text("keep", encoding="utf-8")
            marker_path.symlink_to(target)
            self.assertTrue(disable_permanent_unlock(marker_path))
            self.assertFalse(marker_path.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_relock_repairs_partial_or_symlink_generation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker_path = root / "mail-reader.always-unlocked"
            generation = root / "mail-reader.policy-generation"
            generation.write_bytes(b"partial")
            generation.chmod(0o400)
            self.assertFalse(disable_permanent_unlock(marker_path))
            ReaderSessionManager(
                root / "reader.secret",
                permanent_unlock_path=marker_path,
            )

            generation.unlink()
            target = root / "generation-target"
            target.write_text("keep", encoding="utf-8")
            generation.symlink_to(target)
            self.assertFalse(disable_permanent_unlock(marker_path))
            self.assertTrue(generation.is_file())
            self.assertFalse(generation.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_concurrent_stale_read_cannot_overwrite_newer_locked_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker_path = root / "mail-reader.always-unlocked"
            enable_permanent_unlock(marker_path)
            manager = ReaderSessionManager(
                root / "reader.secret",
                permanent_unlock_path=marker_path,
            )
            stale_read_started = threading.Event()
            release_stale_read = threading.Event()
            locked_request_finished = threading.Event()
            results: dict[str, bool] = {}
            original = reader_auth._read_access_policy_state

            def delayed_read(path: Path) -> tuple[bool, tuple[object, ...]]:
                state = original(path)
                if threading.current_thread().name == "stale-reader":
                    stale_read_started.set()
                    self.assertTrue(release_stale_read.wait(timeout=2))
                return state

            def stale_request() -> None:
                results["stale"] = manager.allows_request(None)

            def locked_request() -> None:
                results["locked"] = manager.allows_request(None)
                locked_request_finished.set()

            with patch(
                "feishu_archive.reader_auth._read_access_policy_state",
                side_effect=delayed_read,
            ):
                stale_thread = threading.Thread(target=stale_request, name="stale-reader")
                stale_thread.start()
                self.assertTrue(stale_read_started.wait(timeout=2))
                disable_permanent_unlock(marker_path)
                locked_thread = threading.Thread(target=locked_request, name="locked-reader")
                locked_thread.start()
                self.assertFalse(locked_request_finished.wait(timeout=0.1))
                release_stale_read.set()
                stale_thread.join(timeout=2)
                locked_thread.join(timeout=2)

            self.assertTrue(results["stale"])
            self.assertFalse(results["locked"])
            self.assertFalse(manager.allows_request(None))

    def test_invalid_permanent_unlock_markers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret_path = root / "reader.secret"
            marker_path = root / "mail-reader.always-unlocked"
            marker_path.write_bytes(PERMANENT_UNLOCK_MARKER)
            marker_path.chmod(0o644)
            with self.assertRaises(PermanentUnlockPolicyError):
                ReaderSessionManager(secret_path, permanent_unlock_path=marker_path)

            marker_path.chmod(0o600)
            marker_path.write_text("wrong\n", encoding="utf-8")
            with self.assertRaises(PermanentUnlockPolicyError):
                permanent_unlock_enabled(marker_path)

            marker_path.unlink()
            target = root / "target"
            target.write_bytes(PERMANENT_UNLOCK_MARKER)
            target.chmod(0o600)
            marker_path.symlink_to(target)
            with self.assertRaises(PermanentUnlockPolicyError):
                permanent_unlock_enabled(marker_path)


if __name__ == "__main__":
    unittest.main()
