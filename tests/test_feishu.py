import io
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

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
    def test_token_namespace_isolates_oauth_for_the_same_app_id(self) -> None:
        store = MemoryTokenStore()
        config = FeishuAppConfig(
            "cli_shared",
            "secret",
            "http://127.0.0.1:8766/oauth/callback",
        )
        default_client = FeishuClient(config, store)
        mail_client = FeishuClient(config, store, token_namespace="mail")

        default_client._save_token_result(
            {
                "access_token": "chat-access",
                "expires_in": 3600,
                "refresh_token": "chat-refresh",
                "scope": "im:message:readonly offline_access",
            }
        )
        mail_client._save_token_result(
            {
                "access_token": "mail-access",
                "expires_in": 3600,
                "refresh_token": "mail-refresh",
                "scope": "mail:user_mailbox:readonly offline_access",
            }
        )

        self.assertEqual(store.get("cli_shared:access_token"), "chat-access")
        self.assertEqual(store.get("cli_shared:refresh_token"), "chat-refresh")
        self.assertEqual(store.get("cli_shared:mail:access_token"), "mail-access")
        self.assertEqual(store.get("cli_shared:mail:refresh_token"), "mail-refresh")
        self.assertEqual(default_client.authorized_scopes(), {"im:message:readonly", "offline_access"})
        self.assertEqual(
            mail_client.authorized_scopes(),
            {"mail:user_mailbox:readonly", "offline_access"},
        )

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

    def test_message_search_uses_empty_query_and_p2p_filter(self) -> None:
        client = RecordingClient()
        pages = list(client.iter_message_search_pages(chat_type="p2p"))
        self.assertEqual(len(pages), 2)
        method, path, kwargs = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/im/v1/messages/search")
        self.assertEqual(kwargs["params"]["page_size"], 50)
        self.assertNotIn("query", kwargs["payload"])
        self.assertEqual(kwargs["payload"]["filter"]["chat_type"], "p2p")

    def test_message_search_can_resume_from_page_token(self) -> None:
        client = RecordingClient()
        pages = list(client.iter_message_search_pages(chat_type="p2p", page_token="next"))
        self.assertEqual(len(pages), 1)
        self.assertEqual(client.calls[0][2]["params"]["page_token"], "next")

    def test_authorization_url_contains_offline_access_and_state(self) -> None:
        client = RecordingClient()
        url = client.authorization_url("safe-state")
        self.assertIn("offline_access", url)
        self.assertIn("safe-state", url)
        self.assertIn("client_id=cli_test", url)
        self.assertIn("wiki%3Awiki%3Areadonly", url)
        self.assertIn("docx%3Adocument%3Areadonly", url)

    def test_wiki_and_docx_paging_use_documented_paths(self) -> None:
        client = RecordingClient()
        pages = list(client.iter_wiki_node_pages("spc_1", parent_node_token="wik_parent"))
        self.assertEqual(len(pages), 2)
        method, path, kwargs = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/wiki/v2/spaces/spc_1/nodes")
        self.assertEqual(kwargs["params"]["parent_node_token"], "wik_parent")
        self.assertEqual(kwargs["params"]["page_size"], 50)

        client.calls.clear()
        pages = list(client.iter_docx_block_pages("doc_1"))
        self.assertEqual(len(pages), 2)
        self.assertEqual(client.calls[0][1], "/docx/v1/documents/doc_1/blocks")
        self.assertEqual(client.calls[0][2]["params"]["page_size"], 500)

    def test_oauth_result_persists_granted_scopes(self) -> None:
        client = RecordingClient()
        result = client._save_token_result(
            {
                "access_token": "token",
                "expires_in": 3600,
                "refresh_token": "refresh",
                "refresh_token_expires_in": 7200,
                "scope": "wiki:wiki:readonly docx:document:readonly",
            }
        )
        self.assertIn("wiki:wiki:readonly", result.scope)
        self.assertEqual(
            client.authorized_scopes(),
            {"wiki:wiki:readonly", "docx:document:readonly"},
        )

    def test_resource_download_uses_user_access_token(self) -> None:
        client = RecordingClient()
        client.token_store.set(client.account("access_token"), "user-token")
        client.token_store.set(client.account("access_expires_at"), str(int(time.time()) + 3600))

        with patch("feishu_archive.feishu.urllib.request.urlopen", return_value=object()) as opener:
            client.open_resource("om_1", "img_1", "image")

        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer user-token")

    def test_json_request_retries_timeout(self) -> None:
        client = RecordingClient()
        client.token_store.set(client.account("access_token"), "user-token")
        client.token_store.set(client.account("access_expires_at"), str(int(time.time()) + 3600))

        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"code":0,"data":{"ok":true}}'
        with patch(
            "feishu_archive.feishu.urllib.request.urlopen",
            side_effect=[TimeoutError("slow"), response],
        ) as opener, patch("feishu_archive.feishu.time.sleep"):
            result = FeishuClient._json_request(client, "GET", "/test")

        self.assertEqual(result["data"]["ok"], True)
        self.assertEqual(opener.call_count, 2)

    def test_json_request_retries_http_400_rate_limit(self) -> None:
        client = RecordingClient()
        client.token_store.set(client.account("access_token"), "user-token")
        client.token_store.set(client.account("access_expires_at"), str(int(time.time()) + 3600))

        rate_limit = urllib.error.HTTPError(
            "https://open.feishu.cn/open-apis/test",
            400,
            "Bad Request",
            {"Retry-After": "0"},
            io.BytesIO(b'{"code":99991400,"msg":"too many request"}'),
        )
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"code":0,"data":{"ok":true}}'
        with patch(
            "feishu_archive.feishu.urllib.request.urlopen",
            side_effect=[rate_limit, response],
        ) as opener, patch("feishu_archive.feishu.time.sleep") as sleeper:
            result = FeishuClient._json_request(client, "GET", "/test")

        self.assertEqual(result["data"]["ok"], True)
        self.assertEqual(opener.call_count, 2)
        sleeper.assert_called_once_with(0.0)


if __name__ == "__main__":
    unittest.main()
