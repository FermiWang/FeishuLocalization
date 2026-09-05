"""Subtitle uploads retain spoken content and evidence timing without speakers."""
from pathlib import Path

import pytest

from app.sources import (
    decode_text,
    extract_transcript,
    input_hash,
    parse_srt,
    source_type_for_filename,
    srt_timeline,
    stable_fragments,
)


SAMPLE = (
    "1\n00:00:22,725 --> 00:00:22,725\n"
    "Welcome, everyone.\n\n"
    "2\n00:00:24,245 --> 00:00:26,125\n"
    "Thank you for joining\nour session today.\n"
)


def test_srt_cues_preserve_multiline_zero_duration_and_order():
    cues = parse_srt(SAMPLE)
    assert cues == [
        {"index": 1, "start_ms": 22725, "end_ms": 22725, "text": "Welcome, everyone."},
        {"index": 2, "start_ms": 24245, "end_ms": 26125,
         "text": "Thank you for joining\nour session today."},
    ]
    assert srt_timeline(SAMPLE) == cues
    assert all("speaker" not in cue for cue in cues)


@pytest.mark.parametrize("name", ["meeting.srt", "meeting.SRT", "meeting.SrT"])
def test_srt_is_transcript(name):
    assert source_type_for_filename(name) == "transcript"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
def test_srt_decoding_handles_bom_crlf_and_chinese(tmp_path: Path, encoding):
    raw = "1\r\n00:00:01,000 --> 00:00:02,000\r\n这是会议识别稿。\r\n"
    source = tmp_path / "meeting.SRT"
    source.write_bytes(raw.encode(encoding))
    assert extract_transcript(source) == "[00:00:01.000 --> 00:00:02.000]\n这是会议识别稿。"


def test_no_bom_chinese_is_not_mistaken_for_utf16():
    assert decode_text("会议记录".encode("gb18030")) == "会议记录"


def test_invalid_srt_encoding_does_not_silently_replace_spoken_content(tmp_path: Path):
    source = tmp_path / "damaged.srt"
    source.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\n\x81\n")
    with pytest.raises(ValueError, match="文字编码无效"):
        extract_transcript(source)


def test_srt_style_is_removed_but_escaped_html_is_content():
    cues = parse_srt(
        '1\n00:00:01.000 --> 00:00:02.000\n{\\an8}<font color="red"><b>Revenue '
        '&amp; cost</b></font><br />Use &lt;b&gt; literally; x &lt; 5.\n'
    )
    assert cues[0]["text"] == "Revenue & cost\nUse <b> literally; x < 5."


@pytest.mark.parametrize(("raw", "message"), [
    ("", "没有可读取"),
    ("1\n00:00:00,000 --> 00:00:01,000", "缺少时间轴或字幕正文"),
    ("Text without subtitle timing", "字幕序号"),
    ("1\n00:61:00,000 --> 00:61:01,000\nHello", "时间格式无效"),
    ("1\n00:00:00,00 --> 00:00:01,000\nHello", "时间格式无效"),
    ("1\n00:00:02,000 --> 00:00:01,000\nHello", "结束时间早于开始时间"),
    ("1\n00:00:01,000 --> 00:00:02,000\n<i></i>", "没有可读取的字幕正文"),
    ("1\n00:00:01,000 --> 00:00:02,000\nA\n2\n00:00:02,000 --> 00:00:03,000\nB",
     "缺少空行分隔"),
])
def test_damaged_srt_fails_with_actionable_error(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_srt(raw)


def test_srt_extraction_drops_indexes_without_dropping_spoken_numbers(tmp_path: Path):
    source = tmp_path / "meeting.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\n301\n2026 policy discussion.\n",
                      encoding="utf-8")
    assert extract_transcript(source) == (
        "[00:00:01.000 --> 00:00:02.000]\n301\n2026 policy discussion."
    )


def test_fragments_retain_timestamps_for_sentence_and_oversized_cues(tmp_path: Path):
    source = tmp_path / "meeting.srt"
    body = "第一句。第二句。" * 18
    source.write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{body}\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nFinal statement.\n", encoding="utf-8",
    )
    segment = {"source_id": 3, "position": 0, "speaker_labeled": False,
               "text": extract_transcript(source)}
    fragments = stable_fragments([segment], max_chars=90)
    assert fragments == stable_fragments([segment], max_chars=90)
    assert [f["id"] for f in fragments] == [f"S{i:03d}" for i in range(1, len(fragments) + 1)]
    assert all(f["text"].startswith("[00:00:") for f in fragments)
    assert all(len(f["text"]) <= 90 for f in fragments)
    assert all(not f["speaker_labeled"] for f in fragments)
    assert "".join(f["text"].split("\n", 1)[1] for f in fragments[:-1]) == body
    assert fragments[-1]["text"] == "[00:00:02.000 --> 00:00:03.000]\nFinal statement."


def test_plain_transcript_fragment_behavior_is_preserved():
    segments = [{"source_id": 7, "position": 0, "speaker_labeled": False,
                 "text": "第一句话。第二句话。"}]
    assert stable_fragments(segments, max_chars=5) == [
        {"id": "S001", "source_id": 7, "speaker_labeled": False, "text": "第一句话。"},
        {"id": "S002", "source_id": 7, "speaker_labeled": False, "text": "第二句话。"},
    ]


def test_multiple_short_cues_respect_fragment_boundaries_including_separators():
    # Each cue is 34 chars; combining two also needs one newline.
    cue = "[00:00:01.000 --> 00:00:02.000]\n1234"
    fragments = stable_fragments([
        {"source_id": 1, "position": 0, "speaker_labeled": False,
         "text": cue + "\n\n" + cue},
    ], max_chars=len(cue) * 2)
    assert len(fragments) == 2
    assert all(f["text"] == cue for f in fragments)


def test_background_pages_invalidate_only_semantic_input_changes():
    meeting = {"title": "Test", "meeting_date": "2026-09-05", "sources": []}
    legacy_hash = input_hash(meeting)
    assert input_hash({**meeting, "background_pages": []}) == legacy_hash
    page = {"url": "https://example.com/event", "status": "ready", "content_hash": "first"}
    current_hash = input_hash({**meeting, "background_pages": [page]})
    assert current_hash != legacy_hash
    assert input_hash({**meeting, "background_pages": [
        {**page, "fetched_at": "tomorrow", "error": "historical transient error"},
    ]}) == current_hash
    assert input_hash({**meeting, "background_pages": [
        {**page, "content_hash": "updated"},
    ]}) != current_hash
