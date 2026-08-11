from __future__ import annotations

import errno
import fcntl
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_MAIL_INITIAL_DAYS,
    DEFAULT_MAIL_OVERLAP_DAYS,
    DEFAULT_MAIL_MAX_PAGES,
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    DEFAULT_MAX_MAIL_BYTES,
    ArchivePaths,
)
from .database import ArchiveDatabase
from .mail_database import MailDatabase
from .mail_sync import MailSyncer
from .sync import ArchiveSyncer, SyncCounts
from .wiki import WikiSyncCounts, WikiSyncer


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


def acquire_wiki_sync_lock(paths: ArchivePaths) -> SyncFileLock:
    paths.ensure()
    return SyncFileLock(paths.wiki_sync_lock)


def acquire_mail_sync_lock(paths: ArchivePaths) -> SyncFileLock:
    paths.ensure()
    return SyncFileLock(paths.mail_sync_lock)


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


def run_wiki_sync_cycle(
    database: ArchiveDatabase,
    paths: ArchivePaths,
    client_factory: Callable[[], Any],
    *,
    trigger: str,
    space_ids: list[str] | None = None,
    force: bool = False,
    max_asset_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    lock: SyncFileLock | None = None,
    syncer_factory: Callable[..., WikiSyncer] = WikiSyncer,
) -> dict[str, Any]:
    active_lock = lock or acquire_wiki_sync_lock(paths)
    requested = space_ids or []
    run_id: int | None = None
    counts = WikiSyncCounts()
    try:
        run_id = database.start_wiki_sync_run(trigger, requested)
        syncer = syncer_factory(
            database,
            client_factory(),
            paths,
            max_asset_bytes=max_asset_bytes,
        )
        counts, errors = syncer.sync(space_ids, force=force)
        status = "success"
        if errors:
            status = "partial" if counts.nodes_seen else "error"
        database.finish_wiki_sync_run(
            run_id,
            status=status,
            error="\n".join(errors) or None,
            **counts.as_dict(),
        )
        result = database.latest_wiki_sync_run()
        if result is None:
            raise RuntimeError("知识库同步作业状态未保存")
        return result
    except Exception as exc:
        if run_id is not None:
            database.finish_wiki_sync_run(
                run_id,
                status="error",
                error=str(exc),
                **counts.as_dict(),
            )
        raise
    finally:
        active_lock.release()


class BackgroundWikiSyncController:
    def __init__(
        self,
        database: ArchiveDatabase,
        paths: ArchivePaths,
        client_factory: Callable[[], Any],
        *,
        max_asset_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    ) -> None:
        self.database = database
        self.paths = paths
        self.client_factory = client_factory
        self.max_asset_bytes = max_asset_bytes
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return False
            try:
                lock = acquire_wiki_sync_lock(self.paths)
            except SyncBusyError:
                return False
            self._thread = threading.Thread(
                target=self._run,
                args=(lock,),
                name="feishu-wiki-manual-sync",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, lock: SyncFileLock) -> None:
        try:
            run_wiki_sync_cycle(
                self.database,
                self.paths,
                self.client_factory,
                trigger="manual",
                max_asset_bytes=self.max_asset_bytes,
                lock=lock,
            )
        except Exception as exc:
            print(f"[wiki-sync] 手工同步失败：{exc}", file=sys.stderr)


def run_mail_sync_cycle(
    database: MailDatabase,
    paths: ArchivePaths,
    provider_factory: Callable[[], Any],
    *,
    trigger: str,
    days: int | None = DEFAULT_MAIL_OVERLAP_DAYS,
    folders: list[str] | None = None,
    skip_attachments: bool = False,
    max_mail_bytes: int = DEFAULT_MAX_MAIL_BYTES,
    max_attachment_bytes: int = DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    max_pages: int = DEFAULT_MAIL_MAX_PAGES,
    lock: SyncFileLock | None = None,
    syncer_factory: Callable[..., MailSyncer] = MailSyncer,
) -> dict[str, Any]:
    active_lock = lock or acquire_mail_sync_lock(paths)
    try:
        syncer = syncer_factory(
            database,
            provider_factory(),
            paths,
            max_mail_bytes=max_mail_bytes,
            max_attachment_bytes=max_attachment_bytes,
        )
        syncer.sync(
            folders=folders,
            days=days,
            skip_attachments=skip_attachments,
            trigger=trigger,
            max_pages=max_pages,
        )
        result = database.latest_sync_run()
        if result is None:
            raise RuntimeError("邮箱同步作业状态未保存")
        return result
    finally:
        active_lock.release()


class BackgroundMailSyncController:
    def __init__(
        self,
        database: MailDatabase,
        paths: ArchivePaths,
        provider_factory: Callable[[], Any],
        *,
        days: int | None = DEFAULT_MAIL_INITIAL_DAYS,
        max_mail_bytes: int = DEFAULT_MAX_MAIL_BYTES,
        max_attachment_bytes: int = DEFAULT_MAX_MAIL_ATTACHMENT_BYTES,
    ) -> None:
        self.database = database
        self.paths = paths
        self.provider_factory = provider_factory
        self.days = days
        self.max_mail_bytes = max_mail_bytes
        self.max_attachment_bytes = max_attachment_bytes
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return False
            try:
                lock = acquire_mail_sync_lock(self.paths)
            except SyncBusyError:
                return False
            self._thread = threading.Thread(
                target=self._run,
                args=(lock,),
                name="feishu-mail-manual-sync",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, lock: SyncFileLock) -> None:
        try:
            run_mail_sync_cycle(
                self.database,
                self.paths,
                self.provider_factory,
                trigger="manual",
                days=self.days,
                max_mail_bytes=self.max_mail_bytes,
                max_attachment_bytes=self.max_attachment_bytes,
                lock=lock,
            )
        except Exception as exc:
            print(f"[mail-sync] 手工同步失败：{exc}", file=sys.stderr)


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
