from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_CHUNK_CHARACTERS = 12_000


def calendar_day_window(day: date | str, timezone: str | ZoneInfo) -> dict[str, Any]:
    """Return the half-open UTC epoch window for one local calendar day.

    The end is constructed from the following local midnight instead of adding
    24 hours.  Consequently the window remains correct across daylight-saving
    transitions.
    """

    parsed_day = date.fromisoformat(day) if isinstance(day, str) else day
    if isinstance(parsed_day, datetime) or not isinstance(parsed_day, date):
        raise TypeError("day must be a date or an ISO YYYY-MM-DD string")
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    if not isinstance(zone, ZoneInfo):
        raise TypeError("timezone must be a ZoneInfo or an IANA timezone name")
    start = datetime.combine(parsed_day, time.min, tzinfo=zone)
    end = datetime.combine(parsed_day + timedelta(days=1), time.min, tzinfo=zone)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    return {
        "date": parsed_day.isoformat(),
        "timezone": zone.key,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start_s": start_ms // 1000,
        "end_s": end_ms // 1000,
    }


def extract_daily_sources(
    archive_db: Any,
    mail_db: Any,
    day: date | str,
    timezone: str | ZoneInfo,
) -> dict[str, Any]:
    """Read chat, mail and Wiki evidence occurring on one local calendar day.

    Only explicitly selected, analysis-safe columns are returned.  In
    particular raw provider JSON, BCC recipients, HTML/raw MIME blobs and file
    bytes or paths are never read into the result.
    """

    window = calendar_day_window(day, timezone)
    warnings: list[str] = []
    evidence: list[dict[str, Any]] = []
    counts = {
        "chat": 0,
        "mail_received": 0,
        "mail_sent": 0,
        "wiki_created": 0,
        "wiki_edited": 0,
    }
    latest_sync: dict[str, dict[str, Any] | None] = {
        "chat": None,
        "mail": None,
        "wiki": None,
    }
    readable = {"chat": False, "mail": False, "wiki": False}

    try:
        with _read_connection(archive_db) as archive:
            chat_items, chat_warnings = _extract_chat(archive, window)
            wiki_items, wiki_counts = _extract_wiki(archive, window)
            evidence.extend(chat_items)
            evidence.extend(wiki_items)
            counts["chat"] = len(chat_items)
            counts.update(wiki_counts)
            warnings.extend(chat_warnings)
            latest_sync["chat"] = _latest_chat_sync(archive)
            latest_sync["wiki"] = _latest_sync_row(archive, "wiki_sync_runs", "wiki")
            readable["chat"] = True
            readable["wiki"] = True
    except (OSError, sqlite3.Error) as exc:
        warnings.append(f"聊天/知识库档案不可读：{type(exc).__name__}: {exc}")

    if mail_db is None:
        warnings.append("邮件档案未配置")
    else:
        try:
            with _read_connection(mail_db) as mail:
                mail_items, mail_counts, mail_warnings = _extract_mail(mail, window)
                evidence.extend(mail_items)
                counts.update(mail_counts)
                warnings.extend(mail_warnings)
                latest_sync["mail"] = _latest_sync_row(mail, "sync_runs", "mail")
                readable["mail"] = True
        except (OSError, sqlite3.Error) as exc:
            warnings.append(f"邮件档案不可读：{type(exc).__name__}: {exc}")

    blocking_issues: list[str] = []
    for lane, state in latest_sync.items():
        if not readable[lane]:
            blocking_issues.append(f"{lane} 源档案不可读")
            continue
        if state is None:
            warnings.append(f"{lane} 尚无同步记录")
            blocking_issues.append(f"{lane} 尚无成功同步记录")
            continue
        status = str(state.get("status") or "unknown").lower()
        if status not in {"success", "succeeded", "completed", "ok"}:
            warnings.append(f"{lane} 最近同步状态为 {status}")
            blocking_issues.append(f"{lane} 最近同步未成功")
        if state.get("finished_at") is None:
            blocking_issues.append(f"{lane} 最近同步尚未结束")

    evidence.sort(key=_evidence_sort_key)
    return {
        "window": window,
        "coverage": {
            "date": window["date"],
            "timezone": window["timezone"],
            "window": window,
            "counts": counts,
            "latest_sync": latest_sync,
            "warnings": _stable_unique(warnings),
            "complete": not blocking_issues,
            "blocking_issues": _stable_unique(blocking_issues),
        },
        "evidence": evidence,
    }


