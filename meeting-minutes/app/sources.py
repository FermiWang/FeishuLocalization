"""Meeting source validation, DOCX/TXT/SRT extraction and deterministic assembly."""
from __future__ import annotations

import hashlib
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterator

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".mp4"}
TRANSCRIPT_EXTENSIONS = {".txt", ".text", ".docx", ".srt"}
MAX_DOCX_ENTRIES = 5_000
MAX_DOCX_UNCOMPRESSED_BYTES = 200 * 1024**2
MAX_EXTRACTED_TEXT_CHARS = 50 * 1024**2
_SRT_TIMESTAMP = r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})"
_SRT_TIMING = re.compile(rf"^{_SRT_TIMESTAMP}[ \t]*-->[ \t]*{_SRT_TIMESTAMP}$")
_SRT_MARKER = re.compile(
    r"^\[\d{2,}:[0-5]\d:[0-5]\d\.\d{3} --> "
    r"\d{2,}:[0-5]\d:[0-5]\d\.\d{3}\]$", re.MULTILINE
)


def _source_dedup_key(source: dict[str, Any]) -> tuple[str, str, str, int]:
    """Return the exact-content identity used only for effective processing.

    Pairing stays part of the identity because byte-identical transcripts may
    legitimately be aligned to different recordings.  Raw source rows are
    never deleted by this helper.
    """
    return (
        str(source.get("source_type") or ""),
        str(source.get("sha256") or ""),
        str(source.get("pair_key") or "").strip(),
        int(bool(source.get("generated"))),
    )


def annotate_duplicate_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate later exact duplicates while preserving every raw source row."""
    first_by_key: dict[tuple[str, str, str, int], int] = {}
    result: list[dict[str, Any]] = []
    for source in sorted(sources, key=lambda item: (item.get("position", 0), item.get("id", 0))):
        item = dict(source)
        key = _source_dedup_key(item)
        duplicate_of = first_by_key.get(key) if key[1] else None
        item["duplicate_of_source_id"] = duplicate_of
        if duplicate_of is None:
            first_by_key[key] = int(item["id"])
        result.append(item)
    return result


def effective_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ordered sources that materially participate in processing."""
    return [
        source for source in annotate_duplicate_sources(sources)
        if source.get("duplicate_of_source_id") is None
    ]


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-()（）\u4e00-\u9fff]", "_", Path(name).name)
    return cleaned[:180] or "source"


