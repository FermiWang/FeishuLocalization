from __future__ import annotations

import json
import subprocess
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from feishu_archive.vmlx import (
    DEFAULT_VMLX_MODEL,
    Tunnel,
    VMLXClient,
    VMLXError,
    build_ssh_tunnel_argv,
)


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200, content_length: str | None = None):
        self.payload = payload
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class RecordingOpener:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request: urllib.request.Request, *, timeout: float):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class VMLXClientTests(unittest.TestCase):
    def test_base_url_requires_literal_loopback_without_ambient_url_parts(self) -> None:
        for invalid in (
            "http://example.com:8067",
            "http://127.0.0.1:8067?token=secret",
            "http://127.0.0.1:8067#fragment",
            "http://user:pass@127.0.0.1:8067",
            "ftp://127.0.0.1:8067",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                VMLXClient(invalid)
        self.assertEqual(
            VMLXClient("http://[::1]:11435/v1/").base_url,
            "http://[::1]:11435/v1",
        )

    def test_default_opener_disables_proxies_and_redirects(self) -> None:
        with patch("feishu_archive.vmlx.urllib.request.build_opener") as builder:
            VMLXClient("http://127.0.0.1:11435")
        handlers = builder.call_args.args
        proxy = next(handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        redirect = next(
            handler for handler in handlers if isinstance(handler, urllib.request.HTTPRedirectHandler)
        )
        request = urllib.request.Request("http://127.0.0.1:11435/v1/models")
        self.assertIsNone(redirect.redirect_request(request, None, 302, "Found", {}, "http://evil.invalid"))

    def test_models_get_uses_timeout_and_optional_bearer(self) -> None:
        opener = RecordingOpener(FakeResponse(b'{"data":[{"id":"model-1"}]}'))
        client = VMLXClient(
            "http://127.0.0.1:11435/v1",
            bearer_token="local-secret",
            timeout=2.5,
            opener=opener,
        )
        self.assertEqual(client.models(), [{"id": "model-1"}])
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11435/v1/models")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer local-secret")
        self.assertEqual(timeout, 2.5)

        no_token_opener = RecordingOpener(FakeResponse(b'{"data":[]}'))
        VMLXClient("http://127.0.0.1:11435", bearer_token=None, opener=no_token_opener).models()
        self.assertIsNone(no_token_opener.calls[0][0].get_header("Authorization"))

    def test_chat_json_posts_openai_payload_and_parses_json_fence(self) -> None:
        response = {
            "choices": [
                {"message": {"content": "```json\n{\"summary\":\"完成\",\"count\":2}\n```"}}
            ]
        }
        opener = RecordingOpener(
            FakeResponse(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        )
        client = VMLXClient(
            "http://127.0.0.1:8067",
            model=DEFAULT_VMLX_MODEL,
            opener=opener,
        )
        result = client.chat_json(
            [{"role": "user", "content": "summarize"}],
            max_tokens=321,
            temperature=0.25,
        )
        self.assertEqual(result, {"summary": "完成", "count": 2})
        request = opener.calls[0][0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "http://127.0.0.1:8067/v1/chat/completions")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], DEFAULT_VMLX_MODEL)
        self.assertEqual(payload["max_tokens"], 321)
        self.assertEqual(payload["temperature"], 0.25)
        self.assertEqual(payload["messages"][0]["role"], "user")

    def test_chat_response_and_json_content_are_strict(self) -> None:
        client = VMLXClient("http://127.0.0.1:11435", opener=RecordingOpener())
        invalid_completions = (
            {},
            {"choices": []},
            {"choices": [{"message": {"content": "one"}}, {"message": {"content": "two"}}]},
            {"choices": [{"text": "legacy"}]},
            {"choices": [{"message": {"content": [{"text": "array content"}]}}]},
        )
        for payload in invalid_completions:
            with self.subTest(payload=payload), self.assertRaises(VMLXError):
                client._content_from_completion(payload)

        invalid_content = (
            "prefix {\"ok\":true}",
            "```\n{\"ok\":true}\n```",
            "```JSON\n{\"ok\":true}\n```",
            "```json\n[1,2]\n```",
            "{\"ok\":true} suffix",
        )
        for content in invalid_content:
            opener = RecordingOpener(
                FakeResponse(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
            )
            with self.subTest(content=content), self.assertRaises(VMLXError):
                VMLXClient("http://127.0.0.1:11435", opener=opener).chat_json(
                    [{"role": "user", "content": "test"}]
                )

    def test_response_limit_and_http_errors_do_not_expose_body_or_token(self) -> None:
        oversized = RecordingOpener(FakeResponse(b"x" * 9, content_length="9"))
        with self.assertRaisesRegex(VMLXError, "size limit"):
            VMLXClient(
                "http://127.0.0.1:11435",
                max_response_bytes=8,
                opener=oversized,
            ).models()

        secret = "bearer-secret-value"
        response_body = b"private upstream diagnostic"
        response_file = MagicMock()
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8067/v1/models",
            500,
            "private reason",
            {},
            response_file,
        )
        opener = RecordingOpener(error=error)
        with self.assertRaises(VMLXError) as context:
            VMLXClient(
                "http://127.0.0.1:8067",
                bearer_token=secret,
                opener=opener,
            ).models()
        rendered = str(context.exception)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(response_body.decode(), rendered)
        self.assertNotIn("private reason", rendered)
        response_file.read.assert_not_called()
        response_file.close.assert_called_once_with()

        no_header = RecordingOpener(FakeResponse(b"x" * 9))
        with self.assertRaisesRegex(VMLXError, "size limit"):
            VMLXClient(
                "http://127.0.0.1:11435",
                max_response_bytes=8,
                opener=no_header,
            ).models()


