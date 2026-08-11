from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path


SESSION_COOKIE = "FeishuArchiveSession"
DEFAULT_SESSION_TTL_SECONDS = 15 * 60


class ReaderSessionManager:
    """Small loopback-only session gate for sensitive mail endpoints.

    The durable unlock secret lives in a mode-0600 file. Browser sessions are
    random, memory-only and expire after a short fixed period. The unlock secret
    is intended to travel in the URL fragment and then in a POST body, so it is
    never part of an HTTP request target or access log.
    """

    def __init__(self, secret_path: Path, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> None:
        if ttl_seconds < 60:
            raise ValueError("会话有效期不能少于 60 秒")
        self.secret_path = secret_path
        self.ttl_seconds = ttl_seconds
        self._guard = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._unlock_secret = self._load_or_create_secret()

    @property
    def unlock_secret(self) -> str:
        return self._unlock_secret

    def create_session(self, presented_secret: str) -> str | None:
        if not secrets.compare_digest(presented_secret, self._unlock_secret):
            return None
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        now = time.monotonic()
        with self._guard:
            self._purge_locked(now)
            self._sessions[digest] = now + self.ttl_seconds
        return token

    def validate_cookie(self, cookie_header: str | None) -> bool:
        if not cookie_header:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        if morsel is None or not morsel.value:
            return False
        digest = self._digest(morsel.value)
        now = time.monotonic()
        with self._guard:
            self._purge_locked(now)
            expires_at = self._sessions.get(digest)
            if expires_at is None or expires_at <= now:
                return False
        return True

    def cookie_value(self, session_token: str) -> str:
        return (
            f"{SESSION_COOKIE}={session_token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={self.ttl_seconds}"
        )

    def revoke_cookie(self, cookie_header: str | None) -> None:
        if not cookie_header:
            return
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return
        morsel = cookie.get(SESSION_COOKIE)
        if morsel is None:
            return
        with self._guard:
            self._sessions.pop(self._digest(morsel.value), None)

    def _load_or_create_secret(self) -> str:
        self.secret_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(
                self.secret_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            secret = self.secret_path.read_text(encoding="utf-8").strip()
            os.chmod(self.secret_path, 0o600)
            if len(secret) < 32:
                raise ValueError("本机阅读器解锁密钥无效，请安全移走 reader.secret 后重试")
            return secret
        secret = secrets.token_urlsafe(32)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(secret + "\n")
        return secret

    def _purge_locked(self, now: float) -> None:
        expired = [digest for digest, expires_at in self._sessions.items() if expires_at <= now]
        for digest in expired:
            self._sessions.pop(digest, None)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
