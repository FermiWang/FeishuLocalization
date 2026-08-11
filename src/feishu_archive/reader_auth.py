from __future__ import annotations

import hashlib
import os
import secrets
import stat
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path


SESSION_COOKIE = "FeishuArchiveSession"
DEFAULT_SESSION_TTL_SECONDS = 15 * 60
PERMANENT_UNLOCK_MARKER = b"feishu-archive-mail-reader-permanent-unlock-v1\n"
PERMANENT_UNLOCK_FILENAME = "mail-reader.always-unlocked"
POLICY_GENERATION_FILENAME = "mail-reader.policy-generation"
POLICY_GENERATION_PREFIX = b"feishu-archive-mail-reader-policy-v1:"


class PermanentUnlockPolicyError(ValueError):
    """The persistent local-mail access policy is unsafe or malformed."""


def enable_permanent_unlock(path: Path) -> bool:
    """Safely enable permanent local access for one archive root.

    Returns True when a marker was created and False when an already-valid
    marker existed. Unsafe existing paths are never overwritten. An interrupted
    write remains visible but invalid, so readers fail closed and --lock can
    recover it.
    """

    _ensure_private_parent(path.parent)
    enabled, _ = _read_permanent_unlock_state(path)
    _rotate_policy_generation(_policy_generation_path(path), allow_repair=False)
    if enabled:
        return False

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        enabled, _ = _read_permanent_unlock_state(path)
        if not enabled:
            raise PermanentUnlockPolicyError("永久解锁策略文件创建冲突")
        return False
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(PERMANENT_UNLOCK_MARKER):
            count = os.write(descriptor, PERMANENT_UNLOCK_MARKER[written:])
            if count <= 0:  # pragma: no cover - os.write either writes or raises
                raise OSError("永久解锁策略写入中断")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    enabled, _ = _read_permanent_unlock_state(path)
    if not enabled:  # pragma: no cover - created content is constant
        raise PermanentUnlockPolicyError("永久解锁策略文件创建失败")
    return True


