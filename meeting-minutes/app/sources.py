"""Meeting source validation, DOCX/TXT extraction and deterministic input assembly."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterator

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".mp4"}
TRANSCRIPT_EXTENSIONS = {".txt", ".text", ".docx"}
MAX_DOCX_ENTRIES = 5_000
MAX_DOCX_UNCOMPRESSED_BYTES = 200 * 1024**2
MAX_EXTRACTED_TEXT_CHARS = 50 * 1024**2


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-()（）\u4e00-\u9fff]", "_", Path(name).name)
    return cleaned[:180] or "source"


def source_type_for_filename(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in TRANSCRIPT_EXTENSIONS:
        return "transcript"
    raise ValueError("支持音频 mp3/m4a/wav/aac/flac/mp4，以及识别稿 TXT/DOCX")


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _iter_docx_blocks(document: Any) -> Iterator[str]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, document).text.strip()
            if text:
                yield text
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            for row in table.rows:
                cells = [re.sub(r"\s+", " ", cell.text).strip() for cell in row.cells]
                if any(cells):
                    yield " | ".join(cells)


def extract_transcript(path: str | Path) -> str:
    source = Path(path)
    if source.suffix.lower() == ".docx":
        try:
            with zipfile.ZipFile(source) as package:
                entries = package.infolist()
                if (
                    len(entries) > MAX_DOCX_ENTRIES
                    or sum(item.file_size for item in entries) > MAX_DOCX_UNCOMPRESSED_BYTES
                ):
                    raise ValueError("DOCX 解压后的内容超过安全限制")
        except zipfile.BadZipFile as exc:
            raise ValueError("DOCX 文件结构无效") from exc
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError("服务器缺少 python-docx，暂时无法读取 DOCX") from exc
        text = "\n".join(_iter_docx_blocks(Document(source)))
    else:
        text = decode_text(source.read_bytes())
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("识别稿没有可读取的文字")
    if len(text) > MAX_EXTRACTED_TEXT_CHARS:
        raise ValueError("识别稿提取后的文字超过安全限制")
    return text


def input_hash(meeting: dict[str, Any]) -> str:
    payload = {
        "title": meeting.get("title") or "",
        "meeting_date": meeting.get("meeting_date") or "",
        "background": meeting.get("background") or "",
        "attendees": meeting.get("attendees") or [],
        "sources": [
            {
                "id": source["id"],
                "type": source["source_type"],
                "position": source["position"],
                "pair_key": source.get("pair_key") or "",
                "sha256": source["sha256"],
            }
            for source in meeting.get("sources", [])
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def segment_text(source_id: int, position: int, text: str,
                 *, speaker_labeled: bool, timeline: list[dict] | None = None) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "position": position,
        "text": text.strip(),
        "speaker_labeled": speaker_labeled,
        "timeline": timeline or [],
    }


def stable_fragments(segments: list[dict[str, Any]], max_chars: int = 4200) -> list[dict[str, Any]]:
    """Split ordered sources into stable S001-style evidence fragments."""
    fragments: list[dict[str, Any]] = []
    number = 1
    for segment in sorted(segments, key=lambda item: (item["position"], item["source_id"])):
        paragraphs = [part.strip() for part in re.split(r"\n{2,}|(?<=[。！？!?])\s*", segment["text"])
                      if part.strip()]
        buffer: list[str] = []
        size = 0
        for paragraph in paragraphs:
            if buffer and size + len(paragraph) > max_chars:
                fragments.append({
                    "id": f"S{number:03d}", "source_id": segment["source_id"],
                    "text": "\n".join(buffer), "speaker_labeled": segment["speaker_labeled"],
                })
                number += 1
                buffer, size = [], 0
            while len(paragraph) > max_chars:
                if buffer:
                    fragments.append({
                        "id": f"S{number:03d}", "source_id": segment["source_id"],
                        "text": "\n".join(buffer), "speaker_labeled": segment["speaker_labeled"],
                    })
                    number += 1
                    buffer, size = [], 0
                fragments.append({
                    "id": f"S{number:03d}", "source_id": segment["source_id"],
                    "text": paragraph[:max_chars], "speaker_labeled": segment["speaker_labeled"],
                })
                number += 1
                paragraph = paragraph[max_chars:]
            buffer.append(paragraph)
            size += len(paragraph)
        if buffer:
            fragments.append({
                "id": f"S{number:03d}", "source_id": segment["source_id"],
                "text": "\n".join(buffer), "speaker_labeled": segment["speaker_labeled"],
            })
            number += 1
    return fragments
