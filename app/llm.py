"""调用本地 OpenAI 兼容端点，把讯飞转写整理为结构化会议纪要。"""
import os

import httpx

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11435/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "vmlx/qwen3.8-27b-8bit")

# 超过该字符数走 map-reduce：先逐块提取要点，再合并成终稿
CHUNK_THRESHOLD = 20000
CHUNK_SIZE = 8000

SECTIONS = ["会议纪要", "主要参会人员观点", "观点冲突点",
            "会议达成一致", "会议未达成一致", "会议待办"]

SYSTEM_PROMPT = """你是一位资深会议秘书。根据用户提供的会议资料和讯飞语音转写全文，整理出结构化的会议记录。

输出要求：
1. 使用中文 Markdown，且只输出以下六个二级标题板块，标题名称一字不差，顺序固定：
## 会议纪要
## 主要参会人员观点
## 观点冲突点
## 会议达成一致
## 会议未达成一致
## 会议待办
2. 「会议纪要」：按议题梳理讨论过程与结论，条理清晰。
3. 「主要参会人员观点」：按发言人逐条列出其核心观点，格式「**姓名**：观点」。转写中发言人标识不清时，按上下文合理推断；无法推断可不署名。
4. 若转写文本按「发言人N：」分段（声纹识别聚类结果，同一编号为同一人）：请结合参会人名单与发言内容（自称、他称、被指名回应等）推断每个编号对应的姓名，推断可靠时用真名署名，不可靠时保留「发言人N」署名；同一编号的观点必须归为同一人。
5. 「观点冲突点」：列出参会人之间存在分歧的观点及各方立场；没有则写「无明显冲突」。
6. 「会议达成一致」：列出会议中明确达成共识的事项；没有则写「无」。
7. 「会议未达成一致」：列出讨论了但未形成结论、悬而未决的事项；没有则写「无」。
8. 「会议待办」：用 `- [ ]` 复选框格式逐条列出后续行动项，尽量标注负责人和时限。
9. 只依据转写内容整理，不要编造转写中没有的信息。不要输出六个板块以外的内容。"""

CHUNK_PROMPT = """你是会议记录助手。下面是会议转写的一部分（共 {total} 块，这是第 {idx} 块）。
请提取本块要点：讨论了什么议题、每位发言人表达了什么观点、出现了哪些分歧、达成了哪些一致、留下了哪些悬而未决的问题、有哪些行动项（含负责人）。
用简洁的中文条目输出，不要遗漏事实。"""


def _chat(messages: list[dict], max_tokens: int = 4096) -> str:
    resp = httpx.post(
        f"{LLM_BASE_URL}/chat/completions",
        json={
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
        timeout=600.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _meeting_meta(meeting: dict) -> str:
    lines = [f"会议主题：{meeting.get('title') or '未命名'}"]
    if meeting.get("meeting_date"):
        lines.append(f"会议时间：{meeting['meeting_date']}")
    if meeting.get("background"):
        lines.append(f"会议背景：{meeting['background']}")
    attendees = meeting.get("attendees") or []
    if attendees:
        people = "、".join(
            f"{a['name']}（{a['role']}）" if a.get("role") else a["name"]
            for a in attendees
        )
        lines.append(f"主要参会人：{people}")
    return "\n".join(lines)


def _split_chunks(text: str) -> list[str]:
    """按段落边界切块，单块不超过 CHUNK_SIZE 字符。"""
    paragraphs = text.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paragraphs:
        if buf and size + len(p) + 1 > CHUNK_SIZE:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 1
    if buf:
        chunks.append("\n".join(buf))
    # 兜底：存在超长单块时硬切
    final: list[str] = []
    for c in chunks:
        while len(c) > CHUNK_SIZE:
            final.append(c[:CHUNK_SIZE])
            c = c[CHUNK_SIZE:]
        final.append(c)
    return final


def organize(meeting: dict, transcript: str) -> str:
    """整理一场会议，返回 Markdown 结果。失败时抛出异常。"""
    if not transcript.strip():
        raise ValueError("尚未提供讯飞识别文字，无法整理")

    meta = _meeting_meta(meeting)

    if len(transcript) <= CHUNK_THRESHOLD:
        body = transcript
    else:
        chunks = _split_chunks(transcript)
        notes = []
        for i, chunk in enumerate(chunks, 1):
            note = _chat([
                {"role": "system", "content": CHUNK_PROMPT.format(
                    total=len(chunks), idx=i)},
                {"role": "user", "content": chunk},
            ])
            notes.append(f"【第 {i} 部分要点】\n{note}")
        body = "（转写较长，以下为分段提取的要点汇总）\n\n" + "\n\n".join(notes)

    return _chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{meta}\n\n以下是会议转写内容：\n\n{body}"},
    ], max_tokens=8192)
