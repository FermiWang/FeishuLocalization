from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


DEFAULT_VMLX_MODEL = "vmlx/gemma-4-31b-it-8bit"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class VMLXError(RuntimeError):
    """A safe-to-display failure from the local vMLX client or tunnel."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("vMLX base URL is required")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("vMLX base URL contains invalid characters")
    # urlsplit discards empty query/fragment delimiters, so reject the delimiters
    # themselves as well as non-empty parsed values.
    if "?" in value or "#" in value:
        raise ValueError("vMLX base URL must not contain a query or fragment")
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("vMLX base URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("vMLX base URL must use http or https")
    if not parsed.netloc or not host:
        raise ValueError("vMLX base URL must include a host")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("vMLX base URL must not contain user information")
    normalized_host = host.lower()
    literal_loopback = normalized_host == "localhost"
    if not literal_loopback:
        try:
            literal_loopback = ipaddress.ip_address(normalized_host).is_loopback
        except ValueError:
            literal_loopback = False
    if not literal_loopback:
        raise ValueError("vMLX base URL must use a literal loopback host")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("vMLX base URL port is invalid")
    if "\\" in parsed.path:
        raise ValueError("vMLX base URL path is invalid")

    rendered_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    authority = rendered_host if port is None else f"{rendered_host}:{port}"
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), authority, path, "", ""))


def _json_object_from_content(content: str) -> dict[str, Any]:
    if not isinstance(content, str):
        raise VMLXError("vMLX response content is not text")
    candidate = content.strip()
    if candidate.startswith("```"):
        match = re.fullmatch(r"```json[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```", candidate)
        if match is None:
            raise VMLXError("vMLX response does not contain a single JSON object")
        candidate = match.group("body").strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, UnicodeError):
        raise VMLXError("vMLX response does not contain valid JSON") from None
    if not isinstance(value, dict):
        raise VMLXError("vMLX response JSON must be an object")
    return value


class VMLXClient:
    def __init__(
        self,
        base_url: str,
        model: str = DEFAULT_VMLX_MODEL,
        bearer_token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        opener: Any | None = None,
    ) -> None:
        self.base_url = _validated_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("vMLX model is required")
        if bearer_token is not None:
            if not isinstance(bearer_token, str) or not bearer_token:
                raise ValueError("vMLX bearer token must be a non-empty string or None")
            if any(ord(character) < 33 or ord(character) == 127 for character in bearer_token):
                raise ValueError("vMLX bearer token contains invalid characters")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("vMLX timeout must be a positive number")
        if not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ValueError("vMLX timeout must be a positive number")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError("vMLX response limit must be a positive integer")
        self.model = model.strip()
        self.bearer_token = bearer_token
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def _endpoint(self, resource: str) -> str:
        root = self.base_url.rstrip("/")
        if root.endswith("/v1"):
            return f"{root}/{resource}"
        return f"{root}/v1/{resource}"

    def _request_json(
        self,
        method: str,
        resource: str,
        *,
        payload: Mapping[str, Any] | None = None,
        versioned: bool = True,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if payload is not None:
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise ValueError("vMLX request payload is not JSON serializable") from None
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(
            self._endpoint(resource)
            if versioned
            else f"{self.base_url.removesuffix('/v1').rstrip('/')}/{resource.lstrip('/')}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None)
                if status is None and callable(getattr(response, "getcode", None)):
                    status = response.getcode()
                if status is not None and not 200 <= int(status) < 300:
                    raise VMLXError(f"vMLX request failed with HTTP status {int(status)}")
                content_length = None
                headers_value = getattr(response, "headers", None)
                if headers_value is not None:
                    content_length = headers_value.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError):
                        declared_length = None
                    if declared_length is not None and declared_length > self.max_response_bytes:
                        raise VMLXError("vMLX response exceeded the configured size limit")
                raw = response.read(self.max_response_bytes + 1)
        except VMLXError:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                exc.close()
            except Exception:
                pass
            raise VMLXError(f"vMLX request failed with HTTP status {status}") from None
        except (TimeoutError, socket.timeout):
            raise VMLXError("vMLX request timed out") from None
        except (urllib.error.URLError, OSError):
            raise VMLXError("vMLX request failed") from None
        except Exception:
            # HTTP exceptions can retain response bytes or request headers. Do
            # not copy arbitrary upstream exception text into a displayed error.
            raise VMLXError("vMLX request failed") from None
        if not isinstance(raw, bytes):
            raise VMLXError("vMLX response body is invalid")
        if len(raw) > self.max_response_bytes:
            raise VMLXError("vMLX response exceeded the configured size limit")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise VMLXError("vMLX response is not valid JSON") from None
        if not isinstance(result, dict):
            raise VMLXError("vMLX response must be a JSON object")
        return result

    def models(self) -> list[dict[str, Any]]:
        payload = self._request_json("GET", "models")
        items = payload.get("data")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise VMLXError("vMLX models response has an invalid data field")
        return items

    def health(self) -> dict[str, Any]:
        """Return the engine health object from the non-versioned endpoint."""
        return self._request_json("GET", "health", versioned=False)

    def list_models(self) -> list[dict[str, Any]]:
        return self.models()

    def chat_completion(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence) or not messages:
            raise ValueError("vMLX messages must be a non-empty sequence")
        copied_messages: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError("each vMLX message must be an object")
            role = message.get("role")
            if not isinstance(role, str) or not role.strip() or "content" not in message:
                raise ValueError("each vMLX message requires role and content")
            copied_messages.append(dict(message))
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValueError("vMLX max_tokens must be a positive integer")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("vMLX temperature must be a finite number from 0 to 2")
        temperature_value = float(temperature)
        if not math.isfinite(temperature_value) or not 0 <= temperature_value <= 2:
            raise ValueError("vMLX temperature must be a finite number from 0 to 2")
        return self._request_json(
            "POST",
            "chat/completions",
            payload={
                "model": self.model,
                "messages": copied_messages,
                "max_tokens": max_tokens,
                "temperature": temperature_value,
            },
        )

    @staticmethod
    def _content_from_completion(payload: Mapping[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise VMLXError("vMLX response must contain exactly one choice")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise VMLXError("vMLX response choice is invalid")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise VMLXError("vMLX response choice has no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise VMLXError("vMLX response message has no text content")
        return content

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> str:
        payload = self.chat_completion(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._content_from_completion(payload)

    def chat_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        return _json_object_from_content(
            self.chat(messages, max_tokens=max_tokens, temperature=temperature)
        )


def _validated_port(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{name} must be an integer from 1 to 65535")
    return value


def build_ssh_tunnel_argv(
    host: str,
    user: str,
    local_port: int,
    remote_port: int = 8067,
    identity_file: str | None = None,
) -> list[str]:
    if (
        not isinstance(host, str)
        or not host
        or host.startswith("-")
        or "@" in host
        or re.fullmatch(r"[A-Za-z0-9._:-]+", host) is None
    ):
        raise ValueError("SSH host is invalid")
    if (
        not isinstance(user, str)
        or not user
        or user.startswith("-")
        or re.fullmatch(r"[A-Za-z0-9._-]+", user) is None
    ):
        raise ValueError("SSH user is invalid")
    local = _validated_port(local_port, "local_port")
    remote = _validated_port(remote_port, "remote_port")
    identity_path: str | None = None
    if identity_file is not None:
        if not isinstance(identity_file, str) or not identity_file.strip():
            raise ValueError("SSH identity file is invalid")
        if any(ord(character) < 32 or ord(character) == 127 for character in identity_file):
            raise ValueError("SSH identity file is invalid")
        candidate = Path(identity_file).expanduser()
        if not candidate.is_absolute() or not candidate.is_file():
            raise ValueError("SSH identity file must be an existing absolute path")
        identity_path = str(candidate.resolve())
    argv = [
        "ssh",
        "-F",
        "/dev/null",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
    ]
    if identity_path is not None:
        argv.extend(("-i", identity_path))
    argv.extend((
        "-L",
        f"127.0.0.1:{local}:127.0.0.1:{remote}",
        "--",
        f"{user}@{host}",
    ))
    return argv


class Tunnel:
    def __init__(
        self,
        host: str,
        user: str,
        local_port: int,
        remote_port: int = 8067,
        identity_file: str | None = None,
        *,
        startup_timeout: float = 10.0,
        poll_interval: float = 0.05,
        shutdown_timeout: float = 3.0,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        connection_factory: Callable[..., Any] = socket.create_connection,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.argv = build_ssh_tunnel_argv(
            host,
            user,
            local_port,
            remote_port,
            identity_file=identity_file,
        )
        startup_timeout = _positive_seconds(startup_timeout, "startup_timeout")
        poll_interval = _positive_seconds(poll_interval, "poll_interval")
        shutdown_timeout = _positive_seconds(shutdown_timeout, "shutdown_timeout")
        self.local_port = local_port
        self.remote_port = remote_port
        self.base_url = f"http://127.0.0.1:{local_port}"
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = float(poll_interval)
        self.shutdown_timeout = float(shutdown_timeout)
        self._popen_factory = popen_factory
        self._connection_factory = connection_factory
        self._monotonic = monotonic
        self._sleep = sleep
        self._process: Any | None = None

    @property
    def process(self) -> Any | None:
        return self._process

    def __enter__(self) -> str:
        if self._process is not None:
            raise RuntimeError("SSH tunnel context cannot be entered twice")
        try:
            self._process = self._popen_factory(
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            self._process = None
            raise VMLXError("could not start the SSH tunnel") from None

        deadline = self._monotonic() + self.startup_timeout
        try:
            while True:
                if self._process.poll() is not None:
                    raise VMLXError("SSH tunnel exited before becoming ready")
                try:
                    connection = self._connection_factory(
                        ("127.0.0.1", self.local_port),
                        timeout=min(0.25, self.poll_interval),
                    )
                except (OSError, TimeoutError):
                    connection = None
                if connection is not None:
                    try:
                        return self.base_url
                    finally:
                        close = getattr(connection, "close", None)
                        if callable(close):
                            close()
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise VMLXError("SSH tunnel did not become ready before timeout")
                self._sleep(min(self.poll_interval, remaining))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=self.shutdown_timeout)
            except (OSError, subprocess.SubprocessError):
                pass
        except (OSError, subprocess.SubprocessError):
            pass

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.close()
        return False


VMLXTunnel = Tunnel


def _positive_seconds(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive number")
    return result
