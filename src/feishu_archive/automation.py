from __future__ import annotations

import errno
import fcntl
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULT_INCREMENTAL_DAYS, DEFAULT_MAX_ATTACHMENT_BYTES, ArchivePaths
from .database import ArchiveDatabase
from .sync import ArchiveSyncer, SyncCounts


class SyncBusyError(RuntimeError):
    pass


class SyncFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("a+", encoding="utf-8")
        os.chmod(path, 0o600)
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise SyncBusyError("已有同步任务正在运行") from exc
            raise
        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"pid={os.getpid()} started_at={int(time.time() * 1000)}\n")
        self._file.flush()

    def release(self) -> None:
        if self._file.closed:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()


def acquire_sync_lock(paths: ArchivePaths) -> SyncFileLock:
    paths.ensure()
    return SyncFileLock(paths.sync_lock)


def run_sync_cycle(
    database: ArchiveDatabase,
    paths: ArchivePaths,
    client_factory: Callable[[], Any],
    *,
    trigger: str,
    overlap_days: int = DEFAULT_INCREMENTAL_DAYS,
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    lock: SyncFileLock | None = None,
    syncer_factory: Callable[..., ArchiveSyncer] = ArchiveSyncer,
) -> dict[str, Any]:
    if overlap_days < 1:
        raise ValueError("overlap_days 必须大于 0")
    active_lock = lock or acquire_sync_lock(paths)
    job_id: int | None = None
    totals = SyncCounts()
    conversations_discovered = 0
    new_conversations_count = 0
    try:
        job_id = database.start_sync_job(trigger)
        existing_ids = set(database.conversation_ids())
        syncer = syncer_factory(
            database,
            client_factory(),
            paths,
            max_attachment_bytes=max_attachment_bytes,
        )
        syncer.discover()
        all_chat_ids = database.conversation_ids()
        conversations_discovered = len(all_chat_ids)
        new_chat_ids = [chat_id for chat_id in all_chat_ids if chat_id not in existing_ids]
        existing_chat_ids = [chat_id for chat_id in all_chat_ids if chat_id in existing_ids]
        new_conversations_count = len(new_chat_ids)

        statuses: list[str] = []
        errors: list[str] = []
        for chat_ids, days in ((new_chat_ids, None), (existing_chat_ids, overlap_days)):
            if not chat_ids:
                continue
            counts = syncer.sync(chat_ids, days=days)
            _add_counts(totals, counts)
            latest = database.status().get("latest_sync") or {}
            statuses.append(str(latest.get("status") or "error"))
            if latest.get("error"):
                errors.append(str(latest["error"]))

        status = _combined_status(statuses)
        database.finish_sync_job(
            job_id,
            status=status,
            error="\n".join(errors) or None,
            conversations_discovered=conversations_discovered,
            new_conversations=new_conversations_count,
            **totals.as_dict(),
        )
        result = database.latest_sync_job()
        if result is None:
            raise RuntimeError("同步作业状态未保存")
        return result
    except Exception as exc:
        if job_id is not None:
            database.finish_sync_job(
                job_id,
                status="error",
                error=str(exc),
                conversations_discovered=conversations_discovered,
                new_conversations=new_conversations_count,
                **totals.as_dict(),
            )
        raise
    finally:
        active_lock.release()


class BackgroundSyncController:
    def __init__(
        self,
        database: ArchiveDatabase,
        paths: ArchivePaths,
        client_factory: Callable[[], Any],
        *,
        overlap_days: int = DEFAULT_INCREMENTAL_DAYS,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    ) -> None:
        self.database = database
        self.paths = paths
        self.client_factory = client_factory
        self.overlap_days = overlap_days
        self.max_attachment_bytes = max_attachment_bytes
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return False
            try:
                lock = acquire_sync_lock(self.paths)
            except SyncBusyError:
                return False
            self._thread = threading.Thread(
                target=self._run,
                args=(lock,),
                name="feishu-manual-sync",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, lock: SyncFileLock) -> None:
        try:
            run_sync_cycle(
                self.database,
                self.paths,
                self.client_factory,
                trigger="manual",
                overlap_days=self.overlap_days,
                max_attachment_bytes=self.max_attachment_bytes,
                lock=lock,
            )
        except Exception as exc:
            print(f"[sync] 手工同步失败：{exc}", file=sys.stderr)


def _add_counts(total: SyncCounts, value: SyncCounts) -> None:
    total.messages_seen += value.messages_seen
    total.messages_written += value.messages_written
    total.attachments_downloaded += value.attachments_downloaded
    total.attachments_skipped += value.attachments_skipped
    total.attachments_pruned += value.attachments_pruned
    total.attachment_bytes_pruned += value.attachment_bytes_pruned


def _combined_status(statuses: list[str]) -> str:
    if not statuses or all(status == "success" for status in statuses):
        return "success"
    if all(status == "error" for status in statuses):
        return "error"
    return "partial"