def source_type_for_filename(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in TRANSCRIPT_EXTENSIONS:
        return "transcript"
    raise ValueError("支持音频 mp3/m4a/wav/aac/flac/mp4，以及识别稿 TXT/DOCX/SRT")


def decode_text(raw: bytes, *, strict: bool = False) -> str:
    # UTF-16 accepts many even-length GB18030 byte strings without raising.
    # Only a BOM may select it; otherwise Chinese transcripts can turn into
    # plausible but unrelated Unicode characters.
    try:
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            return raw.decode("utf-32")
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16")
    except UnicodeDecodeError as exc:
        raise ValueError("识别稿文字编码无效，请保存为 UTF-8 后重新上传") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    if strict:
        raise ValueError("识别稿文字编码无效，请保存为 UTF-8 后重新上传")
    return raw.decode("utf-8", errors="replace")


def _srt_text(text: str) -> str:
    """Remove display-only subtitle styling, without interpreting quoted HTML."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(
        r"</?(?:b|i|u|s|strike|font|span|ruby|rt|c(?:\.[\w-]+)*)(?:\s[^<>]*)?>",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\{\\[^{}]*\}", "", text)
    # Unescape last: &lt;b&gt; is quoted text, not a formatting instruction.
    return html.unescape(text).strip()


def _srt_milliseconds(parts: tuple[str, ...]) -> int:
    hours, minutes, seconds, milliseconds = map(int, parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _srt_time_label(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_srt(text: str) -> list[dict[str, Any]]:
    """Parse ordered SRT cues as index/start_ms/end_ms/text dictionaries.

    Times are integer milliseconds. Cue order and multi-line text are retained;
    zero-duration cues are valid and no speaker identity is inferred.
    """
    if len(text) > MAX_EXTRACTED_TEXT_CHARS:
        raise ValueError("识别稿提取后的文字超过安全限制")
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("SRT 字幕文件没有可读取的内容")
    cues: list[dict[str, Any]] = []
    for position, block in enumerate(re.split(r"\n[ \t]*\n+", normalized), start=1):
        lines = block.strip().split("\n")
        if not re.fullmatch(r"[0-9]+", lines[0].strip()) or int(lines[0].strip()) < 1:
            raise ValueError(f"SRT 第 {position} 段缺少有效的字幕序号")
        if len(lines) < 3:
            raise ValueError(f"SRT 第 {position} 段缺少时间轴或字幕正文")
        timing = _SRT_TIMING.fullmatch(lines[1].strip())
        if timing is None:
            raise ValueError(
                f"SRT 第 {position} 段时间格式无效，应为 00:00:00,000 --> 00:00:01,000"
            )
        start_ms = _srt_milliseconds(timing.groups()[:4])
        end_ms = _srt_milliseconds(timing.groups()[4:])
        if end_ms < start_ms:
            raise ValueError(f"SRT 第 {position} 段结束时间早于开始时间")
        body = _srt_text("\n".join(lines[2:]))
        if not body:
            raise ValueError(f"SRT 第 {position} 段没有可读取的字幕正文")
        # A missing blank separator must not silently swallow the next cue's
        # index and timing line into this cue's spoken content.
        if any(_SRT_TIMING.fullmatch(line.strip()) for line in lines[2:]):
            raise ValueError(f"SRT 第 {position} 段字幕之间缺少空行分隔")
        cues.append({
            "index": int(lines[0].strip()), "start_ms": start_ms,
            "end_ms": end_ms, "text": body,
        })
    return cues


def srt_timeline(text: str) -> list[dict[str, Any]]:
    """Return the raw SRT timeline; millisecond units match FunASR segments."""
    return parse_srt(text)


def _render_srt(cues: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[{_srt_time_label(cue['start_ms'])} --> {_srt_time_label(cue['end_ms'])}]\n{cue['text']}"
        for cue in cues
    )


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
    elif source.suffix.lower() == ".srt":
        text = _render_srt(parse_srt(decode_text(source.read_bytes(), strict=True)))
    else:
        text = decode_text(source.read_bytes())
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("识别稿没有可读取的文字")
    if len(text) > MAX_EXTRACTED_TEXT_CHARS:
        raise ValueError("识别稿提取后的文字超过安全限制")
    return text


def input_hash(meeting: dict[str, Any]) -> str:
    sources = effective_sources(list(meeting.get("sources", [])))
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
            for source in sources
        ],
    }
    if meeting.get("background_pages"):
        payload["background_pages"] = [
            {"url": page.get("url") or "", "status": page.get("status") or "",
             "content_hash": page.get("content_hash") or ""}
            for page in meeting["background_pages"]
        ]
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
    if max_chars <= 0:
        raise ValueError("片段长度必须大于零")
    fragments: list[dict[str, Any]] = []
    number = 1
    for segment in sorted(segments, key=lambda item: (item["position"], item["source_id"])):
        text = segment["text"]
        markers = list(_SRT_MARKER.finditer(text))
        timestamped = bool(markers and markers[0].start() == 0)
        if timestamped:
            paragraphs = []
            for index, marker in enumerate(markers):
                stop = markers[index + 1].start() if index + 1 < len(markers) else len(text)
                body = text[marker.end():stop].strip()
                prefix = marker.group() + "\n"
                available = max_chars - len(prefix)
                if available <= 0:
                    raise ValueError("片段长度不足以保留字幕时间信息")
                # Every piece of an oversized cue keeps its own timestamp;
                # sentence splitting cannot detach a statement from its cue.
                paragraphs.extend(prefix + body[offset:offset + available]
                                  for offset in range(0, len(body), available))
        else:
            paragraphs = [part.strip() for part in re.split(r"\n{2,}|(?<=[。！？!?])\s*", text)
                          if part.strip()]
        buffer: list[str] = []
        size = 0
        for paragraph in paragraphs:
            separator_chars = len(buffer) if timestamped else 0
            if buffer and size + len(paragraph) + separator_chars > max_chars:
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
