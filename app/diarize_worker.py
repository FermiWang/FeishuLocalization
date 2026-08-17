"""说话人分离 + 讯飞文本对齐 —— 独立进程脚本（需 funasr 环境运行，勿被主应用 import）。

用法：
    python diarize_worker.py <audio.wav(16k单声道)> <transcript.txt> <out.json>

输出 out.json：
    {"speakers": N, "labeled": "发言人1：……\n发言人2：……"}
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
    segs = (res[0].get("sentence_info") if res else None) or []
    if not segs:
        raise RuntimeError("声纹分离无结果（音频可能为空或无声）")

    with open(transcript_path, encoding="utf-8") as f:
        transcript = f.read()
    speakers = len({s["spk"] for s in segs})
    labeled = render_labeled(align_speakers(transcript, segs)) if speakers > 1 else ""

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"speakers": speakers, "labeled": labeled}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
