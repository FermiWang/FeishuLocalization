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

    def current_user_open_id(self):
        return "ou_self"

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

    def message(
        self,
        message_id,
        *,
        thread_id=None,
        content=None,
        message_type="text",
        created_at=None,
        sender_id="ou_1",
    ):
        if content is None:
            content = {"text": message_id}
        return {
            "message_id": message_id,
            "chat_id": "oc_1",
            "thread_id": thread_id,
            "msg_type": message_type,
            "create_time": str(created_at or self.now_ms),
            "update_time": str(created_at or self.now_ms),
            "sender": {"id": sender_id, "sender_type": "user"},
            "body": {"content": json.dumps(content)},
        }

    def open_resource(self, message_id, file_key, resource_type):
        self.calls.append(("resource", message_id, file_key, resource_type))
        return FakeResponse(b"offline attachment")


class TimeoutClient(FakeClient):
    def open_resource(self, message_id, file_key, resource_type):
        self.calls.append(("resource", message_id, file_key, resource_type))
        return TimeoutResponse(b"offline attachment")


class OwnImageClient(FakeClient):
    def iter_message_pages(self, container_type, container_id, **kwargs):
        self.calls.append((container_type, container_id, kwargs))
        if container_type == "chat":
            yield {
                "items": [
                    self.message(
                        "om_self_image",
                        content={"image_key": "img_self"},
                        message_type="image",
                        sender_id="ou_self",
                    )
                ],
                "has_more": False,
            }
        else:
            yield {"items": [], "has_more": False}


class DiscoverClient(FakeClient):
    def iter_chat_pages(self):
        yield {
            "items": [{"chat_id": "oc_group", "name": "内部群", "chat_mode": "group"}],
            "has_more": False,
        }

    def iter_message_search_pages(self, *, chat_type, page_token=None):
        self.calls.append(("search", chat_type, page_token))
        yield {
            "items": [
                {
                    "meta_data": {
                        "chat_id": "oc_p2p",
                        "is_p2p_chat": True,
                    }
                },
                {
                    "meta_data": {
                        "chat_id": "oc_p2p",
                        "is_p2p_chat": True,
                    }
                },
            ],
            "has_more": False,
        }

    def iter_member_pages(self, chat_id):
        self.calls.append(("members", chat_id))
        yield {
            "items": [
                {"member_id": "ou_self", "name": "我", "member_id_type": "open_id"},
                {"member_id": "ou_other", "name": "对方用户", "member_id_type": "open_id"},
            ],
            "has_more": False,
        }


class SyncTests(unittest.TestCase):
    def test_discover_combines_groups_and_p2p_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            syncer = ArchiveSyncer(
                database,
                DiscoverClient(),
                paths,
                max_attachment_bytes=1024 * 1024,
            )

            chats = syncer.discover()

            self.assertEqual({item["chat_id"] for item in chats}, {"oc_group", "oc_p2p"})
            conversations = {item["chat_id"]: item for item in database.list_conversations()}
            self.assertEqual(conversations["oc_p2p"]["name"], "对方用户")
            self.assertEqual(conversations["oc_p2p"]["chat_mode"], "p2p")

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

    def test_all_history_omits_time_filter(self) -> None:
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

            counts = syncer.sync(["oc_1"])

            chat_call = next(call for call in client.calls if call[0] == "chat")
            self.assertIsNone(chat_call[2]["start_time"])
            self.assertIsNone(chat_call[2]["end_time"])
            self.assertEqual(counts.messages_written, 3)
            latest = database.status()["latest_sync"]
            self.assertIsNone(latest["requested_days"])

    def test_own_images_are_recorded_and_downloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            client = OwnImageClient()
            syncer = ArchiveSyncer(
                database,
                client,
                paths,
                max_attachment_bytes=1024 * 1024,
            )

            counts = syncer.sync(["oc_1"], days=30)

            self.assertEqual(counts.attachments_downloaded, 1)
            image = database.resources_for_messages(["om_self_image"])["om_self_image"][0]
            self.assertEqual(image["resource_type"], "image")
            self.assertEqual(image["status"], "downloaded")
            self.assertTrue((paths.root / image["local_path"]).is_file())
            self.assertIn(
                ("resource", "om_self_image", "img_self", "image"),
                client.calls,
            )

    def test_sent_attachments_are_pruned_and_not_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            client = FakeClient()
            own_message = client.message(
                "om_self_file",
                content={"file_key": "file_self", "file_name": "mine.txt"},
                message_type="file",
                sender_id="ou_self",
            )
            from feishu_archive.parser import normalize_message

            database.upsert_message(normalize_message(own_message, "oc_1"))
            attachment_id = database.ensure_attachment(
                "om_self_file", "file_self", "file", "mine.txt"
            )
            local_path = Path("attachments") / "oc_1" / "mine.txt"
            target = paths.root / local_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"mine")
            database.update_attachment(
                attachment_id,
                status="downloaded",
                byte_size=4,
                local_path=str(local_path),
            )
            own_image = client.message(
                "om_self_image",
                content={"image_key": "img_self"},
                message_type="image",
                sender_id="ou_self",
            )
            database.upsert_message(normalize_message(own_image, "oc_1"))
            image_id = database.ensure_attachment(
                "om_self_image", "img_self", "image", None
            )
            image_path = Path("attachments") / "oc_1" / "mine.png"
            image_target = paths.root / image_path
            image_target.write_bytes(b"image")
            database.update_attachment(
                image_id,
                status="downloaded",
                byte_size=5,
                local_path=str(image_path),
            )
            syncer = ArchiveSyncer(
                database,
                client,
                paths,
                max_attachment_bytes=1024 * 1024,
            )

            counts = syncer.sync(["oc_1"], days=30)

            self.assertEqual(counts.attachments_pruned, 1)
            self.assertEqual(counts.attachment_bytes_pruned, 4)
            self.assertFalse(target.exists())
            self.assertNotIn(
                "om_self_file",
                database.attachments_for_messages(["om_self_file"]),
            )
            self.assertTrue(image_target.exists())
            preserved = database.resources_for_messages(["om_self_image"])["om_self_image"][0]
            self.assertEqual(preserved["resource_type"], "image")
            self.assertEqual(preserved["status"], "downloaded")


if __name__ == "__main__":
    unittest.main()
