from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .config import ArchivePaths
from .database import ArchiveDatabase


def seed_demo(database: ArchiveDatabase, paths: ArchivePaths) -> dict[str, int]:
    now = int(time.time() * 1000)
    day = 86400000
    conversations = [
        {
            "chat_id": "demo_p2p",
            "name": "PoC 单聊",
            "chat_mode": "p2p",
            "chat_type": "private",
        },
        {
            "chat_id": "demo_internal",
            "name": "PoC 内部群",
            "chat_mode": "group",
            "chat_type": "private",
        },
        {
            "chat_id": "demo_external",
            "name": "PoC 外部群",
            "chat_mode": "group",
            "chat_type": "private",
            "external": True,
        },
    ]
    for conversation in conversations:
        database.upsert_conversation(conversation)

    messages = [
        _message(
            "demo_msg_1",
            "demo_p2p",
            "王小明",
            "text",
            "这是通过官方接口同步的单聊示例。",
            now - 5 * day,
        ),
        _message(
            "demo_msg_2",
            "demo_p2p",
            "我",
            "text",
            "可以按日期、人员和类型筛选，也可以全文搜索。",
            now - 5 * day + 60000,
            updated_at=now - 4 * day,
        ),
        _message(
            "demo_msg_3",
            "demo_internal",
            "项目机器人",
            "post",
            "30 天覆盖率 PoC 已开始：验证文本、话题、编辑、图片和文件。",
            now - 3 * day,
            thread_id="demo_thread_1",
        ),
        _message(
            "demo_msg_4",
            "demo_internal",
            "李雷",
            "text",
            "这是一条通过 thread 容器补充同步的话题回复。",
            now - 3 * day + 120000,
            thread_id="demo_thread_1",
            parent_id="demo_msg_3",
            root_id="demo_msg_3",
        ),
        _message(
            "demo_msg_5",
            "demo_internal",
            "韩梅梅",
            "image",
            "[图片] 话题中的图片资源示例",
            now - 2 * day,
        ),
        _message(
            "demo_msg_6",
            "demo_external",
            "外部协作方",
            "file",
            "PoC 覆盖率核对说明.txt",
            now - day,
        ),
        _message(
            "demo_msg_7",
            "demo_external",
            "系统",
            "text",
            "此消息已撤回；官方接口不可见的内容无法事后恢复。",
            now - day + 60000,
            recalled=True,
        ),
    ]
    written = sum(int(database.upsert_message(message)) for message in messages)

    attachment_id = database.ensure_attachment(
        "demo_msg_6", "demo_file_key", "file", "PoC 覆盖率核对说明.txt"
    )
    relative = Path("attachments") / "demo_external" / f"{attachment_id}-coverage.txt"
    target = paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (
        "Feishu Archive PoC\n"
        "\n"
        "此文件由 demo 命令在本地生成，用于验证附件的离线打开链路。\n"
        "真实附件只会通过飞书官方资源接口下载。\n"
    ).encode("utf-8")
    target.write_bytes(payload)
    os.chmod(target, 0o600)
    database.update_attachment(
        attachment_id,
        mime_type="text/plain; charset=utf-8",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        local_path=str(relative),
        status="downloaded",
        error=None,
        downloaded_at=now,
    )
    return {"conversations": len(conversations), "messages_written": written, "attachments": 1}


def _message(
    message_id: str,
    chat_id: str,
    sender_name: str,
    message_type: str,
    body_text: str,
    created_at: int,
    *,
    updated_at: int | None = None,
    thread_id: str | None = None,
    parent_id: str | None = None,
    root_id: str | None = None,
    recalled: bool = False,
) -> dict[str, object]:
    raw = {
        "message_id": message_id,
        "chat_id": chat_id,
        "msg_type": message_type,
        "create_time": str(created_at),
        "update_time": str(updated_at or created_at),
        "sender": {"id": f"demo_{sender_name}", "sender_type": "user", "name": sender_name},
        "body": {"content": json.dumps({"text": body_text}, ensure_ascii=False)},
        "thread_id": thread_id,
        "parent_id": parent_id,
        "root_id": root_id,
        "recalled": recalled,
    }
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "parent_id": parent_id,
        "root_id": root_id,
        "message_type": message_type,
        "sender_id": f"demo_{sender_name}",
        "sender_type": "user",
        "sender_name": sender_name,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "deleted": False,
        "recalled": recalled,
        "body_text": body_text,
        "raw_json": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
    }
