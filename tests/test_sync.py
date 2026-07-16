import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.sync import ArchiveSyncer


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload)), "Content-Type": "text/plain"}


class TimeoutResponse(FakeResponse):
    def read(self, size=-1):
        raise TimeoutError("test timeout")


class FakeClient:
    def __init__(self) -> None:
        self.now_ms = int(time.time() * 1000)
        self.calls = []

    def iter_member_pages(self, chat_id):
        self.calls.append(("members", chat_id))
        yield {"items": [{"member_id": "ou_1", "name": "测试用户", "member_id_type": "open_id"}], "has_more": False}

    def iter_message_pages(self, container_type, container_id, **kwargs):
        self.calls.append((container_type, container_id, kwargs))
        if container_type == "chat":
            yield {
                "items": [self.message("om_root", thread_id="omt_1")],
                "has_more": False,
            }
        else:
            yield {
                "items": [
                    self.message("om_root", thread_id="omt_1"),
                    self.message(
                        "om_reply",
                        thread_id="omt_1",
                        content={"file_key": "file_1", "file_name": "reply.txt"},
                        message_type="file",
                    ),
                    self.message("om_old", created_at=self.now_ms - 90 * 86400000),
                ],
                "has_more": False,
            }

    def message(self, message_id, *, thread_id=None, content=None, message_type="text", created_at=None):
        if content is None:
            content = {"text": message_id}
        return {
            "message_id": message_id,
            "chat_id": "oc_1",
            "thread_id": thread_id,
            "msg_type": message_type,
            "create_time": str(created_at or self.now_ms),
            "update_time": str(created_at or self.now_ms),
            "sender": {"id": "ou_1", "sender_type": "user"},
            "body": {"content": json.dumps(content)},
        }

    def open_resource(self, message_id, file_key, resource_type):
        self.calls.append(("resource", message_id, file_key, resource_type))
        return FakeResponse(b"offline attachment")


class TimeoutClient(FakeClient):
    def open_resource(self, message_id, file_key, resource_type):
        self.calls.append(("resource", message_id, file_key, resource_type))
        return TimeoutResponse(b"offline attachment")


class SyncTests(unittest.TestCase):
    def test_chat_thread_and_attachment_vertical_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            client = FakeClient()
            syncer = ArchiveSyncer(
                database,
                client,
                paths,
                max_attachment_bytes=1024 * 1024,
            )
            counts = syncer.sync(["oc_1"], days=30)

            self.assertEqual(counts.messages_seen, 3)
            self.assertEqual(counts.messages_written, 2)
            self.assertEqual(counts.attachments_downloaded, 1)
            messages = database.query_messages(chat_id="oc_1")
            self.assertEqual({item["message_id"] for item in messages}, {"om_root", "om_reply"})
            self.assertEqual({item["sender_name"] for item in messages}, {"测试用户"})
            attachments = database.attachments_for_messages(["om_reply"])["om_reply"]
            self.assertEqual(attachments[0]["status"], "downloaded")
            self.assertTrue((paths.root / attachments[0]["local_path"]).is_file())
            self.assertEqual(client.calls[0][0], "members")
            self.assertEqual(client.calls[1][0], "chat")
            self.assertEqual(client.calls[2][0], "thread")
            self.assertEqual(client.calls[2][2], {})

    def test_attachment_timeout_is_recorded_without_aborting_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            syncer = ArchiveSyncer(
                database,
                TimeoutClient(),
                paths,
                max_attachment_bytes=1024 * 1024,
            )

            counts = syncer.sync(["oc_1"], days=30)

            self.assertEqual(counts.messages_written, 2)
            attachment = database.attachments_for_messages(["om_reply"])["om_reply"][0]
            self.assertEqual(attachment["status"], "error")
            self.assertIn("test timeout", attachment["error"])
            latest = database.status()["latest_sync"]
            self.assertEqual(latest["status"], "partial")
            self.assertIn("1 个附件下载失败", latest["error"])


if __name__ == "__main__":
    unittest.main()