class TunnelTests(unittest.TestCase):
    def test_ssh_argv_is_fixed_loopback_forward_without_shell(self) -> None:
        argv = build_ssh_tunnel_argv("192.168.100.179", "apple", 11435)
        self.assertEqual(argv[0:5], ["ssh", "-F", "/dev/null", "-N", "-T"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("127.0.0.1:11435:127.0.0.1:8067", argv)
        self.assertEqual(argv[-2:], ["--", "apple@192.168.100.179"])
        for unsafe in ("-oProxyCommand=bad", "host;command", "user@host"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                build_ssh_tunnel_argv(unsafe, "apple", 11435)

    def test_tunnel_polls_then_cleans_up(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        process.wait.return_value = 0
        popen = MagicMock(return_value=process)
        connection = MagicMock()
        connector = MagicMock(side_effect=[ConnectionRefusedError(), connection])
        sleeps = []
        tunnel = Tunnel(
            "192.168.100.179",
            "apple",
            11435,
            remote_port=11435,
            startup_timeout=1,
            poll_interval=0.01,
            popen_factory=popen,
            connection_factory=connector,
            sleep=sleeps.append,
        )
        with tunnel as base_url:
            self.assertEqual(base_url, "http://127.0.0.1:11435")
            kwargs = popen.call_args.kwargs
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(kwargs["shell"])
        self.assertEqual(len(sleeps), 1)
        connection.close.assert_called_once_with()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3.0)

    def test_tunnel_early_exit_and_kill_fallback_are_cleaned_up(self) -> None:
        exited = MagicMock()
        exited.poll.return_value = 23
        tunnel = Tunnel(
            "host.example",
            "user",
            12000,
            popen_factory=MagicMock(return_value=exited),
        )
        with self.assertRaisesRegex(VMLXError, "exited"):
            tunnel.__enter__()
        exited.terminate.assert_not_called()

        running = MagicMock()
        running.poll.return_value = None
        running.wait.side_effect = [subprocess.TimeoutExpired("ssh", 3), 0]
        connector = MagicMock(return_value=MagicMock())
        tunnel = Tunnel(
            "host.example",
            "user",
            12001,
            popen_factory=MagicMock(return_value=running),
            connection_factory=connector,
        )
        with tunnel:
            pass
        running.terminate.assert_called_once_with()
        running.kill.assert_called_once_with()

    def test_tunnel_timeout_terminates_process(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        process.wait.return_value = 0
        ticks = iter((0.0, 2.0))
        tunnel = Tunnel(
            "host.example",
            "user",
            12002,
            startup_timeout=1,
            popen_factory=MagicMock(return_value=process),
            connection_factory=MagicMock(side_effect=ConnectionRefusedError()),
            monotonic=lambda: next(ticks),
        )
        with self.assertRaisesRegex(VMLXError, "timeout"):
            tunnel.__enter__()
        process.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
