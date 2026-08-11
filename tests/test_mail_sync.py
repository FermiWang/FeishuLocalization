from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from feishu_archive.config import ArchivePaths
from feishu_archive.mail_database import MailDatabase
from feishu_archive.mail_provider import FakeMailProvider
from feishu_archive.mail_sync import (
    MailAuthorizationError,
    MailSyncPartialError,
    MailSyncer,
)


def encoded(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


class MailSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.paths = ArchivePaths(Path(self.temp.name))
        self.paths.ensure()
        self.database = MailDatabase(self.paths.mail_database)
        self.database.initialize()
        self.provider = FakeMailProvider(
            profile_value={
                "primary_email_address": "owner@example.com",
                "mailbox_id": "owner@example.com",
                "display_name": "Owner",
            },
            folders=[
                {
                    "id": "projects",
                    "name": "Projects",
                    "folder_type": 2,
                    "unread_message_count": 3,
                }
            ],
            messages={
                "msg-1": {
                    "message_id": "msg-1",
                    "thread_id": "thread-1",
                    "smtp_message_id": "<msg-1@example.com>",
                    "subject": "Local archive",
                    "head_from": {"name": "Alice", "mail_address": "alice@example.com"},
                    "to": [{"name": "Owner", "mail_address": "owner@example.com"}],
                    "reply_to": "reply@example.com",
                    "internal_date": "1776000000000",
                    "message_state": 1,
                    "folder_id": "INBOX",
                    "label_ids": ["UNREAD", "IMPORTANT"],
                    "priority_type": "normal",
                    "body_preview": encoded("预览"),
                    "body_plain_text": encoded("仅显示本机纯文本邮件正文"),
                    "body_html": encoded("<img src='https://tracker.invalid/pixel'>"),
                    "raw": encoded("From: alice@example.com\r\n\r\nBody"),
                    "attachments": [
                        {
                            "id": "att-inline",
                            "filename": "report.pdf",
                            "body": encoded(b"PDF-DATA"),
                        },
                        {
                            "id": "att-url",
                            "filename": "unsafe.html",
                        },
                    ],
                }
            },
            attachment_urls={
                "msg-1": {
                    "att-url": "https://download.example.com/att-url?signature=secret"
                }
            },
            downloads={
                "https://download.example.com/att-url?signature=secret": b"<script>no</script>"
            },
        )
        self.disk_usage = SimpleNamespace(
            total=500 * 1024**3,
            used=50 * 1024**3,
            free=450 * 1024**3,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sync_persists_metadata_cas_blobs_and_forced_attachment_states(self) -> None:
        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(
                self.database,
                self.provider,
                self.paths,
                max_mail_bytes=10 * 1024**2,
                max_attachment_bytes=1024**2,
            ).sync(folders=["INBOX"], days=1)

        self.assertEqual(counts.messages_seen, 1)
        self.assertEqual(counts.messages_written, 1)
        self.assertEqual(counts.attachments_downloaded, 2)
        mailbox = self.database.list_mailboxes()[0]
        detail = self.database.get_message(
            self.database.find_message(mailbox["id"], "msg-1")["id"]
        )
        self.assertEqual(detail["sender_address"], "alice@example.com")
        self.assertEqual(detail["send_date"], 1_776_000_000_000)
        self.assertEqual(detail["received_date"], 1_776_000_000_000)
        self.assertEqual(detail["body_plain_text"], "仅显示本机纯文本邮件正文")
        self.assertIsNotNone(detail["raw_blob_id"])
        self.assertIsNotNone(detail["body_html_blob_id"])
        self.assertNotIn("tracker.invalid", detail["raw_json"])
        self.assertEqual(
            {(item["role"], item["normalized_address"]) for item in detail["recipients"]},
            {
                ("from", "alice@example.com"),
                ("to", "owner@example.com"),
                ("reply_to", "reply@example.com"),
            },
        )
        attachments = {item["provider_attachment_id"]: item for item in detail["attachments"]}
        self.assertEqual(attachments["att-inline"]["content_type"], "application/pdf")
        self.assertEqual(attachments["att-inline"]["status"], "available")
        self.assertEqual(attachments["att-url"]["content_type"], "text/html")
        self.assertEqual(attachments["att-url"]["status"], "quarantined")
        for item in attachments.values():
            blob_path = self.paths.root / item["relative_path"]
            self.assertTrue(blob_path.is_file())
            self.assertEqual(oct(blob_path.stat().st_mode & 0o777), "0o600")

        folder_ids = {item["provider_folder_id"] for item in self.database.list_folders(mailbox["id"])}
        self.assertEqual(
            folder_ids,
            {
                "projects",
                "INBOX",
                "SENT",
                "DRAFT",
                "SCHEDULED",
                "TRASH",
                "SPAM",
                "ARCHIVED",
            },
        )
        status = self.database.status()
        self.assertEqual(status["downloaded_attachments"], 2)
        self.assertEqual(status["latest_sync"]["status"], "success")

    def test_second_overlap_sync_is_idempotent_and_does_not_redownload_attachment(self) -> None:
        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            syncer = MailSyncer(self.database, self.provider, self.paths)
            first = syncer.sync(folders=["INBOX"], days=1)
            download_calls_after_first = sum(
                name == "open_download_url" for name, _ in self.provider.calls
            )
            second = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )

        self.assertEqual(first.messages_written, 1)
        self.assertEqual(second.messages_written, 0)
        self.assertEqual(self.database.status()["messages"], 1)
        self.assertEqual(
            sum(name == "open_download_url" for name, _ in self.provider.calls),
            download_calls_after_first,
        )

    def test_duplicate_message_ids_in_one_page_are_ingested_once(self) -> None:
        self.provider.search_message_ids = ["msg-1", "msg-1"]

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )

        batch_calls = [
            arguments
            for name, arguments in self.provider.calls
            if name == "batch_get_messages"
        ]
        self.assertEqual(batch_calls[0]["message_ids"], ["msg-1"])
        self.assertEqual(counts.messages_seen, 1)
        self.assertEqual(self.database.status()["messages"], 1)

    def test_overlap_sync_repairs_same_size_corrupted_cas_attachment(self) -> None:
        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )
        mailbox = self.database.list_mailboxes()[0]
        message = self.database.find_message(mailbox["id"], "msg-1")
        detail = self.database.get_message(message["id"])
        attachment = next(
            item
            for item in detail["attachments"]
            if item["provider_attachment_id"] == "att-url"
        )
        target = self.paths.root / attachment["relative_path"]
        expected = self.provider.downloads[
            "https://download.example.com/att-url?signature=secret"
        ]
        target.write_bytes(b"x" * len(expected))
        self.provider.calls.clear()

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )

        self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(
            sum(name == "open_download_url" for name, _ in self.provider.calls),
            1,
        )
        self.assertEqual(
            self.database.blob_integrity_report(self.paths.root),
            {"checked": 4, "missing": 0, "corrupt": 0},
        )

    def test_missing_mail_scope_stops_before_any_archive_write(self) -> None:
        self.provider.granted_scope_values.remove("mail:user_mailbox.message.body:read")
        before = self.database.status()

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            with self.assertRaisesRegex(MailAuthorizationError, "message.body:read"):
                MailSyncer(self.database, self.provider, self.paths).sync(days=1)

        after = self.database.status()
        self.assertEqual(after, before)
        self.assertEqual(self.database.list_mailboxes(), [])
        self.assertEqual(self.provider.calls, [("granted_scopes", {})])

    def test_offline_access_not_echoed_by_access_token_does_not_block_manual_sync(self) -> None:
        self.provider.granted_scope_values.discard("offline_access")

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1, skip_attachments=True
            )

        self.assertEqual(counts.messages_seen, 1)
        self.assertEqual(self.database.latest_sync_run()["status"], "partial")

    def test_omitted_message_fields_do_not_overwrite_existing_archive_data(self) -> None:
        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )
        mailbox = self.database.list_mailboxes()[0]
        message_id = self.database.find_message(mailbox["id"], "msg-1")["id"]
        before = self.database.get_message(message_id)

        self.provider.messages["msg-1"] = {
            "message_id": "msg-1",
            "message_state": 2,
        }
        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )

        after = self.database.get_message(message_id)
        for key in (
            "subject",
            "body_plain_text",
            "sender_name",
            "sender_address",
            "raw_blob_id",
            "body_html_blob_id",
        ):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["recipients"], before["recipients"])
        self.assertEqual(after["labels"], before["labels"])
        self.assertEqual(after["attachments"], before["attachments"])
        self.assertEqual(
            [
                (item["provider_folder_id"], item["name"], item["folder_type"])
                for item in after["folders"]
            ],
            [
                (item["provider_folder_id"], item["name"], item["folder_type"])
                for item in before["folders"]
            ],
        )

    def test_all_attachment_download_failures_mark_sync_partial(self) -> None:
        self.provider.messages["msg-1"]["attachments"] = [
            {"id": "att-fail", "filename": "failed.pdf"}
        ]
        self.provider.attachment_urls = {}

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )

        self.assertEqual(counts.attachments_seen, 1)
        self.assertEqual(counts.attachments_downloaded, 0)
        self.assertEqual(counts.attachments_skipped, 1)
        latest = self.database.latest_sync_run()
        self.assertEqual(latest["status"], "partial")
        self.assertIn("未返回下载地址", latest["error"])

    def test_default_sync_searches_every_system_and_nested_custom_folder(self) -> None:
        self.provider.folders = [
            {
                "id": "project-root",
                "name": "ProjectX",
                "parent_folder_id": "0",
                "folder_type": 2,
            },
            {
                "id": "project-year",
                "name": "FY2026",
                "parent_folder_id": "project-root",
                "folder_type": 2,
            },
        ]

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(days=1)

        searches = [
            arguments for name, arguments in self.provider.calls if name == "search_messages"
        ]
        self.assertEqual(
            [arguments["folder"] for arguments in searches],
            [
                "inbox",
                "sent",
                "draft",
                "scheduled",
                "trash",
                "spam",
                "archive",
                "ProjectX",
                "ProjectX/FY2026",
            ],
        )
        self.assertEqual(counts.windows_scanned, 9)
        self.assertEqual(counts.folders_seen, 9)
        self.assertFalse(
            any(name == "list_message_ids" for name, _arguments in self.provider.calls)
        )
        latest = self.database.latest_sync_run()
        self.assertEqual(latest["status"], "success")
        self.assertIsNotNone(latest["window_start"])
        self.assertIsNotNone(latest["window_end"])
        self.assertLess(latest["window_start"], latest["window_end"])
        mailbox = self.database.list_mailboxes()[0]
        self.assertEqual(
            {item["provider_folder_id"] for item in self.database.list_folders(mailbox["id"])},
            {
                "INBOX",
                "SENT",
                "DRAFT",
                "SCHEDULED",
                "TRASH",
                "SPAM",
                "ARCHIVED",
                "project-root",
                "project-year",
            },
        )

    def test_default_full_history_lists_every_provider_folder_id_and_scheduled_label(self) -> None:
        self.provider.folders = [
            {
                "id": "project-root",
                "name": "ProjectX",
                "parent_folder_id": "0",
                "folder_type": 2,
            },
            {
                "id": "project-year",
                "name": "FY2026",
                "parent_folder_id": "project-root",
                "folder_type": 2,
            },
        ]
        self.provider.messages = {}

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync()

        listings = [
            arguments
            for name, arguments in self.provider.calls
            if name == "list_message_ids"
        ]
        self.assertEqual(
            [(item["folder_id"], item["label_id"]) for item in listings],
            [
                ("INBOX", None),
                ("SENT", None),
                ("DRAFT", None),
                (None, "SCHEDULED"),
                ("TRASH", None),
                ("SPAM", None),
                ("ARCHIVED", None),
                ("project-root", None),
                ("project-year", None),
            ],
        )
        self.assertFalse(
            any(name == "search_messages" for name, _arguments in self.provider.calls)
        )
        self.assertEqual(counts.folders_seen, 9)
        self.assertEqual(counts.windows_scanned, 9)
        self.assertEqual(counts.pages_scanned, 9)
        latest = self.database.latest_sync_run()
        self.assertEqual(latest["status"], "success")
        self.assertIsNone(latest["window_start"])
        self.assertIsNotNone(latest["window_end"])
        mailbox = self.database.list_mailboxes()[0]
        for folder_id in (
            "INBOX",
            "SENT",
            "DRAFT",
            "SCHEDULED",
            "TRASH",
            "SPAM",
            "ARCHIVED",
            "project-root",
            "project-year",
        ):
            state = self.database.get_sync_state(mailbox["id"], f"folder:{folder_id}")
            self.assertEqual(state["status"], "success")
            self.assertIsNone(state["window_start"])
            self.assertEqual(state["window_end"], latest["window_end"])
            extra = json.loads(state["extra_json"])
            self.assertEqual(extra["mode"], "full")
            self.assertEqual(extra["run_id"], latest["id"])
            self.assertEqual(extra["pages"], 1)

    def test_full_history_paginates_and_deduplicates_ids_across_folders(self) -> None:
        self.provider.folders = [
            {"id": "folder-a", "name": "A", "parent_folder_id": "0", "folder_type": 2},
            {"id": "folder-b", "name": "B", "parent_folder_id": "0", "folder_type": 2},
        ]
        message_ids = [f"history-{index:02d}" for index in range(21)]
        self.provider.messages = {
            message_id: {
                "message_id": message_id,
                "subject": message_id,
                "folder_id": "folder-a",
            }
            for message_id in message_ids
        }
        self.provider.listed_message_ids = {
            "folder-a": list(message_ids),
            "folder-b": [message_ids[0], message_ids[-1]],
        }

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["folder-a", "folder-b"]
            )

        listings = [
            arguments
            for name, arguments in self.provider.calls
            if name == "list_message_ids"
        ]
        self.assertEqual(
            [(item["folder_id"], item["page_token"]) for item in listings],
            [("folder-a", None), ("folder-a", "20"), ("folder-b", None)],
        )
        batch_calls = [
            arguments
            for name, arguments in self.provider.calls
            if name == "batch_get_messages"
        ]
        self.assertEqual([len(item["message_ids"]) for item in batch_calls], [20, 1])
        self.assertEqual(
            [message_id for item in batch_calls for message_id in item["message_ids"]],
            message_ids,
        )
        self.assertEqual(counts.pages_scanned, 3)
        self.assertEqual(counts.windows_scanned, 2)
        self.assertEqual(counts.message_ids_seen, 21)
        self.assertEqual(counts.messages_seen, 21)
        self.assertEqual(counts.messages_written, 21)
        self.assertEqual(self.database.status()["messages"], 21)

    def test_full_history_uses_enumerated_folder_when_message_detail_omits_folder(self) -> None:
        self.provider.folders = [
            {
                "id": "archive-2020",
                "name": "Archive 2020",
                "parent_folder_id": "0",
                "folder_type": 2,
            }
        ]
        self.provider.messages = {
            "history-without-folder": {
                "message_id": "history-without-folder",
                "subject": "Old message",
            }
        }
        self.provider.listed_message_ids = {
            "archive-2020": ["history-without-folder"]
        }

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["archive-2020"]
            )

        self.assertEqual(counts.messages_written, 1)
        mailbox = self.database.list_mailboxes()[0]
        message = self.database.find_message(mailbox["id"], "history-without-folder")
        detail = self.database.get_message(message["id"])
        self.assertEqual(
            [item["provider_folder_id"] for item in detail["folders"]],
            ["archive-2020"],
        )
        latest = self.database.latest_sync_run()
        self.assertEqual(latest["status"], "success")
        self.assertIsNone(latest["window_start"])
        self.assertIsNotNone(latest["window_end"])

    def test_scheduled_full_history_falls_back_to_scheduled_folder(self) -> None:
        self.provider.folders = []
        self.provider.messages = {
            "scheduled-without-folder": {
                "message_id": "scheduled-without-folder",
                "subject": "Send later",
                "label_ids": ["SCHEDULED"],
            }
        }

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["SCHEDULED"]
            )

        self.assertEqual(counts.messages_written, 1)
        mailbox = self.database.list_mailboxes()[0]
        message = self.database.find_message(mailbox["id"], "scheduled-without-folder")
        detail = self.database.get_message(message["id"])
        self.assertEqual(
            [item["provider_folder_id"] for item in detail["folders"]],
            ["SCHEDULED"],
        )

    def test_full_history_ignores_unrelated_invalid_folder_tree_for_explicit_id(self) -> None:
        self.provider.folders = [
            {"id": "valid", "name": "Valid", "folder_type": 2},
            {
                "id": "orphan",
                "name": "Orphan",
                "parent_folder_id": "missing-parent",
                "folder_type": 2,
            },
        ]
        self.provider.messages = {}

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["valid"]
            )

        self.assertEqual(counts.windows_scanned, 1)
        listing = next(
            arguments
            for name, arguments in self.provider.calls
            if name == "list_message_ids"
        )
        self.assertEqual(listing["folder_id"], "valid")

    def test_full_history_page_budget_failure_is_audited_as_error(self) -> None:
        message_ids = [f"budget-{index:02d}" for index in range(21)]
        self.provider.messages = {
            message_id: {
                "message_id": message_id,
                "subject": message_id,
                "folder_id": "projects",
            }
            for message_id in message_ids
        }
        self.provider.listed_message_ids = {"projects": message_ids}

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            with self.assertRaisesRegex(MailSyncPartialError, "max_pages"):
                MailSyncer(self.database, self.provider, self.paths).sync(
                    folders=["projects"], max_pages=1, skip_attachments=True
                )

        latest = self.database.latest_sync_run()
        self.assertEqual(latest["status"], "error")
        self.assertEqual(latest["pages_scanned"], 1)
        self.assertEqual(latest["messages_seen"], 0)
        mailbox = self.database.list_mailboxes()[0]
        state = self.database.get_sync_state(mailbox["id"], "folder:projects")
        self.assertEqual(state["status"], "error")
        self.assertIn("max_pages", state["error"])

    def test_explicit_custom_folder_id_resolves_to_name_and_unknown_id_fails(self) -> None:
        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["DRAFT", "projects"], days=1
            )

        searches = [
            arguments for name, arguments in self.provider.calls if name == "search_messages"
        ]
        self.assertEqual(
            [arguments["folder"] for arguments in searches],
            ["draft", "Projects"],
        )
        self.assertEqual(counts.messages_seen, 0)

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            with self.assertRaisesRegex(ValueError, "未找到邮件文件夹"):
                MailSyncer(self.database, self.provider, self.paths).sync(
                    folders=["missing-folder"], days=1
                )

    def test_exact_mixed_case_custom_name_wins_over_system_alias(self) -> None:
        self.provider.folders.append(
            {
                "id": "custom-inbox",
                "name": "Inbox",
                "parent_folder_id": "0",
                "folder_type": 2,
            }
        )

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["Inbox"], days=1
            )

        search = next(
            arguments for name, arguments in self.provider.calls if name == "search_messages"
        )
        self.assertEqual(search["folder"], "Inbox")

    def test_custom_folder_named_like_uppercase_system_id_uses_its_exact_name(self) -> None:
        self.provider.folders.append(
            {
                "id": "custom-uppercase-inbox",
                "name": "INBOX",
                "parent_folder_id": "0",
                "folder_type": 2,
            }
        )

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["custom-uppercase-inbox"], days=1
            )

        search = next(
            arguments for name, arguments in self.provider.calls if name == "search_messages"
        )
        self.assertEqual(search["folder"], "INBOX")

    def test_unavailable_message_id_is_retried_immediately_in_the_same_window(self) -> None:
        recovered_message = dict(self.provider.messages["msg-1"])
        with (
            patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage),
            patch("feishu_archive.mail_sync.time.sleep"),
            patch.object(
                self.provider,
                "batch_get_messages",
                side_effect=[
                    {"messages": [], "unavailable_message_ids": ["msg-1"]},
                    {"messages": [recovered_message], "unavailable_message_ids": []},
                ],
            ) as batch_get,
        ):
            counts = MailSyncer(self.database, self.provider, self.paths).sync(
                folders=["INBOX"], days=1
            )

        self.assertEqual(batch_get.call_count, 2)
        self.assertEqual(counts.messages_written, 1)
        self.assertEqual(self.database.status()["messages"], 1)
        self.assertEqual(self.database.latest_sync_run()["status"], "success")

    def test_invalid_custom_folder_tree_fails_before_database_writes(self) -> None:
        self.provider.folders = [
            {
                "id": "orphan",
                "name": "Child",
                "parent_folder_id": "missing-parent",
                "folder_type": 2,
            }
        ]

        with patch("feishu_archive.mail_sync.shutil.disk_usage", return_value=self.disk_usage):
            with self.assertRaisesRegex(ValueError, "父文件夹不存在"):
                MailSyncer(self.database, self.provider, self.paths).sync(days=1)

        self.assertEqual(self.database.list_mailboxes(), [])
        self.assertIsNone(self.database.latest_sync_run())


if __name__ == "__main__":
    unittest.main()
