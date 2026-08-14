from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from feishu_archive.database import ArchiveDatabase
from feishu_archive.insights_sources import (
    archive_history_bounds,
    calendar_day_window,
    chunk_evidence,
    extract_daily_sources,
)
from feishu_archive.mail_database import MailDatabase


DAY = date(2026, 3, 29)
TIMEZONE = "Europe/Amsterdam"


class InsightsSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.archive = ArchiveDatabase(root / "archive.sqlite3")
        self.archive.initialize()
        self.mail = MailDatabase(root / "mail.sqlite3")
        self.mail.initialize()
        self.window = calendar_day_window(DAY, TIMEZONE)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_calendar_window_uses_adjacent_midnights_across_dst(self) -> None:
        spring = calendar_day_window("2026-03-29", ZoneInfo(TIMEZONE))
        autumn = calendar_day_window("2026-10-25", TIMEZONE)

        self.assertEqual(spring["end_s"] - spring["start_s"], 23 * 60 * 60)
        self.assertEqual(autumn["end_s"] - autumn["start_s"], 25 * 60 * 60)
        self.assertEqual(spring["start_ms"], spring["start_s"] * 1000)
        self.assertEqual(spring["end_ms"], spring["end_s"] * 1000)

    def test_extracts_more_than_500_chat_messages_without_raw_json(self) -> None:
        self.archive.set_metadata("current_user_open_id", "ou_me")
        self.archive.upsert_conversation({"chat_id": "oc_one", "name": "全量会话"})
        with self.archive.transaction() as con:
            con.executemany(
                """
                INSERT INTO messages(
                    message_id, chat_id, message_type, sender_id, sender_name,
                    created_at, body_text, raw_json, archived_at
                ) VALUES (?, 'oc_one', 'text', ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"om_{index:03d}",
                        "ou_me" if index % 2 else "ou_other",
                        "Me" if index % 2 else "Other",
                        self.window["start_ms"] + index,
                        f"message {index}",
                        '{"private":"RAW-SECRET"}',
                        self.window["start_ms"] + index,
                    )
                    for index in range(620)
                ],
            )

        result = extract_daily_sources(self.archive, self.mail, DAY, TIMEZONE)
        chats = [item for item in result["evidence"] if item["source_kind"] == "chat"]

        self.assertEqual(len(chats), 620)
        self.assertEqual(result["coverage"]["counts"]["chat"], 620)
        self.assertEqual(result["window"], result["coverage"]["window"])
        self.assertEqual({item["direction"] for item in chats}, {"sent", "received"})
        self.assertEqual(chats[0]["evidence_id"], f"chat:oc_one/{chats[0]['source_id']}")
        self.assertNotIn("RAW-SECRET", json.dumps(result, ensure_ascii=False))
        self.assertTrue(all(set(item) >= _required_evidence_keys() for item in chats))

    def test_deleted_and_recalled_chat_text_never_reaches_analysis(self) -> None:
        self.archive.upsert_conversation({"chat_id": "oc_hidden", "name": "撤回测试"})
        with self.archive.connection() as con:
            con.executemany(
                """
                INSERT INTO messages(
                    message_id, chat_id, message_type, created_at, deleted, recalled,
                    body_text, raw_json, archived_at
                ) VALUES (?, 'oc_hidden', 'text', ?, ?, ?, ?, '{}', ?)
                """,
                [
                    ("deleted", self.window["start_ms"] + 1, 1, 0, "DELETED-SECRET", self.window["start_ms"] + 1),
                    ("recalled", self.window["start_ms"] + 2, 0, 1, "RECALLED-SECRET", self.window["start_ms"] + 2),
                ],
            )

        result = extract_daily_sources(self.archive, self.mail, DAY, TIMEZONE)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("DELETED-SECRET", rendered)
        self.assertNotIn("RECALLED-SECRET", rendered)
        self.assertTrue(any("已删除或已撤回" in item for item in result["coverage"]["warnings"]))

    def test_mail_is_deduplicated_and_sent_folder_has_priority(self) -> None:
        mailbox_id = self._mailbox_with_folders()
        timestamp = self.window["start_ms"] + 10_000
        message_id, _ = self.mail.upsert_message(
            mailbox_id,
            {
                "message_id": "msg-multi",
                "thread_id": "mail-thread",
                "subject": "同时出现在多个文件夹",
                "head_from": {"name": "Owner", "mail_address": "owner@example.com"},
                "to": [{"name": "Recipient", "mail_address": "recipient@example.com"}],
                "bcc": [{"name": "Hidden", "mail_address": "hidden@example.com"}],
                "date": timestamp,
                "internal_date": timestamp,
                "folder_ids": ["SENT", "INBOX", "SPAM", "TRASH"],
                "body_plain_text": "mail body",
                "raw_json": {"private": "RAW-MAIL-SECRET"},
            },
        )
        with self.mail.connection() as con:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM message_folders WHERE message_id=?", (message_id,)).fetchone()[0],
                4,
            )

        result = extract_daily_sources(self.archive, self.mail, DAY, TIMEZONE)
        mails = [item for item in result["evidence"] if item["source_kind"] == "mail"]

        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0]["direction"], "sent")
        self.assertTrue(mails[0]["metadata"]["flags"]["spam"])
        self.assertTrue(mails[0]["metadata"]["flags"]["trash"])
        self.assertEqual(result["coverage"]["counts"]["mail_sent"], 1)
        rendered = json.dumps(result, ensure_ascii=False).lower()
        self.assertNotIn("hidden@example.com", rendered)
        self.assertNotIn("bcc", rendered)
        self.assertNotIn("raw-mail-secret", rendered)
        self.assertNotIn("raw_json", rendered)

    def test_draft_and_scheduled_are_not_counted_but_spam_and_trash_are(self) -> None:
        mailbox_id = self._mailbox_with_folders()
        timestamp = self.window["start_ms"] + 20_000
        for message_id, folder_id in (
            ("draft", "DRAFT"),
            ("scheduled", "SCHEDULED"),
            ("spam", "SPAM"),
            ("trash", "TRASH"),
        ):
            self.mail.upsert_message(
                mailbox_id,
                {
                    "message_id": message_id,
                    "subject": message_id,
                    "head_from": {"mail_address": "sender@example.com"},
                    "to": [{"mail_address": "owner@example.com"}],
                    "date": timestamp,
                    "internal_date": timestamp,
                    "folder_id": folder_id,
                    "body_plain_text": message_id,
                },
            )

        result = extract_daily_sources(self.archive, self.mail, DAY, TIMEZONE)
        mails = {item["source_id"]: item for item in result["evidence"] if item["source_kind"] == "mail"}

        self.assertEqual(set(mails), {"spam", "trash"})
        self.assertEqual(result["coverage"]["counts"]["mail_received"], 2)
        self.assertTrue(mails["spam"]["metadata"]["flags"]["spam"])
        self.assertTrue(mails["trash"]["metadata"]["flags"]["trash"])

    def test_mail_attachment_metadata_excludes_binary_and_paths(self) -> None:
        mailbox_id = self._mailbox_with_folders()
        timestamp = self.window["start_ms"] + 30_000
        message_id, _ = self.mail.upsert_message(
            mailbox_id,
            {
                "message_id": "with-attachment",
                "subject": "附件",
                "head_from": {"mail_address": "sender@example.com"},
                "to": [{"mail_address": "owner@example.com"}],
                "date": timestamp,
                "internal_date": timestamp,
                "folder_id": "INBOX",
                "body_plain_text": "正文",
                "attachments": [
                    {
                        "id": "att-1",
                        "filename": "evidence.pdf",
                        "content_type": "application/pdf",
                        "size": 123,
                        "body": "BINARY-SECRET",
                    }
                ],
            },
        )
        attachment_id = self.mail.get_message(message_id)["attachments"][0]["id"]
        blob_id = self.mail.upsert_blob("a" * 64, 123, "aa/private.pdf", "application/pdf")
        self.mail.link_attachment_blob(attachment_id, blob_id)

        result = extract_daily_sources(self.archive, self.mail, DAY, TIMEZONE)
        mail = next(item for item in result["evidence"] if item["source_kind"] == "mail")
        attachment = mail["metadata"]["attachments"][0]

        self.assertEqual(attachment["filename"], "evidence.pdf")
        self.assertEqual(attachment["content_type"], "application/pdf")
        self.assertEqual(attachment["byte_size"], 123)
        self.assertEqual(attachment["sha256"], "a" * 64)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("BINARY-SECRET", rendered)
        self.assertNotIn("private.pdf", rendered)

    def test_wiki_deduplicates_obj_token_and_marks_created_and_edited(self) -> None:
        with self.archive.connection() as con:
            con.execute(
                "INSERT INTO wiki_spaces(space_id, name) VALUES ('space-a', 'Knowledge')"
            )
        created_s = self.window["start_s"] + 60
        edited_s = self.window["start_s"] + 120
        self.archive.upsert_wiki_node(
            {
                "node_token": "node-origin",
                "obj_token": "doc-shared",
                "obj_type": "docx",
                "node_type": "origin",
                "title": "Current title",
                "obj_create_time": created_s,
                "obj_edit_time": edited_s,
            },
            space_id="space-a",
            parent_node_token=None,
            path="Current title",
            position=0,
            seen_at=self.window["start_ms"],
        )
        self.archive.upsert_wiki_node(
            {
                "node_token": "node-shortcut",
                "obj_token": "doc-shared",
                "obj_type": "docx",
                "node_type": "shortcut",
                "title": "Shortcut",
                "node_create_time": created_s + 1,
                "obj_edit_time": edited_s,
            },
            space_id="space-a",
            parent_node_token=None,
            path="Shortcut",
            position=1,
            seen_at=self.window["start_ms"],
        )
        self.archive.upsert_wiki_document(
            {
                "obj_token": "doc-shared",
                "obj_type": "docx",
                "title": "Current title",
                "revision_id": 7,
                "source_edit_time": edited_s,
                "content_text": "CURRENT SNAPSHOT",
                "content_sha256": "digest",
                "status": "synced",
            }
        )

        result = extract_daily_sources(self.archive, self.mail, DAY, TIMEZONE)
        wiki = [item for item in result["evidence"] if item["source_kind"] == "wiki"]

        self.assertEqual(len(wiki), 1)
        self.assertEqual(wiki[0]["source_id"], "doc-shared")
        self.assertEqual(wiki[0]["text"], "CURRENT SNAPSHOT")
        self.assertEqual(wiki[0]["metadata"]["events"], ["created", "edited"])
        self.assertEqual(wiki[0]["metadata"]["node_tokens"], ["node-origin", "node-shortcut"])
        self.assertEqual(result["coverage"]["counts"]["wiki_created"], 1)
        self.assertEqual(result["coverage"]["counts"]["wiki_edited"], 1)

    def test_historical_wiki_creation_does_not_send_newer_revision_text(self) -> None:
        with self.archive.connection() as con:
            con.execute(
                "INSERT INTO wiki_spaces(space_id, name) VALUES ('space-history', 'History')"
            )
        created_s = self.window["start_s"] + 60
        later_edit_s = self.window["end_s"] + 60
        self.archive.upsert_wiki_node(
            {
                "node_token": "node-history",
                "obj_token": "doc-history",
                "obj_type": "docx",
                "node_type": "origin",
                "title": "Historical title",
                "obj_create_time": created_s,
                "obj_edit_time": later_edit_s,
            },
            space_id="space-history",
            parent_node_token=None,
            path="Historical title",
            position=0,
            seen_at=self.window["start_ms"],
        )
        self.archive.upsert_wiki_document(
            {
                "obj_token": "doc-history",
                "obj_type": "docx",
                "title": "Historical title",
                "revision_id": 9,
                "source_edit_time": later_edit_s,
                "content_text": "FUTURE REVISION TEXT",
                "content_sha256": "future-digest",
                "status": "synced",
            }
        )

        result = extract_daily_sources(self.archive, self.mail, DAY, TIMEZONE)
        item = next(value for value in result["evidence"] if value["source_kind"] == "wiki")
        self.assertEqual(item["text"], "")
        self.assertFalse(item["metadata"]["actionable"])
        self.assertNotIn("FUTURE REVISION TEXT", json.dumps(result, ensure_ascii=False))
        self.assertTrue(any("仅使用元数据" in warning for warning in result["coverage"]["warnings"]))

    def test_archive_history_bounds_are_reported_as_snapshot_dates(self) -> None:
        self.archive.upsert_conversation({"chat_id": "bounds", "name": "Bounds"})
        with self.archive.connection() as con:
            con.execute(
                """
                INSERT INTO messages(
                    message_id, chat_id, message_type, created_at,
                    body_text, raw_json, archived_at
                ) VALUES ('bounds-message', 'bounds', 'text', ?, 'body', '{}', ?)
                """,
                (self.window["start_ms"] + 1, self.window["start_ms"] + 1),
            )
        bounds = archive_history_bounds(self.archive, self.mail, TIMEZONE)
        self.assertEqual(bounds["earliest_date"], DAY.isoformat())
        self.assertEqual(bounds["latest_date"], DAY.isoformat())
        self.assertEqual(bounds["lanes"]["chat"]["observed_records"], 1)
        self.assertEqual(bounds["basis"], "current_local_archive_snapshot")

    def test_mail_bounds_include_sent_date_when_received_date_is_next_day(self) -> None:
        mailbox_id = self._mailbox_with_folders()
        prior = calendar_day_window("2026-03-28", TIMEZONE)
        send_date = prior["end_ms"] - 60_000
        received_date = prior["end_ms"] + 60_000
        message_id, _ = self.mail.upsert_message(
            mailbox_id,
            {
                "message_id": "sent-before-midnight",
                "subject": "跨午夜发件",
                "head_from": {"mail_address": "owner@example.com"},
                "to": [{"mail_address": "recipient@example.com"}],
                "date": received_date,
                "internal_date": received_date,
                "folder_id": "SENT",
                "body_plain_text": "sent body",
            },
        )
        with self.mail.connection() as con:
            con.execute(
                "UPDATE messages SET send_date=?, received_date=? WHERE id=?",
                (send_date, received_date, message_id),
            )

        bounds = archive_history_bounds(self.archive, self.mail, TIMEZONE)
        self.assertEqual(bounds["lanes"]["mail"]["earliest_date"], "2026-03-28")
        extracted = extract_daily_sources(
            self.archive, self.mail, "2026-03-28", TIMEZONE
        )
        sent_ids = {
            item["source_id"]
            for item in extracted["evidence"]
            if item["source_kind"] == "mail" and item["direction"] == "sent"
        }
        self.assertIn("sent-before-midnight", sent_ids)

    def test_chunking_groups_threads_honors_budget_and_removes_duplicates(self) -> None:
        base = {
            "source_kind": "chat",
            "thread_key": "chat:one",
            "title": "Thread",
            "occurred_at": 1,
            "direction": "received",
            "text": "x" * 80,
            "metadata": {},
            "citation": "chat:one/message",
        }
        evidence = [
            {**base, "source_id": "one"},
            {**base, "source_id": "one"},
            {**base, "source_id": "two", "occurred_at": 2},
            {
                **base,
                "source_kind": "wiki",
                "source_id": "doc",
                "thread_key": "wiki:doc",
                "citation": "wiki:doc",
            },
        ]

        chunks = chunk_evidence(evidence, max_chars=150)
        ids = [item for chunk in chunks for item in chunk["evidence_ids"]]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {"chat:one", "chat:two", "wiki:doc"})
        self.assertEqual(sum(len(chunk["items"]) for chunk in chunks), 3)
        self.assertTrue(all(chunk["char_count"] <= 150 for chunk in chunks))
        self.assertTrue(all(len({entry["source_kind"] for entry in chunk["items"]}) == 1 for chunk in chunks))

    def _mailbox_with_folders(self) -> int:
        mailbox_id = self.mail.upsert_mailbox(
            {
                "provider": "feishu",
                "mailbox_id": "owner-mailbox",
                "primary_email_address": "owner@example.com",
                "display_name": "Owner",
            }
        )
        self.mail.replace_folders(
            mailbox_id,
            [
                {"folder_id": marker, "name": marker, "folder_type": marker.lower()}
                for marker in ("INBOX", "SENT", "DRAFT", "SCHEDULED", "SPAM", "TRASH")
            ],
            seen_at=self.window["start_ms"],
        )
        return mailbox_id


def _required_evidence_keys() -> set[str]:
    return {
        "source_kind",
        "evidence_id",
        "source_id",
        "thread_key",
        "title",
        "occurred_at",
        "direction",
        "text",
        "metadata",
        "citation",
    }


if __name__ == "__main__":
    unittest.main()
