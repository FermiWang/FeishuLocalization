from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from feishu_archive.cli import (
    _app_config,
    _client,
    _mail_client,
    _mail_app_config,
    _mail_oauth_readiness,
    build_parser,
    main,
)
from feishu_archive.config import (
    DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    DEFAULT_MAX_MAIL_BYTES,
    DEFAULT_SCOPES,
    MAIL_SCOPES,
    MAIL_TOKEN_NAMESPACE,
)
from feishu_archive.keychain import MemoryTokenStore


class AppConfigTests(unittest.TestCase):
    def test_mail_preflight_is_independent_of_archive_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.cli.ArchiveDatabase"
        ) as archive_database:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["--archive-dir", temp, "mail-preflight"])

            archive_database.assert_not_called()
            self.assertFalse((Path(temp) / "archive.sqlite3").exists())
            self.assertTrue((Path(temp) / "mail.sqlite3").exists())
            secret = Path(temp) / "reader.secret"
            self.assertTrue(secret.exists())
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            self.assertIn("schema/FTS", output.getvalue())

    def test_chat_command_does_not_initialize_mail_database(self) -> None:
        archive_database = MagicMock()
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.cli.ArchiveDatabase", return_value=archive_database
        ), patch("feishu_archive.cli.MailDatabase") as mail_database, patch(
            "feishu_archive.cli.seed_demo",
            return_value={"conversations": 0, "messages_written": 0, "attachments": 0},
        ):
            main(["--archive-dir", temp, "demo"])

        archive_database.initialize.assert_called_once_with()
        mail_database.assert_not_called()

    def test_serve_degrades_mail_directory_failure_without_stopping_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mail").write_text("not a directory", encoding="utf-8")
            error = io.StringIO()
            with patch("feishu_archive.cli.serve") as serve_reader, contextlib.redirect_stderr(
                error
            ):
                main(["--archive-dir", temp, "serve"])

            self.assertTrue((root / "archive.sqlite3").exists())
            kwargs = serve_reader.call_args.kwargs
            self.assertIsNone(kwargs["mail_database"])
            self.assertIsNone(kwargs["mail_session_manager"])
            self.assertIsNone(kwargs["mail_sync_controller"])
            self.assertIn("FileExistsError", kwargs["mail_unavailable_reason"])
            self.assertIn("聊天与知识库阅读仍可用", error.getvalue())

    def test_serve_degrades_mail_database_initialization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.cli.MailDatabase"
        ) as mail_database, patch("feishu_archive.cli.serve") as serve_reader:
            mail_database.return_value.initialize.side_effect = RuntimeError("schema failed")
            main(["--archive-dir", temp, "serve"])

        kwargs = serve_reader.call_args.kwargs
        self.assertIsNone(kwargs["mail_database"])
        self.assertIn("schema failed", kwargs["mail_unavailable_reason"])

    def test_serve_degrades_invalid_reader_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "reader.secret").write_text("short\n", encoding="utf-8")
            with patch("feishu_archive.cli.serve") as serve_reader:
                main(["--archive-dir", temp, "serve"])

        kwargs = serve_reader.call_args.kwargs
        self.assertIsNone(kwargs["mail_session_manager"])
        self.assertIn("解锁密钥无效", kwargs["mail_unavailable_reason"])

    def test_sync_defaults_to_all_history(self) -> None:
        args = build_parser().parse_args(["sync", "--all-discovered"])
        self.assertTrue(args.all_discovered)
        self.assertIsNone(args.days)

    def test_attachment_resume_defaults_to_four_workers(self) -> None:
        args = build_parser().parse_args(["attachments"])
        self.assertEqual(args.workers, 4)

    def test_scheduled_sync_uses_two_day_overlap(self) -> None:
        args = build_parser().parse_args(["scheduled-sync"])
        self.assertEqual(args.days, 2)

    def test_wiki_rebuild_can_force_local_rendering(self) -> None:
        args = build_parser().parse_args(["wiki-rebuild", "--force"])
        self.assertEqual(args.command, "wiki-rebuild")
        self.assertTrue(args.force)

    def test_mail_lane_has_independent_sync_and_reader_commands(self) -> None:
        sync_args = build_parser().parse_args(["mail-sync"])
        self.assertIsNone(sync_args.days)
        self.assertEqual(sync_args.max_pages, 5000)
        self.assertEqual(sync_args.max_mail_gib, DEFAULT_MAX_MAIL_BYTES / 1024**3)
        self.assertEqual(
            sync_args.max_attachment_mib,
            DEFAULT_MAX_MAIL_ATTACHMENT_BYTES / 1024**2,
        )
        self.assertEqual(sync_args.max_mail_gib, 10)
        self.assertEqual(sync_args.max_attachment_mib, 1024)
        self.assertIsNone(sync_args.folder)
        self.assertFalse(sync_args.skip_attachments)
        bounded_args = build_parser().parse_args(["mail-sync", "--days", "30"])
        self.assertEqual(bounded_args.days, 30)
        scoped_args = build_parser().parse_args(
            ["mail-sync", "--folder", "DRAFT", "--folder", "7421369296749756417"]
        )
        self.assertEqual(scoped_args.folder, ["DRAFT", "7421369296749756417"])
        scheduled_args = build_parser().parse_args(["mail-scheduled-sync"])
        self.assertEqual(scheduled_args.days, 2)
        self.assertEqual(scheduled_args.max_mail_gib, 10)
        self.assertEqual(scheduled_args.max_attachment_mib, 1024)
        reader_args = build_parser().parse_args(["mail-reader-url", "--open"])
        self.assertTrue(reader_args.open)
        permanent_args = build_parser().parse_args(["mail-reader-url", "--permanent"])
        self.assertTrue(permanent_args.permanent)
        self.assertFalse(permanent_args.lock)
        lock_args = build_parser().parse_args(["mail-reader-url", "--lock"])
        self.assertTrue(lock_args.lock)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["mail-reader-url", "--permanent", "--lock"]
            )

    def test_mail_reader_permanent_unlock_and_relock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = io.StringIO()
            with patch("feishu_archive.cli.webbrowser.open") as opener, contextlib.redirect_stdout(
                output
            ):
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--permanent",
                        "--open",
                    ]
                )

            marker = root / "mail-reader.always-unlocked"
            secret = root / "reader.secret"
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertTrue(secret.is_file())
            opened_url = opener.call_args.args[0]
            self.assertEqual(opened_url, "http://127.0.0.1:8765/?mode=mail")
            self.assertNotIn("#", output.getvalue())
            self.assertNotIn(secret.read_text(encoding="utf-8").strip(), output.getvalue())

            with patch("feishu_archive.cli.serve") as serve_reader:
                main(["--archive-dir", temp, "serve"])
            deployed_manager = serve_reader.call_args.kwargs["mail_session_manager"]
            self.assertIsNotNone(deployed_manager)
            self.assertTrue(deployed_manager.allows_request(None))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["--archive-dir", temp, "mail-reader-url", "--lock"])
            self.assertFalse(marker.exists())
            self.assertTrue(secret.is_file())
            self.assertTrue((root / "mail-reader.policy-generation").is_file())
            self.assertIn("已恢复短期会话锁定", output.getvalue())

    def test_mail_reader_lock_rejects_open_without_changing_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            main(["--archive-dir", temp, "mail-reader-url", "--permanent"])
            marker = Path(temp) / "mail-reader.always-unlocked"
            self.assertTrue(marker.exists())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--lock",
                        "--open",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertTrue(marker.exists())

    def test_invalid_reader_secret_does_not_enable_permanent_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "reader.secret").write_text("short\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--permanent",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertFalse((root / "mail-reader.always-unlocked").exists())

    def test_nonloopback_permanent_unlock_does_not_create_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--host",
                        "0.0.0.0",
                        "--permanent",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertFalse((Path(temp) / "mail-reader.always-unlocked").exists())

    def test_misresolved_localhost_does_not_create_permanent_policy(self) -> None:
        external_resolution = [
            (2, 1, 6, "", ("192.0.2.10", 0)),
        ]
        with tempfile.TemporaryDirectory() as temp, patch(
            "feishu_archive.web.socket.getaddrinfo",
            return_value=external_resolution,
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                main(
                    [
                        "--archive-dir",
                        temp,
                        "mail-reader-url",
                        "--host",
                        "localhost",
                        "--permanent",
                    ]
                )
            self.assertEqual(context.exception.code, 2)
            self.assertFalse((Path(temp) / "mail-reader.always-unlocked").exists())

    def test_app_config_reads_credentials_from_keychain(self) -> None:
        store = MemoryTokenStore()
        store.set("app_id", "cli_keychain")
        store.set("cli_keychain:app_secret", "keychain-secret")

        with patch.dict(os.environ, {}, clear=True):
            config = _app_config(8877, store)

        self.assertEqual(config.app_id, "cli_keychain")
        self.assertEqual(config.app_secret, "keychain-secret")
        self.assertEqual(config.redirect_uri, "http://127.0.0.1:8877/oauth/callback")

    def test_environment_credentials_take_priority(self) -> None:
        store = MemoryTokenStore()
        store.set("app_id", "cli_keychain")
        store.set("cli_env:app_secret", "stored-secret")

        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "cli_env", "FEISHU_APP_SECRET": "env-secret"},
            clear=True,
        ):
            config = _app_config(store=store)

        self.assertEqual(config.app_id, "cli_env")
        self.assertEqual(config.app_secret, "env-secret")

    def test_mail_app_can_be_separate_or_reuse_main_app_without_dropping_scopes(self) -> None:
        store = MemoryTokenStore()
        store.set("mail_app_id", "cli_mail")
        store.set("cli_mail:app_secret", "mail-secret")
        with patch.dict(os.environ, {}, clear=True):
            dedicated = _mail_app_config(8877, store)
        self.assertEqual(dedicated.app_id, "cli_mail")
        self.assertEqual(dedicated.scopes, MAIL_SCOPES)

        shared_store = MemoryTokenStore()
        shared_store.set("app_id", "cli_shared")
        shared_store.set("cli_shared:app_secret", "shared-secret")
        with patch.dict(os.environ, {}, clear=True):
            shared = _mail_app_config(8877, shared_store)
        self.assertEqual(shared.app_id, "cli_shared")
        self.assertEqual(set(shared.scopes), set(DEFAULT_SCOPES) | set(MAIL_SCOPES))

    def test_scheduled_mail_sync_requires_refresh_token_and_all_read_scopes(self) -> None:
        store = MemoryTokenStore()
        store.set("mail_app_id", "cli_mail")
        store.set("cli_mail:app_secret", "mail-secret")
        with patch.dict(os.environ, {}, clear=True):
            ready, detail = _mail_oauth_readiness(store)
        self.assertFalse(ready)
        self.assertIn("mail-auth", detail)

        store.set("cli_mail:mail:refresh_token", "refresh")
        store.set("cli_mail:mail:scope", " ".join(set(MAIL_SCOPES) - {"offline_access"}))
        with patch.dict(os.environ, {}, clear=True):
            ready, detail = _mail_oauth_readiness(store)
        self.assertTrue(ready)
        self.assertIn("cli_mail", detail)

    def test_mail_client_factory_always_uses_mail_token_namespace(self) -> None:
        store = MemoryTokenStore()
        store.set("app_id", "cli_shared")
        store.set("cli_shared:app_secret", "shared-secret")

        with patch.dict(os.environ, {}, clear=True), patch(
            "feishu_archive.cli.KeychainStore", return_value=store
        ):
            client = _mail_client()

        self.assertEqual(client.token_namespace, MAIL_TOKEN_NAMESPACE)
        self.assertEqual(client.account("refresh_token"), "cli_shared:mail:refresh_token")

        default_client = _client(store=store)
        self.assertEqual(default_client.config.app_id, client.config.app_id)
        self.assertEqual(default_client.account("refresh_token"), "cli_shared:refresh_token")
        self.assertIsNone(store.get("cli_shared:refresh_token"))


if __name__ == "__main__":
    unittest.main()
