import json
import tempfile
import unittest
from pathlib import Path

from feishu_archive.database import ArchiveDatabase


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = ArchiveDatabase(Path(self.temp.name) / "archive.sqlite3")
        self.database.initialize()
        self.database.upsert_conversation(
            {"chat_id": "oc_internal", "name": "内部项目群", "chat_mode": "group"}
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def message(self, message_id: str, body: str, sender: str = "王小明", created: int = 1720000000000):
        return {
            "message_id": message_id,
            "chat_id": "oc_internal",
            "message_type": "text",
            "sender_id": f"id_{sender}",
            "sender_type": "user",
            "sender_name": sender,
            "created_at": created,
            "updated_at": created,
            "body_text": body,
            "raw_json": json.dumps({"body": body}, ensure_ascii=False),
        }

    def test_upsert_search_filter_and_counts(self) -> None:
        self.assertTrue(self.database.upsert_message(self.message("om_1", "可靠的本地离线档案")))
        self.assertFalse(self.database.upsert_message(self.message("om_1", "可靠的本地离线档案已编辑")))
        self.database.upsert_message(self.message("om_2", "第二条消息", sender="李雷", created=1720000100000))

        matches = self.database.query_messages(query="离线档案")
        self.assertEqual([item["message_id"] for item in matches], ["om_1"])
        self.assertIn("已编辑", matches[0]["body_text"])
        by_sender = self.database.query_messages(chat_id="oc_internal", sender="李雷")
        self.assertEqual([item["message_id"] for item in by_sender], ["om_2"])
        newest_first = self.database.query_messages(chat_id="oc_internal")
        self.assertEqual([item["message_id"] for item in newest_first], ["om_2", "om_1"])
        oldest_first = self.database.query_messages(chat_id="oc_internal", newest_first=False)
        self.assertEqual([item["message_id"] for item in oldest_first], ["om_1", "om_2"])
        conversations = self.database.list_conversations()
        self.assertEqual(conversations[0]["name"], "内部项目群")
        self.assertEqual(conversations[0]["message_count"], 2)
        self.assertEqual(self.database.integrity_check(), "ok")

    def test_attachment_grouping(self) -> None:
        self.database.upsert_message(self.message("om_1", "包含附件"))
        attachment_id = self.database.ensure_attachment("om_1", "file_1", "file", "说明.txt")
        self.database.update_attachment(attachment_id, status="downloaded", byte_size=12)
        image_id = self.database.ensure_attachment("om_1", "image_1", "image", None)
        self.database.update_attachment(image_id, status="downloaded", byte_size=20)

        grouped = self.database.resources_for_messages(["om_1"])
        self.assertEqual(
            {resource["resource_type"] for resource in grouped["om_1"]},
            {"file", "image"},
        )
        message = self.database.query_messages(chat_id="oc_internal")[0]
        self.assertEqual(message["image_count"], 1)
        self.assertEqual(message["attachment_count"], 1)
        status = self.database.status()
        self.assertEqual(status["images"], 1)
        self.assertEqual(status["image_bytes"], 20)
        self.assertEqual(status["attachments"], 1)
        self.assertEqual(status["attachment_bytes"], 12)
        self.assertEqual(status["resource_bytes"], 32)
        self.assertEqual(self.database.attachment_bytes(), 32)


if __name__ == "__main__":
    unittest.main()
