from __future__ import annotations

import json
import ssl
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable
from unittest.mock import MagicMock, patch

from feishu_archive.config import FeishuAppConfig
from feishu_archive.feishu import FeishuAPIError, FeishuClient
from feishu_archive.feishu_mail import FeishuMailProvider, _SafeHTTPSRedirectHandler
from feishu_archive.keychain import MemoryTokenStore
from feishu_archive.mail_provider import FakeMailProvider, MailProvider


class ScriptedClient:
    def __init__(self, handler: Callable[..., dict[str, Any]]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.timeout = 30
        self.ssl_context = ssl.create_default_context()

    def _json_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        return self.handler(method, path, **kwargs)

    @staticmethod
    def _http_error(exc: Exception) -> FeishuAPIError:
        return FeishuAPIError(str(exc))


class FakeResponse:
    def __init__(
        self,
        url: str,
        body: bytes = b"payload",
        *,
        peer_ip: str = "93.184.216.34",
    ) -> None:
        self.url = url
        self.body = body
        self.headers: dict[str, str] = {}
        self.peer_ip = peer_ip
        self.closed = False

    def geturl(self) -> str:
        return self.url

    def read(self, amt: int = -1) -> bytes:
        return self.body if amt < 0 else self.body[:amt]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.request: urllib.request.Request | None = None
        self.timeout: float | None = None

    def open(self, request: urllib.request.Request, *, timeout: float) -> FakeResponse:
        self.request = request
        self.timeout = timeout
        return self.response


class FeishuMailProviderTests(unittest.TestCase):
    def test_profile_uses_real_client_user_access_token(self) -> None:
        store = MemoryTokenStore()
        client = FeishuClient(
            FeishuAppConfig("mail_app", "secret", "http://127.0.0.1:8766/oauth/callback"),
            store,
        )
        store.set(client.account("access_token"), "user-token")
        store.set(client.account("access_expires_at"), str(int(time.time()) + 3600))
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "code": 0,
                "data": {
                    "user_mailbox": {
                        "primary_email_address": "owner@example.com",
                    }
                },
            }
        ).encode()

        with patch(
            "feishu_archive.feishu.urllib.request.urlopen",
            return_value=response,
        ) as opener:
            profile = FeishuMailProvider(client).profile()

        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer user-token")
        self.assertEqual(
            request.full_url,
            "https://open.feishu.cn/open-apis/mail/v1/user_mailboxes/me/profile",
        )
        self.assertEqual(profile["primary_email_address"], "owner@example.com")
        self.assertEqual(profile["mailbox_id"], "owner@example.com")

    def test_granted_scopes_uses_the_mail_clients_current_token_namespace(self) -> None:
        store = MemoryTokenStore()
        client = FeishuClient(
            FeishuAppConfig("mail_app", "secret", "http://127.0.0.1:8766/oauth/callback"),
            store,
            token_namespace="mail",
        )
        store.set(client.account("access_token"), "mail-user-token")
        store.set(client.account("access_expires_at"), str(int(time.time()) + 3600))
        store.set(
            client.account("scope"),
            "mail:user_mailbox:readonly mail:user_mailbox.message.body:read",
        )

        self.assertEqual(
            FeishuMailProvider(client).granted_scopes(),
            {
                "mail:user_mailbox:readonly",
                "mail:user_mailbox.message.body:read",
            },
        )

    def test_lists_folders_and_escapes_mailbox_segment(self) -> None:
        client = ScriptedClient(
            lambda *args, **kwargs: {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "id": "folder-1",
                            "name": "Projects",
                            "parent_folder_id": "0",
                            "folder_type": 1,
                        }
                    ]
                },
            }
        )
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]

        folders = provider.list_folders("owner+archive@example.com")

        self.assertEqual(folders[0]["id"], "folder-1")
        method, path, kwargs = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            path,
            "/mail/v1/user_mailboxes/owner%2Barchive%40example.com/folders",
        )
        self.assertTrue(kwargs["auth"])

        malformed = FeishuMailProvider(  # type: ignore[arg-type]
            ScriptedClient(lambda *args, **kwargs: {"code": 0})
        )
        with self.assertRaisesRegex(FeishuAPIError, "data"):
            malformed.list_folders()

    def test_search_builds_documented_filter_and_extracts_message_ids(self) -> None:
        client = ScriptedClient(
            lambda *args, **kwargs: {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "meta_data": {
                                "message_biz_id": "msg-1",
                                "thread_id": "thread-1",
                            }
                        }
                    ],
                    "has_more": True,
                    "page_token": "next-page",
                    "notice": "window incomplete",
                },
            }
        )
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]
        start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)

        page = provider.search_messages(
            folder="inbox",
            start_time=start,
            end_time=end,
            subject="Report",
            has_attachment=True,
            page_token="resume",
        )

        self.assertEqual(page["message_ids"], ["msg-1"])
        self.assertEqual(page["page_token"], "next-page")
        self.assertEqual(page["notice"], "window incomplete")
        method, path, kwargs = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/mail/v1/user_mailboxes/me/search")
        self.assertEqual(kwargs["params"], {"page_size": 15, "page_token": "resume"})
        self.assertEqual(
            kwargs["payload"]["filter"],
            {
                "folder": ["inbox"],
                "subject": "Report",
                "has_attachment": True,
                "create_time": {
                    "start_time": "2026-08-01T00:00:00+00:00",
                    "end_time": "2026-08-02T00:00:00+00:00",
                },
            },
        )

    def test_iter_search_pages_rejects_repeated_cursor(self) -> None:
        client = ScriptedClient(
            lambda *args, **kwargs: {
                "code": 0,
                "data": {"items": [], "has_more": True, "page_token": "same"},
            }
        )
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]

        with self.assertRaisesRegex(FeishuAPIError, "page_token"):
            list(provider.iter_search_pages(folder="INBOX"))

        with self.assertRaisesRegex(ValueError, "同时提供"):
            provider.search_messages(folder="INBOX", start_time="2026-08-01T00:00:00Z")

    def test_search_preserves_custom_names_that_match_system_aliases(self) -> None:
        client = ScriptedClient(
            lambda *args, **kwargs: {
                "code": 0,
                "data": {"items": [], "has_more": False},
            }
        )
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]

        provider.search_messages(folder="INBOX")

        self.assertEqual(
            client.calls[0][2]["payload"]["filter"]["folder"],
            ["INBOX"],
        )

    def test_list_message_ids_uses_limits_and_mutually_exclusive_filters(self) -> None:
        client = ScriptedClient(
            lambda *args, **kwargs: {
                "code": 0,
                "data": {"items": ["msg-1", "msg-2"], "has_more": False},
            }
        )
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]

        page = provider.list_message_ids(folder_id="SENT", only_unread=True)

        self.assertEqual(page["message_ids"], ["msg-1", "msg-2"])
        self.assertEqual(
            client.calls[0][2]["params"],
            {"page_size": 20, "folder_id": "SENT", "only_unread": True},
        )
        with self.assertRaisesRegex(ValueError, "不能同时"):
            provider.list_message_ids(folder_id="INBOX", label_id="FLAGGED")
        with self.assertRaisesRegex(ValueError, "1 到 20"):
            provider.list_message_ids(page_size=21)

    def test_batch_get_chunks_by_twenty_and_preserves_requested_order(self) -> None:
        missing = "msg-5"

        def handler(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            messages = [
                {"message_id": item, "subject": item}
                for item in kwargs["payload"]["message_ids"]
                if item != missing
            ]
            return {"code": 0, "data": {"messages": messages}}

        client = ScriptedClient(handler)
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]
        ids = [f"msg-{index}" for index in range(21)]
        ids.append("msg-1")

        result = provider.batch_get_messages("owner@example.com", ids, format="full")

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(client.calls[0][2]["payload"]["message_ids"]), 20)
        self.assertEqual(len(client.calls[1][2]["payload"]["message_ids"]), 1)
        self.assertEqual(client.calls[0][2]["payload"]["format"], "full")
        self.assertEqual(result["unavailable_message_ids"], [missing])
        returned_ids = [item["message_id"] for item in result["messages"]]
        self.assertEqual(returned_ids[-1], "msg-1")
        self.assertEqual(returned_ids.count("msg-1"), 2)
        with self.assertRaisesRegex(ValueError, "format"):
            provider.batch_get_messages("me", ["msg-1"], format="raw")  # type: ignore[arg-type]

    def test_attachment_url_request_uses_repeated_ids_and_rejects_http(self) -> None:
        def handler(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            query = path.partition("?")[2]
            ids = urllib.parse.parse_qs(query)["attachment_ids"]
            return {
                "code": 0,
                "data": {
                    "download_urls": [
                        {
                            "attachment_id": item,
                            "download_url": f"https://download.example.com/{item}?signed=1",
                        }
                        for item in ids
                    ]
                },
            }

        client = ScriptedClient(handler)
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]
        urls = provider.attachment_download_urls(
            "me", "msg/with/slash", ["att-1", "att-2"]
        )

        self.assertEqual(set(urls), {"att-1", "att-2"})
        path = client.calls[0][1]
        self.assertIn("/messages/msg%2Fwith%2Fslash/attachments/download_url?", path)
        self.assertEqual(path.count("attachment_ids="), 2)
        self.assertTrue(client.calls[0][2]["auth"])

        unsafe_client = ScriptedClient(
            lambda *args, **kwargs: {
                "code": 0,
                "data": {
                    "download_urls": [
                        {
                            "attachment_id": "att-1",
                            "download_url": "http://download.example.com/file",
                        }
                    ]
                },
            }
        )
        with self.assertRaisesRegex(FeishuAPIError, "HTTPS"):
            FeishuMailProvider(unsafe_client).attachment_download_urls(  # type: ignore[arg-type]
                "me", "msg-1", ["att-1"]
            )

    def test_download_has_no_bearer_and_checks_public_dns(self) -> None:
        client = ScriptedClient(lambda *args, **kwargs: {})
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]
        url = "https://download.example.com/file?signed=secret"
        opener = FakeOpener(FakeResponse(url))
        public_dns = [
            (
                2,
                1,
                6,
                "",
                ("93.184.216.34", 443),
            )
        ]

        with patch(
            "feishu_archive.feishu_mail.socket.getaddrinfo",
            return_value=public_dns,
        ), patch(
            "feishu_archive.feishu_mail.urllib.request.build_opener",
            return_value=opener,
        ) as build_opener:
            response = provider.open_download_url(url)

        self.assertEqual(response.read(), b"payload")
        self.assertIsNotNone(opener.request)
        assert opener.request is not None
        self.assertIsNone(opener.request.get_header("Authorization"))
        self.assertEqual(opener.request.get_header("Accept"), "application/octet-stream")
        handlers = build_opener.call_args.args
        self.assertTrue(
            any(
                isinstance(handler, urllib.request.ProxyHandler)
                and handler.proxies == {}
                for handler in handlers
            )
        )

    def test_download_rejects_private_connected_peer_after_public_dns(self) -> None:
        client = ScriptedClient(lambda *args, **kwargs: {})
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]
        url = "https://download.example.com/file"
        response = FakeResponse(url, peer_ip="127.0.0.1")
        opener = FakeOpener(response)
        public_dns = [(2, 1, 6, "", ("93.184.216.34", 443))]

        with patch(
            "feishu_archive.feishu_mail.socket.getaddrinfo",
            return_value=public_dns,
        ), patch(
            "feishu_archive.feishu_mail.urllib.request.build_opener",
            return_value=opener,
        ), self.assertRaisesRegex(FeishuAPIError, "连接到本机或私网"):
            provider.open_download_url(url)

        self.assertTrue(response.closed)

    def test_download_rejects_private_initial_url_dns_and_redirect(self) -> None:
        client = ScriptedClient(lambda *args, **kwargs: {})
        provider = FeishuMailProvider(client)  # type: ignore[arg-type]

        with self.assertRaisesRegex(FeishuAPIError, "本机或私网"):
            provider.open_download_url("https://127.0.0.1/file")

        private_dns = [(2, 1, 6, "", ("10.0.0.7", 443))]
        with patch(
            "feishu_archive.feishu_mail.socket.getaddrinfo",
            return_value=private_dns,
        ), self.assertRaisesRegex(FeishuAPIError, "私网"):
            provider.open_download_url("https://download.example.com/file")

        redirect = _SafeHTTPSRedirectHandler(
            lambda target: provider._validate_download_url(target, resolve=True)
        )
        source_request = urllib.request.Request("https://download.example.com/file")
        with self.assertRaisesRegex(FeishuAPIError, "本机或私网"):
            redirect.redirect_request(
                source_request,
                None,
                302,
                "Found",
                {},
                "https://192.168.1.10/redirected",
            )

    def test_fake_provider_satisfies_protocol_and_is_copy_safe(self) -> None:
        fake = FakeMailProvider(
            messages={
                "msg-1": {
                    "message_id": "msg-1",
                    "folder_id": "INBOX",
                    "subject": "One",
                },
                "msg-2": {
                    "message_id": "msg-2",
                    "folder_id": "INBOX",
                    "subject": "Two",
                },
            },
            downloads={"https://example.com/a": b"attachment"},
        )

        self.assertIsInstance(fake, MailProvider)
        pages = list(fake.iter_search_pages(folder="INBOX", page_size=1))
        self.assertEqual([page["message_ids"] for page in pages], [["msg-1"], ["msg-2"]])
        fetched = fake.batch_get_messages("me", ["msg-1"])
        fetched["messages"][0]["subject"] = "changed"
        self.assertEqual(fake.messages["msg-1"]["subject"], "One")
        self.assertEqual(fake.open_download_url("https://example.com/a").read(), b"attachment")


if __name__ == "__main__":
    unittest.main()
