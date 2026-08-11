from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from feishu_archive.config import ArchivePaths
from feishu_archive.mail_database import MailDatabase
from feishu_archive.mail_provider import FakeMailProvider
from feishu_archive.mail_sync import MailAuthorizationError, MailSyncer


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
        self.assertEqual(folder_ids, {"projects", "INBOX", "SENT"})
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
            MailSyncer(self.database, self.provider, self.paths).sync(folders=[""], days=1)

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


if __name__ == "__main__":
    unittest.main()
