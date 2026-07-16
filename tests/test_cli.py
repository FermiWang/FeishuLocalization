from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from feishu_archive.cli import _app_config, build_parser
from feishu_archive.keychain import MemoryTokenStore


class AppConfigTests(unittest.TestCase):
    def test_sync_defaults_to_all_history(self) -> None:
        args = build_parser().parse_args(["sync", "--all-discovered"])
        self.assertTrue(args.all_discovered)
        self.assertIsNone(args.days)

    def test_attachment_resume_defaults_to_four_workers(self) -> None:
        args = build_parser().parse_args(["attachments"])
        self.assertEqual(args.workers, 4)

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


if __name__ == "__main__":
    unittest.main()
