from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ResourceRef:
    file_key: str
    resource_type: str
    filename: str | None = None


def _walk_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk_text(item)
        return
    if isinstance(value, dict):
        tag = value.get("tag")
        if tag in {"a", "at", "text"}:
            text = value.get("text") or value.get("name") or value.get("user_name")
            if isinstance(text, str) and text.strip():
                yield text.strip()
        for key, item in value.items():
            if key in {"file_key", "image_key", "file_name", "tag", "style"}:
                continue
            yield from _walk_text(item)


def parse_content(message_type: str, content: str | dict[str, Any] | None) -> tuple[str, list[ResourceRef]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        try:
            payload: Any = json.loads(content)
        except json.JSONDecodeError:
            payload = content
    else:
        payload = content

    resources: list[ResourceRef] = []
    if isinstance(payload, dict):
        image_key = payload.get("image_key")
        file_key = payload.get("file_key")
        filename = payload.get("file_name") or payload.get("name")
        if isinstance(image_key, str) and image_key:
            resources.append(ResourceRef(image_key, "image", filename))
        if isinstance(file_key, str) and file_key:
            resources.append(ResourceRef(file_key, "file", filename))

        if message_type == "text":
            return str(payload.get("text", "")), resources
        if message_type in {"file", "audio", "media"}:
            return str(filename or f"[{message_type}]"), resources
        if message_type == "image":
            return "[图片]", resources

    parts = list(dict.fromkeys(_walk_text(payload)))
    return "\n".join(parts), resources


def normalize_message(item: dict[str, Any], fallback_chat_id: str) -> dict[str, Any]:
    body = item.get("body") or {}
    message_type = item.get("msg_type") or item.get("message_type") or "unknown"
    text, resources = parse_content(message_type, body.get("content"))
    sender = item.get("sender") or {}
    return {
        "message_id": item["message_id"],
        "chat_id": item.get("chat_id") or fallback_chat_id,
        "thread_id": item.get("thread_id"),
        "parent_id": item.get("parent_id"),
        "root_id": item.get("root_id"),
        "message_type": message_type,
        "sender_id": sender.get("id"),
        "sender_type": sender.get("sender_type"),
        "sender_name": sender.get("name") or sender.get("sender_name"),
        "created_at": _as_int(item.get("create_time")),
        "updated_at": _as_int(item.get("update_time")),
        "deleted": bool(item.get("deleted")),
        "recalled": bool(item.get("recalled")),
        "body_text": text,
        "raw_json": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
        "resources": resources,
    }


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