def disable_permanent_unlock(path: Path) -> bool:
    """Relock one archive without touching its durable reader secret."""

    _ensure_private_parent(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        _validate_relock_target(path, metadata)
    _rotate_policy_generation(_policy_generation_path(path), allow_repair=True)
    if metadata is None:
        return False
    current = path.lstat()
    _validate_relock_target(path, current)
    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise PermanentUnlockPolicyError("永久解锁策略文件在回锁期间发生变化")
    path.unlink()
    _fsync_directory(path.parent)
    return True


def permanent_unlock_enabled(path: Path) -> bool:
    enabled, _ = _read_permanent_unlock_state(path)
    return enabled


class ReaderSessionManager:
    """Small loopback-only session gate for sensitive mail endpoints.

    The durable unlock secret lives in a mode-0600 file. Browser sessions are
    random, memory-only and expire after a short fixed period. The unlock secret
    is intended to travel in the URL fragment and then in a POST body, so it is
    never part of an HTTP request target or access log.
    """

    def __init__(
        self,
        secret_path: Path,
        *,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        permanent_unlock_path: Path | None = None,
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("会话有效期不能少于 60 秒")
        self.secret_path = secret_path
        self.permanent_unlock_path = permanent_unlock_path or (
            secret_path.parent / PERMANENT_UNLOCK_FILENAME
        )
        self.ttl_seconds = ttl_seconds
        self._guard = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._unlock_secret = self._load_or_create_secret()
        self._permanent_enabled, self._policy_revision = _read_access_policy_state(
            self.permanent_unlock_path
        )

    @property
    def unlock_secret(self) -> str:
        return self._unlock_secret

    @property
    def permanent_unlock_enabled(self) -> bool:
        enabled, _ = _read_permanent_unlock_state(self.permanent_unlock_path)
        return enabled

    def allows_request(self, cookie_header: str | None) -> bool:
        """Apply the persistent access policy before the cookie-only gate."""

        enabled = self._refresh_access_policy()
        if enabled is None:
            return False
        if enabled:
            return True
        return self.validate_cookie(cookie_header)

    def _refresh_access_policy(self) -> bool | None:
        # Serialize the filesystem read with revision application. Otherwise a
        # slow request could read an old enabled state, wait behind a newer
        # locked request, then overwrite the newer in-memory revision.
        with self._guard:
            try:
                enabled, revision = _read_access_policy_state(self.permanent_unlock_path)
            except PermanentUnlockPolicyError:
                self._sessions.clear()
                self._permanent_enabled = False
                self._policy_revision = ("invalid",)
                return None
            if revision != self._policy_revision:
                # A policy toggle invalidates every temporary browser session so
                # relocking is immediate and cannot inherit an old cookie.
                self._sessions.clear()
                self._policy_revision = revision
            self._permanent_enabled = enabled
        return enabled

    def create_session(self, presented_secret: str) -> str | None:
        if not secrets.compare_digest(presented_secret, self._unlock_secret):
            return None
        if self._refresh_access_policy() is None:
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


def _ensure_private_parent(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PermanentUnlockPolicyError("档案根目录必须是普通目录，不能是符号链接")
    if metadata.st_uid != os.getuid():
        raise PermanentUnlockPolicyError("档案根目录必须归当前用户所有")
    os.chmod(parent, 0o700)


def _validate_marker_metadata(
    path: Path,
    metadata: os.stat_result,
    *,
    require_mode: bool = True,
) -> None:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PermanentUnlockPolicyError(f"永久解锁策略必须是普通文件：{path}")
    if metadata.st_uid != os.getuid():
        raise PermanentUnlockPolicyError("永久解锁策略文件必须归当前用户所有")
    if metadata.st_nlink != 1:
        raise PermanentUnlockPolicyError("永久解锁策略文件不能有硬链接")
    mode = stat.S_IMODE(metadata.st_mode)
    if require_mode and mode != 0o600:
        raise PermanentUnlockPolicyError(
            f"永久解锁策略文件权限必须是 0600，当前为 {oct(mode)}"
        )


def _validate_relock_target(path: Path, metadata: os.stat_result) -> None:
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise PermanentUnlockPolicyError(f"拒绝删除非文件类型的策略路径：{path}")
    if metadata.st_uid != os.getuid():
        raise PermanentUnlockPolicyError("策略路径必须归当前用户所有才能回锁")


def _read_permanent_unlock_state(path: Path) -> tuple[bool, tuple[object, ...]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False, ("absent",)
    _validate_marker_metadata(path, metadata)

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PermanentUnlockPolicyError("无法安全读取永久解锁策略文件") from exc
    try:
        opened = os.fstat(descriptor)
        _validate_marker_metadata(path, opened)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PermanentUnlockPolicyError("永久解锁策略文件在读取期间发生变化")
        payload = os.read(descriptor, len(PERMANENT_UNLOCK_MARKER) + 1)
    finally:
        os.close(descriptor)
    if payload != PERMANENT_UNLOCK_MARKER:
        raise PermanentUnlockPolicyError("永久解锁策略文件内容无效")
    revision = (
        "enabled",
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        stat.S_IMODE(opened.st_mode),
    )
    return True, revision


def _read_access_policy_state(path: Path) -> tuple[bool, tuple[object, ...]]:
    enabled, marker_revision = _read_permanent_unlock_state(path)
    generation_revision = _read_policy_generation_state(_policy_generation_path(path))
    return enabled, (marker_revision, generation_revision)


def _policy_generation_path(marker_path: Path) -> Path:
    return marker_path.parent / POLICY_GENERATION_FILENAME


def _read_policy_generation_state(path: Path) -> tuple[object, ...]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ("generation-absent",)
    _validate_marker_metadata(path, metadata)
    payload = _read_private_policy_file(path, metadata, "策略代次")
    token = payload[len(POLICY_GENERATION_PREFIX) : -1]
    if (
        not payload.startswith(POLICY_GENERATION_PREFIX)
        or not payload.endswith(b"\n")
        or len(token) != 32
        or any(character not in b"0123456789abcdef" for character in token)
    ):
        raise PermanentUnlockPolicyError("邮箱访问策略代次文件内容无效")
    return ("generation", payload)


def _read_private_policy_file(
    path: Path,
    metadata: os.stat_result,
    label: str,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PermanentUnlockPolicyError(f"无法安全读取{label}文件") from exc
    try:
        opened = os.fstat(descriptor)
        _validate_marker_metadata(path, opened)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PermanentUnlockPolicyError(f"{label}文件在读取期间发生变化")
        return os.read(descriptor, 4097)
    finally:
        os.close(descriptor)


def _rotate_policy_generation(path: Path, *, allow_repair: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if allow_repair:
            _validate_relock_target(path, metadata)
        else:
            _read_policy_generation_state(path)

    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = POLICY_GENERATION_PREFIX + secrets.token_hex(16).encode("ascii") + b"\n"
        try:
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:  # pragma: no cover - os.write either writes or raises
                    raise OSError("邮箱访问策略代次写入中断")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    _read_policy_generation_state(path)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
