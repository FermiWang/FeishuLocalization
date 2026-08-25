"""FunASR 转写、标点、VAD 与说话人分离 worker（由独立环境执行）。

用法：
    python diarize_worker.py <audio.wav(16k单声道)> <transcript.txt|-> <out.json>

输出 out.json：
    {"speakers": N, "text": "……", "labeled": "发言人1：……", "segments": [...]}
失败时抛出非零退出码，stderr 带原因；调用方负责回退。
"""
import json
import re
import sys
from difflib import SequenceMatcher

PUNCT = "，。！？、；：,.!?;:"


def normalize(text: str) -> str:
    """去掉标点空白，只留可比对字符。"""
    return re.sub(r"[\s" + re.escape(PUNCT) + r"]", "", text)


def align_speakers(iflytek_text: str, asr_segs: list[dict]) -> list[dict]:
    """讯飞原句继承 ASR 句段的说话人标签，返回 [{'speaker': id, 'text': 原句}]。"""
    asr_chars: list[str] = []
    asr_spk: list[int] = []
    for seg in asr_segs:
        for ch in normalize(seg["text"]):
            asr_chars.append(ch)
            asr_spk.append(seg["spk"])

    sentences = [s for s in re.split(r"(?<=[。！？!?；;])\s*", iflytek_text) if s.strip()]

    fk_chars: list[str] = []
    fk_index: list[int] = []
    for i, s in enumerate(sentences):
        for ch in normalize(s):
            fk_chars.append(ch)
            fk_index.append(i)

    sm = SequenceMatcher(None, "".join(fk_chars), "".join(asr_chars), autojunk=False)
    char_spk: list[int | None] = [None] * len(fk_chars)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            for k in range(i2 - i1):
                char_spk[i1 + k] = asr_spk[j1 + k]

    # 未匹配字符顺延前一个已知标签
    last = None
    for k in range(len(char_spk)):
        if char_spk[k] is None:
            char_spk[k] = last
        else:
            last = char_spk[k]

    votes: dict[int, dict[int, int]] = {}
    for k, spk in enumerate(char_spk):
        if spk is not None:
            votes.setdefault(fk_index[k], {}).setdefault(spk, 0)
            votes[fk_index[k]][spk] += 1

    result = []
    for i, s in enumerate(sentences):
        v = votes.get(i)
        spk = max(v, key=v.get) if v else (result[-1]["speaker"] if result else 0)
        result.append({"speaker": spk, "text": s.strip()})
    return result


def render_labeled(items: list[dict]) -> str:
    """相邻同发言人的句子合并为一段，发言人编号从 1 开始展示。"""
    lines: list[str] = []
    cur = None
    for item in items:
        spk = item["speaker"] + 1
        if spk != cur:
            cur = spk
            lines.append(f"\n发言人{spk}：{item['text']}")
        else:
            lines[-1] += item["text"]
    return "\n".join(lines).strip()


def _segments(sentence_info: list[dict]) -> list[dict]:
    result = []
    for item in sentence_info:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        result.append({
            "speaker": int(item.get("spk") or 0),
            "text": text,
            "start_ms": int(item.get("start") or 0),
            "end_ms": int(item.get("end") or 0),
        })
    return result


def main() -> None:
    wav_path, transcript_path, out_path = sys.argv[1:4]

    from funasr import AutoModel  # 延迟导入：仅在 worker 环境可用

    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        spk_model="cam++",
        disable_update=True,
    )
    res = model.generate(input=wav_path, batch_size_s=300)
    first = res[0] if res else {}
    segs = (first.get("sentence_info") if first else None) or []
    if not segs:
        raise RuntimeError("声纹分离无结果（音频可能为空或无声）")

    recognized = str(first.get("text") or "").strip()
    speakers = len({int(s.get("spk") or 0) for s in segs})
    if transcript_path != "-":
        with open(transcript_path, encoding="utf-8") as f:
            transcript = f.read()
        aligned = align_speakers(transcript, segs)
        text = transcript
        labeled = render_labeled(aligned)
    else:
        rendered = _segments(segs)
        text = recognized or "".join(item["text"] for item in rendered)
        labeled = render_labeled(rendered)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "speakers": speakers,
            "text": text,
            "labeled": labeled,
            "segments": _segments(segs),
            "models": {
                "asr": "paraformer-zh", "vad": "fsmn-vad",
                "punc": "ct-punc", "speaker": "cam++",
            },
        }, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