def chunk_evidence(
    evidence: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    max_chars: int = DEFAULT_CHUNK_CHARACTERS,
    *,
    char_budget: int | None = None,
) -> list[dict[str, Any]]:
    """Group evidence by thread/document without exceeding the text budget.

    Duplicate source records are discarded before grouping.  An individual
    record larger than the budget is included once and its rendered text is
    truncated; a record is never repeated across chunks.
    """

    budget = int(char_budget if char_budget is not None else max_chars)
    if budget < 1:
        raise ValueError("character budget must be positive")

    unique: dict[str, dict[str, Any]] = {}
    for value in evidence:
        item = dict(value)
        source_kind = str(item.get("source_kind") or "")
        source_id = str(item.get("source_id") or "")
        if not source_kind or not source_id:
            raise ValueError("each evidence item needs source_kind and source_id")
        key = str(item.get("evidence_id") or f"{source_kind}:{source_id}")
        unique.setdefault(key, item)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sorted(unique.values(), key=_evidence_sort_key):
        group_key = str(item.get("thread_key") or f"{item['source_kind']}:{item['source_id']}")
        grouped[group_key].append(item)

    chunks: list[dict[str, Any]] = []
    for group_key in sorted(grouped):
        part = 1
        texts: list[str] = []
        items: list[dict[str, Any]] = []
        truncated_ids: list[str] = []

        def flush() -> None:
            nonlocal part, texts, items, truncated_ids
            if not items:
                return
            text = "\n\n".join(texts)
            digest = hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:12]
            chunks.append(
                {
                    "chunk_id": f"{digest}-{part:03d}",
                    "thread_key": group_key,
                    "source_kinds": sorted({str(item["source_kind"]) for item in items}),
                    "items": list(items),
                    "evidence_ids": [
                        str(
                            item.get("evidence_id")
                            or f"{item['source_kind']}:{item['source_id']}"
                        )
                        for item in items
                    ],
                    "citations": [str(item.get("citation") or "") for item in items],
                    "text": text,
                    "char_count": len(text),
                    "truncated_source_ids": list(truncated_ids),
                }
            )
            part += 1
            texts = []
            items = []
            truncated_ids = []

        for item in grouped[group_key]:
            rendered = _render_evidence(item)
            was_truncated = len(rendered) > budget
            if was_truncated:
                rendered = rendered[:budget]
            projected = len(rendered) if not texts else sum(map(len, texts)) + 2 * len(texts) + len(rendered)
            if texts and projected > budget:
                flush()
            texts.append(rendered)
            items.append(item)
            if was_truncated:
                truncated_ids.append(str(item["source_id"]))
        flush()
    return chunks


