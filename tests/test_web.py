import tempfile
import threading
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
            starts = []
            server = ArchiveHTTPServer(
                ("127.0.0.1", 0),
                database,
                paths,
                sync_start=lambda: starts.append(True) or True,
                sync_schedule={"enabled": True, "description": "每天 03:30 自动同步"},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    body = response.read().decode()
                    self.assertIn("Feishu Archive", body)
                    self.assertIn("立即同步", body)
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/messages?chat_id=demo_external", timeout=2
                ) as response:
                    body = response.read().decode()
                    self.assertIn("PoC 覆盖率核对说明", body)
                    self.assertIn('"attachments"', body)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/sync/status", timeout=2
                ) as response:
                    body = response.read().decode()
                    self.assertIn("每天 03:30 自动同步", body)
                sync_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sync",
                    method="POST",
                    headers={"X-Feishu-Archive-Action": "sync"},
                )
                with urllib.request.urlopen(sync_request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(starts, [True])
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
