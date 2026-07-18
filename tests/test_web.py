import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.demo import seed_demo
from feishu_archive.web import ArchiveHTTPServer, is_loopback_host


class WebTests(unittest.TestCase):
    def test_loopback_policy(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("0.0.0.0"))

    def test_reader_serves_static_and_local_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            seed_demo(database, paths)
            image_id = database.ensure_attachment(
                "demo_msg_5", "demo_image_key", "image", None
            )
            image_path = Path("attachments") / "demo_internal" / "demo-image.png"
            image_payload = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            )
            (paths.root / image_path).parent.mkdir(parents=True, exist_ok=True)
            (paths.root / image_path).write_bytes(image_payload)
            database.update_attachment(
                image_id,
                status="downloaded",
                mime_type="image/png",
                byte_size=len(image_payload),
                local_path=str(image_path),
            )
            now = int(time.time() * 1000)
            database.upsert_wiki_space(
                {"space_id": "spc_1", "name": "本地知识库"}, seen_at=now
            )
            database.upsert_wiki_node(
                {
                    "node_token": "wik_1",
                    "obj_token": "doc_1",
                    "obj_type": "docx",
                    "title": "离线文档",
                },
                space_id="spc_1",
                parent_node_token=None,
                path="离线文档",
                position=0,
                seen_at=now,
            )
            database.upsert_wiki_document(
                {
                    "obj_token": "doc_1",
                    "obj_type": "docx",
                    "title": "离线文档",
                    "content_text": "本地全文检索",
                    "rendered_html": "<p>本地全文检索</p>",
                    "status": "synced",
                }
            )
            wiki_asset_id = database.ensure_wiki_asset(
                "doc_1", "asset_1", "image", filename="wiki.png"
            )
            wiki_asset_path = paths.knowledge_assets / "wiki.png"
            wiki_asset_path.write_bytes(image_payload)
            database.update_wiki_asset(
                wiki_asset_id,
                status="downloaded",
                mime_type="image/png",
                byte_size=len(image_payload),
                local_path=str(wiki_asset_path),
            )
            starts = []
            wiki_starts = []
            server = ArchiveHTTPServer(
                ("127.0.0.1", 0),
                database,
                paths,
                sync_start=lambda: starts.append(True) or True,
                sync_schedule={"enabled": True, "description": "每天 03:30 自动同步"},
                wiki_sync_start=lambda: wiki_starts.append(True) or True,
                wiki_sync_schedule={"enabled": True, "description": "每天 03:45 自动同步知识库"},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    body = response.read().decode()
                    self.assertIn("Feishu Archive", body)
                    self.assertIn("立即同步", body)
                    self.assertIn("知识库", body)
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/messages?chat_id=demo_internal", timeout=2
                ) as response:
                    payload = json.loads(response.read())
                    image_message = next(
                        item for item in payload["items"] if item["message_id"] == "demo_msg_5"
                    )
                    self.assertEqual(image_message["image_count"], 1)
                    self.assertEqual(image_message["attachment_count"], 0)
                    self.assertEqual(image_message["resources"][0]["resource_type"], "image")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/images/{image_id}", timeout=2
                ) as response:
                    self.assertEqual(response.headers["Content-Type"], "image/png")
                    self.assertIsNone(response.headers["Content-Disposition"])
                    self.assertEqual(response.read(), image_payload)
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/attachments/{image_id}", timeout=2
                    )
                self.assertEqual(context.exception.code, 404)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/static/app.js", timeout=2
                ) as response:
                    script = response.read().decode()
                    self.assertIn("message-image", script)
                    self.assertIn("/api/images/", script)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sync/status", timeout=2
                ) as response:
                    body = response.read().decode()
                    self.assertIn("每天 03:30 自动同步", body)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/spaces", timeout=2
                ) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(payload["items"][0]["name"], "本地知识库")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/nodes?space_id=spc_1", timeout=2
                ) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(payload["items"][0]["node_token"], "wik_1")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/search?q=%E5%85%A8%E6%96%87%E6%A3%80%E7%B4%A2",
                    timeout=2,
                ) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(payload["items"][0]["title"], "离线文档")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/document?node_token=wik_1", timeout=2
                ) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(payload["status"], "synced")
                    self.assertEqual(payload["assets"][0]["id"], wiki_asset_id)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/assets/{wiki_asset_id}", timeout=2
                ) as response:
                    self.assertEqual(response.headers["Content-Type"], "image/png")
                    self.assertEqual(response.read(), image_payload)
                sync_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sync",
                    method="POST",
                    headers={"X-Feishu-Archive-Action": "sync"},
                )
                with urllib.request.urlopen(sync_request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(starts, [True])
                wiki_sync_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/wiki/sync",
                    method="POST",
                    headers={"X-Feishu-Archive-Action": "wiki-sync"},
                )
                with urllib.request.urlopen(wiki_sync_request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(wiki_starts, [True])
                unsafe_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sync",
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(unsafe_request, timeout=2)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