@contextmanager
def _read_connection(database: Any) -> Iterator[sqlite3.Connection]:
    if isinstance(database, sqlite3.Connection):
        started = not database.in_transaction
        if started:
            database.execute("BEGIN")
        try:
            with nullcontext(database) as connection:
                yield connection
        finally:
            if started and database.in_transaction:
                database.execute("ROLLBACK")
        return
    path_value = getattr(database, "path", database)
    path = Path(path_value).expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def _extract_chat(
    connection: sqlite3.Connection,
    window: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    start_ms, end_ms = int(window["start_ms"]), int(window["end_ms"])
    current_user_row = connection.execute(
        "SELECT value FROM metadata WHERE key='current_user_open_id'"
    ).fetchone()
    current_user_id = str(current_user_row[0]).strip() if current_user_row else ""
    rows = connection.execute(
        """
        SELECT m.message_id, m.chat_id, m.thread_id, m.parent_id, m.root_id,
               m.message_type, m.sender_id, m.sender_type, m.sender_name,
               m.created_at, m.updated_at, m.deleted, m.recalled, m.body_text,
               c.name AS conversation_name, c.chat_mode, c.chat_type, c.external
        FROM messages m
        LEFT JOIN conversations c ON c.chat_id=m.chat_id
        WHERE m.created_at>=? AND m.created_at<?
        ORDER BY m.created_at, m.message_id
        """,
        (start_ms, end_ms),
    ).fetchall()
    attachment_rows = connection.execute(
        """
        SELECT a.message_id, a.file_key, a.resource_type, a.filename,
               a.mime_type, a.byte_size, a.sha256, a.status
        FROM attachments a JOIN messages m ON m.message_id=a.message_id
        WHERE m.created_at>=? AND m.created_at<?
        ORDER BY a.message_id, a.id
        """,
        (start_ms, end_ms),
    ).fetchall()
    attachments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in attachment_rows:
        attachments[str(row["message_id"])].append(
            {
                "file_key": row["file_key"],
                "resource_type": row["resource_type"],
                "filename": row["filename"],
                "mime_type": row["mime_type"],
                "byte_size": row["byte_size"],
                "sha256": row["sha256"],
                "status": row["status"],
            }
        )

    evidence: list[dict[str, Any]] = []
    unknown_direction = 0
    suppressed = 0
    for row in rows:
        if bool(row["deleted"]) or bool(row["recalled"]):
            suppressed += 1
            continue
        sender_id = str(row["sender_id"] or "")
        if current_user_id:
            direction = "sent" if sender_id == current_user_id else "received"
        else:
            direction = "unknown"
            unknown_direction += 1
        chat_id = str(row["chat_id"])
        thread_id = str(row["thread_id"] or row["root_id"] or "")
        thread_key = f"chat:{chat_id}:thread:{thread_id}" if thread_id else f"chat:{chat_id}"
        message_id = str(row["message_id"])
        evidence.append(
            {
                "evidence_id": f"chat:{chat_id}/{message_id}",
                "source_kind": "chat",
                "source_id": message_id,
                "thread_key": thread_key,
                "title": str(row["conversation_name"] or chat_id),
                "occurred_at": int(row["created_at"]),
                "direction": direction,
                "text": str(row["body_text"] or ""),
                "metadata": {
                    "chat_id": chat_id,
                    "thread_id": row["thread_id"],
                    "parent_id": row["parent_id"],
                    "root_id": row["root_id"],
                    "message_type": row["message_type"],
                    "sender_id": row["sender_id"],
                    "sender_type": row["sender_type"],
                    "sender_name": row["sender_name"],
                    "updated_at": row["updated_at"],
                    "deleted": bool(row["deleted"]),
                    "recalled": bool(row["recalled"]),
                    "chat_mode": row["chat_mode"],
                    "chat_type": row["chat_type"],
                    "external": bool(row["external"] or 0),
                    "attachments": attachments.get(message_id, []),
                },
                "citation": f"chat:{chat_id}/{message_id}",
            }
        )
    warnings = []
    if unknown_direction:
        warnings.append("聊天档案缺少 current_user_open_id，消息方向标记为 unknown")
    if suppressed:
        warnings.append(f"已排除 {suppressed} 条已删除或已撤回聊天，不发送给模型")
    return evidence, warnings


def _extract_mail(
    connection: sqlite3.Connection,
    window: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    start_ms, end_ms = int(window["start_ms"]), int(window["end_ms"])
    predicate = "((m.send_date>=? AND m.send_date<?) OR (m.received_date>=? AND m.received_date<?))"
    params = (start_ms, end_ms, start_ms, end_ms)
    rows = connection.execute(
        f"""
        SELECT m.id, m.mailbox_id, m.provider_message_id, m.thread_id,
               m.smtp_message_id, m.subject, m.sender_name, m.sender_address,
               m.send_date, m.received_date, m.message_state, m.priority,
               m.has_attachment, m.body_plain_text,
               mb.primary_email_address AS mailbox_address,
               mb.display_name AS mailbox_name
        FROM messages m JOIN mailboxes mb ON mb.id=m.mailbox_id
        WHERE m.tombstoned_at IS NULL AND {predicate}
        ORDER BY COALESCE(m.received_date, m.send_date), m.provider_message_id
        """,
        params,
    ).fetchall()

    relations: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"folders": [], "recipients": [], "labels": [], "attachments": []}
    )
    for row in connection.execute(
        f"""
        SELECT mf.message_id, f.provider_folder_id, f.name, f.folder_type, f.status
        FROM message_folders mf JOIN folders f ON f.id=mf.folder_id
        JOIN messages m ON m.id=mf.message_id
        WHERE m.tombstoned_at IS NULL AND {predicate}
        ORDER BY mf.message_id, f.provider_folder_id
        """,
        params,
    ):
        relations[int(row["message_id"])]["folders"].append(
            {
                "provider_folder_id": row["provider_folder_id"],
                "name": row["name"],
                "folder_type": row["folder_type"],
                "status": row["status"],
            }
        )
    for row in connection.execute(
        f"""
        SELECT r.message_id, r.role, r.position, r.display_name, r.address,
               r.normalized_address, r.provider_id
        FROM recipients r JOIN messages m ON m.id=r.message_id
        WHERE r.role<>'bcc' AND m.tombstoned_at IS NULL AND {predicate}
        ORDER BY r.message_id, r.role, r.position
        """,
        params,
    ):
        relations[int(row["message_id"])]["recipients"].append(
            {
                "role": row["role"],
                "position": row["position"],
                "display_name": row["display_name"],
                "address": row["address"],
                "normalized_address": row["normalized_address"],
                "provider_id": row["provider_id"],
            }
        )
    for row in connection.execute(
        f"""
        SELECT l.message_id, l.provider_label_id, l.name, l.label_type
        FROM labels l JOIN messages m ON m.id=l.message_id
        WHERE m.tombstoned_at IS NULL AND {predicate}
        ORDER BY l.message_id, l.provider_label_id
        """,
        params,
    ):
        relations[int(row["message_id"])]["labels"].append(
            {
                "provider_label_id": row["provider_label_id"],
                "name": row["name"],
                "label_type": row["label_type"],
            }
        )
    for row in connection.execute(
        f"""
        SELECT a.message_id, a.provider_attachment_id, a.filename,
               a.content_type, a.declared_byte_size, a.byte_size, a.is_inline,
               a.disposition, a.status, b.sha256
        FROM attachments a
        JOIN messages m ON m.id=a.message_id
        LEFT JOIN blobs b ON b.id=a.blob_id
        WHERE m.tombstoned_at IS NULL AND {predicate}
        ORDER BY a.message_id, a.id
        """,
        params,
    ):
        relations[int(row["message_id"])]["attachments"].append(
            {
                "provider_attachment_id": row["provider_attachment_id"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "declared_byte_size": row["declared_byte_size"],
                "byte_size": row["byte_size"],
                "is_inline": bool(row["is_inline"]),
                "disposition": row["disposition"],
                "status": row["status"],
                "sha256": row["sha256"],
            }
        )

    evidence: list[dict[str, Any]] = []
    counts = {"mail_received": 0, "mail_sent": 0}
    excluded_drafts = 0
    unknown_direction = 0
    for row in rows:
        message_id = int(row["id"])
        relation = relations[message_id]
        direction, excluded = _mail_direction(row, relation["folders"], relation["labels"], relation["recipients"])
        if excluded:
            excluded_drafts += 1
            continue
        timestamp = row["send_date"] if direction == "sent" else row["received_date"]
        if timestamp is None:
            timestamp = row["received_date"] if direction == "sent" else row["send_date"]
        occurred_at = int(timestamp or 0)
        if not start_ms <= occurred_at < end_ms:
            continue
        if direction == "sent":
            counts["mail_sent"] += 1
        elif direction == "received":
            counts["mail_received"] += 1
        else:
            unknown_direction += 1
        folder_markers = _folder_markers(relation["folders"])
        provider_message_id = str(row["provider_message_id"])
        mailbox_id = int(row["mailbox_id"])
        thread_id = str(row["thread_id"] or provider_message_id)
        evidence.append(
            {
                "evidence_id": f"mail:{mailbox_id}/{provider_message_id}",
                "source_kind": "mail",
                "source_id": provider_message_id,
                "thread_key": f"mail:{mailbox_id}:thread:{thread_id}",
                "title": str(row["subject"] or "（无主题）"),
                "occurred_at": occurred_at,
                "direction": direction,
                "text": str(row["body_plain_text"] or ""),
                "metadata": {
                    "mailbox_id": mailbox_id,
                    "mailbox_address": row["mailbox_address"],
                    "mailbox_name": row["mailbox_name"],
                    "thread_id": row["thread_id"],
                    "smtp_message_id": row["smtp_message_id"],
                    "sender": {
                        "name": row["sender_name"],
                        "address": row["sender_address"],
                    },
                    "recipients": relation["recipients"],
                    "send_date": row["send_date"],
                    "received_date": row["received_date"],
                    "message_state": row["message_state"],
                    "priority": row["priority"],
                    "has_attachment": bool(row["has_attachment"]),
                    "folders": relation["folders"],
                    "labels": relation["labels"],
                    "attachments": relation["attachments"],
                    "flags": {
                        "spam": bool(folder_markers & {"spam"}),
                        "trash": bool(folder_markers & {"trash"}),
                    },
                },
                "citation": f"mail:{mailbox_id}/{provider_message_id}",
            }
        )
    warnings = []
    if excluded_drafts:
        warnings.append(f"已排除 {excluded_drafts} 封草稿或定时发送邮件")
    if unknown_direction:
        warnings.append(f"有 {unknown_direction} 封邮件无法可靠判定收发方向")
    return evidence, counts, warnings


def _mail_direction(
    row: Mapping[str, Any] | sqlite3.Row,
    folders: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    recipients: Sequence[Mapping[str, Any]],
) -> tuple[str, bool]:
    markers = _folder_markers(folders)
    label_markers = {
        str(marker).strip().lower()
        for value in labels
        for marker in (
            value.get("provider_label_id"),
            value.get("name"),
            value.get("label_type"),
        )
        if marker
    }
    if "sent" in markers:
        return "sent", False
    if markers & {"draft", "scheduled"} or label_markers & {"draft", "scheduled"}:
        return "unknown", True
    mailbox = str(row["mailbox_address"] or "").strip().lower()
    sender = str(row["sender_address"] or "").strip().lower()
    if mailbox and sender == mailbox:
        return "sent", False
    if markers & {"inbox", "spam"}:
        return "received", False
    if mailbox and sender and sender != mailbox:
        return "received", False
    if mailbox and any(
        str(value.get("normalized_address") or value.get("address") or "").strip().lower() == mailbox
        and str(value.get("role") or "") in {"to", "cc"}
        for value in recipients
    ):
        return "received", False
    return "unknown", False


def _folder_markers(folders: Sequence[Mapping[str, Any]]) -> set[str]:
    aliases = {
        "inbox": "inbox",
        "sent": "sent",
        "draft": "draft",
        "scheduled": "scheduled",
        "spam": "spam",
        "trash": "trash",
    }
    result: set[str] = set()
    for folder in folders:
        for key in ("provider_folder_id", "folder_type", "name"):
            value = str(folder.get(key) or "").strip().lower()
            if value in aliases:
                result.add(aliases[value])
    return result


def _extract_wiki(
    connection: sqlite3.Connection,
    window: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = connection.execute(
        """
        SELECT n.node_token, n.space_id, n.obj_token, n.obj_type,
               n.parent_node_token, n.node_type, n.title AS node_title, n.path,
               n.obj_create_time, n.obj_edit_time, n.node_create_time,
               n.creator, n.owner,
               s.name AS space_name,
               d.title AS document_title, d.revision_id, d.source_edit_time,
               d.content_text, d.content_sha256, d.status AS document_status,
               d.last_synced_at AS document_synced_at
        FROM wiki_nodes n
        LEFT JOIN wiki_spaces s ON s.space_id=n.space_id
        LEFT JOIN wiki_documents d ON d.obj_token=n.obj_token
        WHERE n.status<>'missing'
        ORDER BY n.obj_token,
                 CASE WHEN n.node_type='origin' THEN 0 ELSE 1 END,
                 n.path, n.node_token
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["obj_token"])].append(row)

    evidence: list[dict[str, Any]] = []
    counts = {"wiki_created": 0, "wiki_edited": 0}
    for obj_token in sorted(grouped):
        nodes = grouped[obj_token]
        representative = nodes[0]
        object_creation_candidates = [
            value
            for node in nodes
            for value in (_timestamp_ms(node["obj_create_time"]),)
            if value is not None
        ]
        node_creation_candidates = [
            value
            for node in nodes
            for value in (_timestamp_ms(node["node_create_time"]),)
            if value is not None
        ]
        creation_candidates = object_creation_candidates or node_creation_candidates
        edit_candidates = [
            value
            for node in nodes
            for value in (_timestamp_ms(node["source_edit_time"]), _timestamp_ms(node["obj_edit_time"]))
            if value is not None
        ]
        created_at = min(creation_candidates) if creation_candidates else None
        edited_at = max(edit_candidates) if edit_candidates else None
        created_on_day = created_at is not None and _within(created_at, window)
        edited_on_day = edited_at is not None and _within(edited_at, window) and not (
            created_on_day and edited_at is not None and edited_at == created_at
        )
        if not created_on_day and not edited_on_day:
            continue
        if created_on_day:
            counts["wiki_created"] += 1
        if edited_on_day:
            counts["wiki_edited"] += 1
        events = [name for name, included in (("created", created_on_day), ("edited", edited_on_day)) if included]
        occurred_at = max(
            value
            for value in (
                created_at if created_on_day else None,
                edited_at if edited_on_day else None,
            )
            if value is not None
        )
        node_tokens = sorted({str(node["node_token"]) for node in nodes})
        evidence.append(
            {
                "evidence_id": f"wiki:{obj_token}",
                "source_kind": "wiki",
                "source_id": obj_token,
                "thread_key": f"wiki:{obj_token}",
                "title": str(representative["document_title"] or representative["node_title"] or obj_token),
                "occurred_at": occurred_at,
                "direction": "created" if created_on_day else "edited",
                "text": str(representative["content_text"] or ""),
                "metadata": {
                    "events": events,
                    "created_at": created_at,
                    "edited_at": edited_at,
                    "obj_type": representative["obj_type"],
                    "revision_id": representative["revision_id"],
                    "content_sha256": representative["content_sha256"],
                    "document_status": representative["document_status"],
                    "document_synced_at": representative["document_synced_at"],
                    "space_id": representative["space_id"],
                    "space_name": representative["space_name"],
                    "node_token": representative["node_token"],
                    "node_tokens": node_tokens,
                    "parent_node_token": representative["parent_node_token"],
                    "path": representative["path"],
                    "creator": representative["creator"],
                    "owner": representative["owner"],
                },
                "citation": f"wiki:{obj_token}",
            }
        )
    return evidence, counts


def _latest_chat_sync(connection: sqlite3.Connection) -> dict[str, Any] | None:
    candidates = [
        value
        for value in (
            _latest_sync_row(connection, "sync_jobs", "chat_job"),
            _latest_sync_row(connection, "sync_runs", "chat_run"),
        )
        if value is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda value: (int(value.get("started_at") or 0), int(value.get("id") or 0)))


def _latest_sync_row(
    connection: sqlite3.Connection,
    table: str,
    record_type: str,
) -> dict[str, Any] | None:
    allowed_tables = {"sync_jobs", "sync_runs", "wiki_sync_runs"}
    if table not in allowed_tables:
        raise ValueError("unsupported sync table")
    row = connection.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    allowed = {
        "id",
        "trigger",
        "started_at",
        "finished_at",
        "window_start",
        "window_end",
        "requested_days",
        "status",
        "error",
        "conversations_discovered",
        "new_conversations",
        "folders_seen",
        "windows_scanned",
        "pages_scanned",
        "message_ids_seen",
        "messages_seen",
        "messages_written",
        "documents_seen",
        "documents_written",
        "nodes_seen",
        "spaces_seen",
        "attachments_seen",
        "attachments_downloaded",
        "attachments_skipped",
        "assets_downloaded",
        "assets_skipped",
        "bytes_downloaded",
        "events_processed",
    }
    result = {key: row[key] for key in row.keys() if key in allowed}
    result["record_type"] = record_type
    return result


def _timestamp_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    number = int(value)
    return number if abs(number) >= 100_000_000_000 else number * 1000


def _within(timestamp_ms: int, window: Mapping[str, Any]) -> bool:
    return int(window["start_ms"]) <= timestamp_ms < int(window["end_ms"])


def _evidence_sort_key(value: Mapping[str, Any]) -> tuple[int, str, str, str]:
    return (
        int(value.get("occurred_at") or 0),
        str(value.get("source_kind") or ""),
        str(value.get("thread_key") or ""),
        str(value.get("source_id") or ""),
    )


def _render_evidence(value: Mapping[str, Any]) -> str:
    header = {
        "citation": str(value.get("citation") or ""),
        "direction": str(value.get("direction") or ""),
        "occurred_at": int(value.get("occurred_at") or 0),
        "source_id": str(value.get("source_id") or ""),
        "source_kind": str(value.get("source_kind") or ""),
        "title": str(value.get("title") or ""),
    }
    return f"{json.dumps(header, ensure_ascii=False, sort_keys=True)}\n{str(value.get('text') or '')}"


def _stable_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
