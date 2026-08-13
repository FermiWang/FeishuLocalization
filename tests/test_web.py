import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.demo import seed_demo
from feishu_archive.mail_database import MailDatabase
from feishu_archive.insights_database import InsightsDatabase
from feishu_archive.reader_auth import (
    ReaderSessionManager,
    SESSION_COOKIE,
    disable_permanent_unlock,
    enable_permanent_unlock,
)
from feishu_archive.web import ArchiveHTTPServer, is_loopback_host, serve


class WebTests(unittest.TestCase):
    def test_insights_api_reuses_mail_unlock_and_returns_active_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            mail_database = MailDatabase(paths.mail_database)
            mail_database.initialize()
            insights = InsightsDatabase(paths.insights_database)
            insights.initialize()
            run = insights.start_run(
                report_date="2026-08-12",
                timezone="Europe/Amsterdam",
                run_key="web-report",
            )
            insights.finish_run(
                run,
                {
                    "status": "success",
                    "report": {
                        "report_date": "2026-08-12",
                        "yesterday_summary": [],
                        "today_plan": [],
                        "commercial_opportunities": [],
                    },
                    "activate": True,
                },
            )
            sessions = ReaderSessionManager(
                paths.reader_secret,
                ttl_seconds=60,
                permanent_unlock_path=paths.mail_reader_permanent_unlock,
            )
            server = ArchiveHTTPServer(
                ("127.0.0.1", 0),
                database,
                paths,
                mail_database=mail_database,
                mail_session_manager=sessions,
                insights_database=insights,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(f"{base}/api/insights/daily?date=2026-08-12", timeout=2)
                self.assertEqual(context.exception.code, 401)
                enable_permanent_unlock(paths.mail_reader_permanent_unlock)
                with urllib.request.urlopen(
                    f"{base}/api/insights/daily?date=2026-08-12", timeout=2
                ) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["item"]["report"]["report_date"], "2026-08-12")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_loopback_policy(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("0.0.0.0"))

    def test_serve_rejects_localhost_resolving_outside_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            external_resolution = [(2, 1, 6, "", ("192.0.2.10", 0))]
            with mock.patch(
                "feishu_archive.web.socket.getaddrinfo",
                return_value=external_resolution,
            ), self.assertRaises(ValueError):
                serve(database, paths, "localhost", 0)
            with self.assertRaises(ValueError):
                serve(database, paths, "0.0.0.0", 0)

    def test_permanent_mail_access_is_dynamic_but_host_and_origin_stay_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            mail_database = MailDatabase(paths.mail_database)
            mail_database.initialize()
            sessions = ReaderSessionManager(
                paths.reader_secret,
                ttl_seconds=60,
                permanent_unlock_path=paths.mail_reader_permanent_unlock,
            )
            token = sessions.create_session(sessions.unlock_secret)
            self.assertIsNotNone(token)
            old_cookie = f"{SESSION_COOKIE}={token}"

            controller = mock.MagicMock()
            controller.request_manual_sync.return_value = True
            server = ArchiveHTTPServer(
                ("127.0.0.1", 0),
                database,
                paths,
                mail_database=mail_database,
                mail_sync_controller=controller,
                mail_session_manager=sessions,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = int(server.server_address[1])
                base = f"http://127.0.0.1:{port}"
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(f"{base}/api/mail/status", timeout=2)
                self.assertEqual(context.exception.code, 401)

                enable_permanent_unlock(paths.mail_reader_permanent_unlock)
                with urllib.request.urlopen(f"{base}/api/mail/status", timeout=2) as response:
                    self.assertEqual(response.status, 200)

                forged_host = urllib.request.Request(
                    f"{base}/api/mail/status",
                    headers={"Host": f"attacker.example:{port}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(forged_host, timeout=2)
                self.assertEqual(context.exception.code, 421)

                wrong_port = urllib.request.Request(
                    f"{base}/api/mail/status",
                    headers={"Host": f"127.0.0.1:{port + 1}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(wrong_port, timeout=2)
                self.assertEqual(context.exception.code, 421)

                missing_action = urllib.request.Request(
                    f"{base}/api/mail/sync",
                    data=b"",
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(missing_action, timeout=2)
                self.assertEqual(context.exception.code, 403)

                evil_origin = urllib.request.Request(
                    f"{base}/api/mail/sync",
                    data=b"",
                    method="POST",
                    headers={
                        "Origin": f"http://attacker.example:{port}",
                        "X-Feishu-Archive-Action": "mail-sync",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(evil_origin, timeout=2)
                self.assertEqual(context.exception.code, 403)

                https_origin = urllib.request.Request(
                    f"{base}/api/mail/sync",
                    data=b"",
                    method="POST",
                    headers={
                        "Origin": f"https://127.0.0.1:{port}",
                        "X-Feishu-Archive-Action": "mail-sync",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(https_origin, timeout=2)
                self.assertEqual(context.exception.code, 403)
                controller.request_manual_sync.assert_not_called()

                disable_permanent_unlock(paths.mail_reader_permanent_unlock)
                relocked = urllib.request.Request(
                    f"{base}/api/mail/status",
                    headers={"Cookie": old_cookie},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(relocked, timeout=2)
                self.assertEqual(context.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_unavailable_mail_lane_returns_503_while_archive_api_stays_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = ArchivePaths(Path(temp))
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            server = ArchiveHTTPServer(
                ("127.0.0.1", 0),
                database,
                paths,
                mail_unavailable_reason="mail database failed preflight",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/api/status", timeout=2) as response:
                    self.assertEqual(response.status, 200)

                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(f"{base}/api/mail/status", timeout=2)
                self.assertEqual(context.exception.code, 503)
                self.assertIn("聊天与知识库", json.loads(context.exception.read())["error"])

                request = urllib.request.Request(
                    f"{base}/api/mail/session",
                    data=b'{}',
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(context.exception.code, 503)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

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
            wiki_file_asset_id = database.ensure_wiki_asset(
                "doc_1", "asset_pdf", "file", filename="证据清单.pdf"
            )
            wiki_file_payload = b"%PDF-1.4\n% offline test"
            wiki_file_path = paths.knowledge_assets / "evidence.pdf"
            wiki_file_path.write_bytes(wiki_file_payload)
            database.update_wiki_asset(
                wiki_file_asset_id,
                status="downloaded",
                mime_type="application/pdf",
                byte_size=len(wiki_file_payload),
                local_path=str(wiki_file_path),
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
                    self.assertIn('id="wiki-node-view"', body)
                    self.assertIn('id="wiki-back"', body)
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertIn("frame-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
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
                    self.assertIn("showWikiNodeList", script)
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
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/preview/{wiki_file_asset_id}", timeout=2
                ) as response:
                    preview = response.read().decode()
                    self.assertIn("证据清单.pdf", preview)
                    self.assertIn("asset-preview-frame", preview)
                    self.assertIn(f"/api/wiki/assets/{wiki_file_asset_id}", preview)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/assets/{wiki_file_asset_id}", timeout=2
                ) as response:
                    self.assertEqual(response.headers["Content-Type"], "application/pdf")
                    self.assertIsNone(response.headers["Content-Disposition"])
                    self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.read(), wiki_file_payload)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/wiki/assets/{wiki_file_asset_id}?download=1",
                    timeout=2,
                ) as response:
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.read(), wiki_file_payload)
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

    def test_mail_api_requires_session_and_only_downloads_contained_blobs(self) -> None:
        class MailSyncController:
            def __init__(self) -> None:
                self.requests = 0

            def request_manual_sync(self) -> bool:
                self.requests += 1
                return True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "archive"
            paths = ArchivePaths(root)
            paths.ensure()
            database = ArchiveDatabase(paths.database)
            database.initialize()
            mail_database = MailDatabase(paths.mail_database)
            mail_database.initialize()
            mailbox_id = mail_database.upsert_mailbox(
                {
                    "provider": "feishu",
                    "mailbox_id": "owner_1",
                    "primary_email_address": "owner@example.com",
                    "display_name": "Owner",
                }
            )
            folder_id = mail_database.replace_folders(
                mailbox_id,
                [{"folder_id": "INBOX", "name": "收件箱", "folder_type": "inbox"}],
            )[0]
            message_id, _ = mail_database.upsert_message(
                mailbox_id,
                {
                    "message_id": "mail_1",
                    "subject": "本地邮件档案",
                    "head_from": {"name": "Alice", "mail_address": "alice@example.com"},
                    "to": [{"name": "Owner", "mail_address": "owner@example.com"}],
                    "date": 1_720_000_000_000,
                    "folder_id": "INBOX",
                    "body_plain_text": "敏感内容只在解锁后显示",
                    "attachments": [
                        {
                            "id": "att_ok",
                            "filename": "证据.txt",
                            "content_type": "text/plain",
                        },
                        {
                            "id": "att_escape",
                            "filename": "escape.txt",
                            "content_type": "text/plain",
                        },
                        {
                            "id": "att_quarantine",
                            "filename": "unsafe.html",
                            "content_type": "text/html",
                        },
                    ],
                },
            )
            attachments = mail_database.get_message(message_id)["attachments"]
            attachment_id = int(attachments[0]["id"])
            escape_attachment_id = int(attachments[1]["id"])
            quarantine_attachment_id = int(attachments[2]["id"])

            attachment_payload = b"mail attachment payload"
            digest = hashlib.sha256(attachment_payload).hexdigest()
            relative_path = Path("mail") / "blobs" / digest[:2] / digest
            target = paths.root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(attachment_payload)
            blob_id = mail_database.upsert_blob(
                digest,
                len(attachment_payload),
                str(relative_path),
                "text/plain",
            )
            mail_database.link_attachment_blob(
                attachment_id,
                blob_id,
                status="available",
            )

            outside = Path(temp) / "outside.txt"
            outside.write_bytes(b"must not be served")
            escape_relative = Path("mail") / "blobs" / "escape-link"
            escape_target = paths.root / escape_relative
            escape_target.symlink_to(outside)
            escape_blob_id = mail_database.upsert_blob(
                "0" * 64,
                outside.stat().st_size,
                str(escape_relative),
                "text/plain",
            )
            mail_database.link_attachment_blob(
                escape_attachment_id,
                escape_blob_id,
                status="available",
            )

            quarantine_payload = b"<script>unsafe active content</script>"
            quarantine_digest = hashlib.sha256(quarantine_payload).hexdigest()
            quarantine_relative = (
                Path("mail") / "blobs" / quarantine_digest[:2] / quarantine_digest
            )
            quarantine_target = paths.root / quarantine_relative
            quarantine_target.parent.mkdir(parents=True, exist_ok=True)
            quarantine_target.write_bytes(quarantine_payload)
            quarantine_blob_id = mail_database.upsert_blob(
                quarantine_digest,
                len(quarantine_payload),
                str(quarantine_relative),
                "text/html",
            )
            mail_database.link_attachment_blob(
                quarantine_attachment_id,
                quarantine_blob_id,
                status="quarantined",
            )

            sessions = ReaderSessionManager(paths.reader_secret, ttl_seconds=60)
            controller = MailSyncController()
            server = ArchiveHTTPServer(
                ("127.0.0.1", 0),
                database,
                paths,
                mail_database=mail_database,
                mail_sync_controller=controller,
                mail_session_manager=sessions,
                mail_sync_schedule={"enabled": True, "description": "每天 04:00 自动同步邮箱"},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(f"{base}/api/mail/status", timeout=2)
                self.assertEqual(context.exception.code, 401)
                self.assertIn("已锁定", json.loads(context.exception.read())["error"])

                invalid_payload = json.dumps({"secret": "not-the-secret"}).encode()
                invalid_request = urllib.request.Request(
                    f"{base}/api/mail/session",
                    data=invalid_payload,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(invalid_request, timeout=2)
                self.assertEqual(context.exception.code, 401)

                session_payload = json.dumps({"unlock_token": sessions.unlock_secret}).encode()
                session_request = urllib.request.Request(
                    f"{base}/api/mail/session",
                    data=session_payload,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(session_request, timeout=2) as response:
                    cookie = response.headers["Set-Cookie"]
                    self.assertIn("HttpOnly", cookie)
                    self.assertIn("SameSite=Strict", cookie)
                    self.assertEqual(json.loads(response.read())["status"], "unlocked")
                cookie_header = cookie.split(";", 1)[0]

                def get_json(path: str) -> dict:
                    request = urllib.request.Request(
                        f"{base}{path}",
                        headers={"Cookie": cookie_header},
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        return json.loads(response.read())

                status = get_json("/api/mail/status")
                self.assertEqual(status["mailbox"]["address"], "owner@example.com")
                self.assertEqual(status["messages"], 1)
                self.assertIn("每天 04:00", status["schedule"]["description"])

                folders = get_json("/api/mail/folders")
                self.assertEqual(folders["items"][0]["provider_folder_id"], "INBOX")
                messages = get_json(
                    "/api/mail/messages?"
                    + urllib.parse.urlencode(
                        {
                            "q": "本地邮件",
                            "folder_id": folder_id,
                            "page": 1,
                            "page_size": 1,
                        }
                    )
                )
                self.assertEqual(messages["items"][0]["subject"], "本地邮件档案")
                self.assertNotIn("body_plain_text", messages["items"][0])
                self.assertEqual(
                    messages["items"][0]["snippet"],
                    "敏感内容只在解锁后显示",
                )
                self.assertEqual(messages["page"], 1)
                self.assertFalse(messages["has_more"])

                with mock.patch("builtins.print") as print_mock:
                    get_json("/api/mail/messages?q=private-search-term")
                rendered_logs = " ".join(str(call) for call in print_mock.call_args_list)
                self.assertNotIn("private-search-term", rendered_logs)
                self.assertIn("/api/mail/messages", rendered_logs)

                detail = get_json(f"/api/mail/messages/{message_id}")["item"]
                self.assertEqual(detail["body_plain_text"], "敏感内容只在解锁后显示")
                self.assertEqual(detail["recipients"][0]["role"], "from")

                attachment_request = urllib.request.Request(
                    f"{base}/api/mail/attachments/{attachment_id}",
                    headers={"Cookie": cookie_header},
                )
                with urllib.request.urlopen(attachment_request, timeout=2) as response:
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.read(), attachment_payload)

                quarantine_request = urllib.request.Request(
                    f"{base}/api/mail/attachments/{quarantine_attachment_id}",
                    headers={"Cookie": cookie_header},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(quarantine_request, timeout=2)
                self.assertEqual(context.exception.code, 409)
                self.assertIn("风险格式", json.loads(context.exception.read())["error"])

                confirmed_quarantine_request = urllib.request.Request(
                    f"{base}/api/mail/attachments/{quarantine_attachment_id}?confirm=1",
                    headers={"Cookie": cookie_header},
                )
                with urllib.request.urlopen(confirmed_quarantine_request, timeout=2) as response:
                    self.assertEqual(
                        response.headers["X-Feishu-Archive-Warning"],
                        "quarantined-attachment",
                    )
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    self.assertEqual(response.read(), quarantine_payload)

                escape_request = urllib.request.Request(
                    f"{base}/api/mail/attachments/{escape_attachment_id}",
                    headers={"Cookie": cookie_header},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(escape_request, timeout=2)
                self.assertEqual(context.exception.code, 404)

                sync_request = urllib.request.Request(
                    f"{base}/api/mail/sync",
                    method="POST",
                    headers={
                        "Cookie": cookie_header,
                        "X-Feishu-Archive-Action": "mail-sync",
                    },
                )
                with urllib.request.urlopen(sync_request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(controller.requests, 1)

                unsafe_sync = urllib.request.Request(
                    f"{base}/api/mail/sync",
                    method="POST",
                    headers={"Cookie": cookie_header},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(unsafe_sync, timeout=2)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
