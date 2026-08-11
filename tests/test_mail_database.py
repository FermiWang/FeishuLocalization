import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from feishu_archive.mail_database import MAIL_SCHEMA_VERSION, MailDatabase


class MailDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mail.sqlite3"
        self.database = MailDatabase(self.path)
        self.database.initialize()
        self.mailbox_id = self.database.upsert_mailbox(
            {
                "provider": "feishu",
                "mailbox_id": "ou_owner",
                "primary_email_address": "owner@example.com",
                "display_name": "Owner",
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def message(self, message_id: str = "msg_1", **overrides):
        item = {
            "message_id": message_id,
            "thread_id": "thread_1",
            "smtp_message_id": "<one@example.com>",
            "subject": "项目离线档案",
            "head_from": {"name": "Alice", "mail_address": "ALICE@example.com"},
            "to": [{"name": "Owner", "mail_address": "owner@example.com"}],
            "cc": [{"name": "Bob", "mail_address": "bob@example.com"}],
            "date": 1_720_000_000_000,
            "internal_date": 1_720_000_010_000,
            "message_state": "normal",
            "priority_type": "normal",
            "folder_id": "INBOX",
            "label_ids": ["important"],
            "body_plain_text": "这是可以全文搜索的纯文本正文",
            "body_html": "<img src='https://tracker.invalid/pixel'>HTML-SECRET",
            "raw": "UkFXLU1JTUUgU0VDUkVU",
            "attachments": [
                {
                    "id": "att_1",
                    "filename": "说明.txt",
                    "content_type": "text/plain",
                    "size": 12,
                    "body": "QVRUQUNITUVOVC1TRUNSRVQ=",
                }
            ],
        }
        item.update(overrides)
        return item

    def test_migration_version_permissions_and_future_version_guard(self) -> None:
        self.assertEqual(self.database.schema_version(), MAIL_SCHEMA_VERSION)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        self.database.initialize()
        self.assertEqual(self.database.integrity_check(), "ok")

        future_path = Path(self.temp.name) / "future.sqlite3"
        with sqlite3.connect(future_path) as con:
            con.execute(f"PRAGMA user_version={MAIL_SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(RuntimeError, "高于程序支持版本"):
            MailDatabase(future_path).initialize()

    def test_mailbox_and_folder_upserts_are_normalized_and_idempotent(self) -> None:
        same_id = self.database.upsert_mailbox(
            {
                "provider": "FEISHU",
                "provider_mailbox_id": "ou_owner",
                "primary_email_address": "new@example.com",
            }
        )
        self.assertEqual(same_id, self.mailbox_id)
        self.assertEqual(self.database.get_mailbox(same_id)["primary_email_address"], "new@example.com")

        ids = self.database.replace_folders(
            self.mailbox_id,
            [
                {"folder_id": "INBOX", "name": "收件箱", "folder_type": "inbox"},
                {"folder_id": "SENT", "name": "已发送", "folder_type": "sent"},
            ],
            seen_at=100,
        )
        self.assertEqual(len(ids), 2)
        self.database.replace_folders(
            self.mailbox_id,
            [{"folder_id": "INBOX", "name": "收件箱", "folder_type": "inbox"}],
            seen_at=200,
        )
        self.assertEqual([item["provider_folder_id"] for item in self.database.list_folders(self.mailbox_id)], ["INBOX"])
        all_folders = self.database.list_folders(self.mailbox_id, include_missing=True)
        self.assertEqual({item["status"] for item in all_folders}, {"active", "missing"})

    def test_sparse_message_folder_link_preserves_folder_metadata(self) -> None:
        self.database.replace_folders(
            self.mailbox_id,
            [
                {
                    "folder_id": "INBOX",
                    "name": "收件箱",
                    "folder_type": "inbox",
                    "unread_count": 7,
                    "total_count": 42,
                }
            ],
            seen_at=100,
        )
        message_id, _ = self.database.upsert_message(
            self.mailbox_id,
            {"message_id": "msg-folder", "folder_id": "INBOX"},
            seen_at=200,
        )
        self.database.upsert_folder(
            self.mailbox_id,
            {"folder_id": "INBOX"},
            seen_at=300,
        )

        folder = self.database.get_message(message_id)["folders"][0]
        self.assertEqual(folder["name"], "收件箱")
        self.assertEqual(folder["folder_type"], "inbox")
        self.assertEqual(folder["unread_count"], 7)
        self.assertEqual(folder["total_count"], 42)

    def test_message_upsert_replaces_relations_and_does_not_persist_inline_payloads(self) -> None:
        message_id, created = self.database.upsert_message(self.mailbox_id, self.message(), seen_at=100)
        self.assertTrue(created)
        same_id, created_again = self.database.upsert_message(
            self.mailbox_id,
            self.message(
                subject="项目离线档案（更新）",
                to=[{"name": "Carol", "mail_address": "carol@example.com"}],
                cc=[],
                label_ids=["follow-up"],
                attachments=[
                    {"id": "att_2", "filename": "更新.pdf", "content_type": "application/pdf"}
                ],
            ),
            seen_at=200,
        )
        self.assertEqual(same_id, message_id)
        self.assertFalse(created_again)

        detail = self.database.get_message(message_id)
        self.assertEqual(detail["subject"], "项目离线档案（更新）")
        self.assertEqual(detail["sender_address"], "alice@example.com")
        self.assertEqual(
            {(item["role"], item["normalized_address"]) for item in detail["recipients"]},
            {("from", "alice@example.com"), ("to", "carol@example.com")},
        )
        self.assertEqual([item["provider_label_id"] for item in detail["labels"]], ["follow-up"])
        self.assertEqual([item["provider_attachment_id"] for item in detail["attachments"]], ["att_2"])
        self.assertEqual([item["provider_folder_id"] for item in detail["folders"]], ["INBOX"])
        self.assertEqual(detail["send_date"], 1_720_000_000_000)
        self.assertEqual(detail["received_date"], 1_720_000_010_000)
        self.assertEqual(detail["priority"], "normal")
        self.assertIsNone(detail["body_html_blob_id"])
        self.assertIsNone(detail["raw_blob_id"])
        self.assertNotIn("HTML-SECRET", detail["raw_json"])
        self.assertNotIn("UkFXLU1JTUUgU0VDUkVU", detail["raw_json"])
        self.assertNotIn("QVRUQUNITUVOVC1TRUNSRVQ", detail["attachments"][0]["raw_json"])

        with self.database.connection() as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        self.assertNotIn("body_html", columns)
        self.assertNotIn("raw_mime", columns)

    def test_same_provider_message_id_is_unique_only_within_mailbox(self) -> None:
        first, first_created = self.database.upsert_message(self.mailbox_id, self.message())
        second_mailbox = self.database.upsert_mailbox(
            {"mailbox_id": "ou_second", "primary_email_address": "second@example.com"}
        )
        second, second_created = self.database.upsert_message(second_mailbox, self.message())
        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertNotEqual(first, second)

    def test_search_filters_and_fts_refresh_after_replacement(self) -> None:
        message_id, _ = self.database.upsert_message(self.mailbox_id, self.message(), seen_at=100)
        self.database.upsert_message(
            self.mailbox_id,
            self.message("msg_2", subject="短标题", body_plain_text="另一封邮件", folder_id="SENT"),
            seen_at=100,
        )
        self.assertEqual(
            [item["id"] for item in self.database.search_messages("全文搜索")],
            [message_id],
        )
        self.assertEqual(
            [item["id"] for item in self.database.search_messages("alice@example.com")],
            [self.database.find_message(self.mailbox_id, "msg_2")["id"], message_id],
        )
        self.assertEqual(
            [item["provider_message_id"] for item in self.database.query_messages(folder_id="SENT")],
            ["msg_2"],
        )
        self.assertEqual(
            [item["provider_message_id"] for item in self.database.query_messages(label_id="important")],
            ["msg_2", "msg_1"],
        )
        self.assertEqual(
            [item["provider_message_id"] for item in self.database.search_messages("短")],
            ["msg_2"],
        )

        self.database.upsert_message(
            self.mailbox_id,
            self.message(body_plain_text="正文已经替换", subject="新主题"),
            seen_at=200,
        )
        self.assertEqual(self.database.search_messages("全文搜索"), [])
        self.assertEqual([item["id"] for item in self.database.search_messages("正文已经替换")], [message_id])
        self.assertEqual(self.database.integrity_check(), "ok")

    def test_tombstone_requires_distinct_reconciliations_and_upsert_revives(self) -> None:
        message_id, _ = self.database.upsert_message(self.mailbox_id, self.message(), seen_at=100)
        self.assertEqual(self.database.mark_unseen_messages(self.mailbox_id, 200), 0)
        self.assertEqual(self.database.mark_unseen_messages(self.mailbox_id, 200), 0)
        self.assertIsNone(self.database.get_message(message_id)["tombstoned_at"])
        self.assertEqual(self.database.get_message(message_id)["missing_count"], 1)

        self.assertEqual(self.database.mark_unseen_messages(self.mailbox_id, 300), 1)
        self.assertEqual(self.database.query_messages(), [])
        self.assertEqual(len(self.database.query_messages(include_tombstoned=True)), 1)

        same_id, created = self.database.upsert_message(self.mailbox_id, self.message(), seen_at=400)
        self.assertEqual(same_id, message_id)
        self.assertFalse(created)
        revived = self.database.get_message(message_id)
        self.assertIsNone(revived["tombstoned_at"])
        self.assertEqual(revived["missing_count"], 0)

        self.assertEqual(self.database.mark_messages_tombstoned(self.mailbox_id, ["msg_1"]), 1)
        self.assertIsNotNone(self.database.get_message(message_id)["tombstoned_at"])

    def test_blob_attachment_linkage_and_safe_relative_paths(self) -> None:
        message_id, _ = self.database.upsert_message(self.mailbox_id, self.message())
        attachment_id = self.database.get_message(message_id)["attachments"][0]["id"]
        digest = "a" * 64
        blob_id = self.database.upsert_blob(digest, 12, "aa/attachment.bin", "text/plain")
        self.assertEqual(blob_id, self.database.upsert_blob(digest, 12, "aa/attachment.bin"))
        self.database.link_attachment_blob(attachment_id, blob_id, downloaded_at=123)
        attachment = self.database.get_attachment(attachment_id)
        self.assertEqual(attachment["status"], "downloaded")
        self.assertEqual(attachment["byte_size"], 12)
        self.assertEqual(attachment["sha256"], digest)
        self.assertEqual(self.database.list_pending_attachments(), [])
        with self.assertRaisesRegex(ValueError, "安全的相对路径"):
            self.database.upsert_blob("b" * 64, 1, "../escape")

    def test_blob_integrity_report_detects_same_size_corruption_and_missing_files(self) -> None:
        root = Path(self.temp.name)
        payload = b"verified-cas"
        digest = hashlib.sha256(payload).hexdigest()
        relative_path = Path("mail") / "blobs" / digest[:2] / digest
        target = root / relative_path
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)
        self.database.upsert_blob(digest, len(payload), str(relative_path))

        self.assertEqual(
            self.database.blob_integrity_report(root),
            {"checked": 1, "missing": 0, "corrupt": 0},
        )
        target.write_bytes(b"x" * len(payload))
        self.assertEqual(self.database.blob_integrity_report(root)["corrupt"], 1)
        target.unlink()
        report = self.database.blob_integrity_report(root)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["corrupt"], 0)

    def test_sync_state_runs_events_status_and_integrity(self) -> None:
        self.database.set_sync_state(
            self.mailbox_id,
            "folder:INBOX",
            window_start=100,
            window_end=200,
            page_token="next",
            last_message_at=150,
            status="running",
            extra={"window": 1},
        )
        state = self.database.get_sync_state(self.mailbox_id, "folder:INBOX")
        self.assertEqual(state["page_token"], "next")
        self.assertEqual(json.loads(state["extra_json"]), {"window": 1})

        interrupted = self.database.start_sync_run(self.mailbox_id, "manual")
        run_id = self.database.start_sync_run(self.mailbox_id, "scheduled")
        self.assertEqual(self.database.latest_sync_run()["id"], run_id)
        with self.database.connection() as con:
            old = con.execute("SELECT * FROM sync_runs WHERE id=?", (interrupted,)).fetchone()
        self.assertEqual(old["status"], "error")
        self.database.finish_sync_run(
            run_id,
            status="success",
            messages_seen=2,
            messages_written=1,
            attachments_skipped=1,
        )
        self.assertEqual(self.database.latest_sync_run(self.mailbox_id)["messages_seen"], 2)

        event_id, created = self.database.enqueue_event(
            self.mailbox_id,
            "evt_1",
            "message_received",
            "msg_1",
            {"message_id": "msg_1", "body_html": "EVENT-HTML-SECRET"},
            received_at=100,
        )
        duplicate_id, duplicate_created = self.database.enqueue_event(
            self.mailbox_id,
            "evt_1",
            "message_received",
            "msg_1",
            {"message_id": "msg_1"},
        )
        self.assertEqual(duplicate_id, event_id)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertNotIn("EVENT-HTML-SECRET", self.database.pending_events()[0]["payload_json"])
        self.database.finish_event(event_id, status="processed")
        self.assertEqual(self.database.pending_events(), [])

        message_id, _ = self.database.upsert_message(self.mailbox_id, self.message())
        status = self.database.status(self.mailbox_id)
        self.assertEqual(status["messages"], 1)
        self.assertEqual(status["attachments"], 1)
        self.assertEqual(status["pending_attachments"], 1)
        self.assertEqual(status["latest_sync"]["status"], "success")
        self.assertEqual(self.database.integrity_check(), "ok")
        self.assertIsNotNone(self.database.get_message(message_id))


if __name__ == "__main__":
    unittest.main()
