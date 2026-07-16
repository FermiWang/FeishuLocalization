import time
import unittest
from unittest.mock import patch

from feishu_archive.config import FeishuAppConfig
from feishu_archive.feishu import FeishuClient
from feishu_archive.keychain import MemoryTokenStore


class RecordingClient(FeishuClient):
    def __init__(self) -> None:
        super().__init__(
            FeishuAppConfig("cli_test", "secret", "http://127.0.0.1:8766/oauth/callback"),
            MemoryTokenStore(),
        )
        self.calls = []

    def _json_request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        params = kwargs["params"]
        if params.get("page_token"):
            return {"code": 0, "data": {"items": [{"message_id": "om_2"}], "has_more": False}}
        return {
            "code": 0,
            "data": {"items": [{"message_id": "om_1"}], "has_more": True, "page_token": "next"},
        }


class FeishuClientTests(unittest.TestCase):
    def test_message_paging_uses_documented_limits(self) -> None:
        client = RecordingClient()
        pages = list(client.iter_message_pages("chat", "oc_1", start_time=100, end_time=200))
        self.assertEqual(len(pages), 2)
        first = client.calls[0][2]["params"]
        self.assertEqual(first["page_size"], 50)
        self.assertEqual(first["container_id_type"], "chat")
        self.assertEqual(first["start_time"], 100)
        self.assertEqual(client.calls[1][2]["params"]["page_token"], "next")

    def test_thread_rejects_server_side_time_window(self) -> None:
        client = RecordingClient()
        with self.assertRaisesRegex(ValueError, "thread"):
            list(client.iter_message_pages("thread", "omt_1", start_time=100))

    def test_member_paging_uses_open_id_and_limit(self) -> None:
        client = RecordingClient()
        pages = list(client.iter_member_pages("oc_1"))
        self.assertEqual(len(pages), 2)
        method, path, kwargs = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/im/v1/chats/oc_1/members")
        self.assertEqual(kwargs["params"]["member_id_type"], "open_id")
        self.assertEqual(kwargs["params"]["page_size"], 100)

    def test_authorization_url_contains_offline_access_and_state(self) -> None:
        client = RecordingClient()
        url = client.authorization_url("safe-state")
        self.assertIn("offline_access", url)
        self.assertIn("safe-state", url)
        self.assertIn("client_id=cli_test", url)

    def test_resource_download_uses_user_access_token(self) -> None:
        client = RecordingClient()
        client.token_store.set(client.account("access_token"), "user-token")
        client.token_store.set(client.account("access_expires_at"), str(int(time.time()) + 3600))

        with patch("feishu_archive.feishu.urllib.request.urlopen", return_value=object()) as opener:
            client.open_resource("om_1", "img_1", "image")

        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer user-token")


if __name__ == "__main__":
    unittest.main()
