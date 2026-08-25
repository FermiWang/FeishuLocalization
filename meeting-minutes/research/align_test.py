# 原型验证：把讯飞转写文本按声纹分离结果标注发言人
# 思路：
#   1. FunASR 输出带说话人标签的句段（时间戳 + ASR 文本）——时间轴 + 说话人是准的，文本不如讯飞准；
#   2. 讯飞文本没有发言人信息但文字准确；
#   3. 字符级对齐（difflib）把两者匹配起来，讯飞文本的每个字符继承对应 ASR 字符的说话人标签；
#   4. 按讯飞原句切分，句内多数投票决定该句发言人；说话人切换处生成新的段落。
import json
import re
import sys
from difflib import SequenceMatcher

PUNCT = "，。！？、；：,.!?;:"


def normalize(text: str) -> str:
    """去掉标点空白，只留可比对字符。"""
    return re.sub(r"[\s" + re.escape(PUNCT) + r"]", "", text)


def align(iflytek_text: str, asr_segs: list[dict]) -> list[dict]:
    """返回 [{'speaker': spk_id, 'text': 讯飞原句}] 列表。"""
    # 1) ASR 侧：归一化字符 -> 说话人
    asr_chars: list[str] = []
    asr_spk: list[int] = []
    for seg in asr_segs:
        for ch in normalize(seg["text"]):
            asr_chars.append(ch)
            asr_spk.append(seg["spk"])

    # 2) 讯飞侧：按句切分（保留原句），每句记录其归一化字符
    sentences = [s for s in re.split(r"(?<=[。！？!?；;])\s*", iflytek_text) if s.strip()]

    # 3) 全文对齐：讯飞归一化串 vs ASR 归一化串
    fk_chars = []
    fk_index = []  # fk_chars[i] 属于第几句
    for i, s in enumerate(sentences):
        for ch in normalize(s):
            fk_chars.append(ch)
            fk_index.append(i)

    sm = SequenceMatcher(None, "".join(fk_chars), "".join(asr_chars), autojunk=False)
    char_spk: list[int | None] = [None] * len(fk_chars)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("equal", "replace") and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                char_spk[i1 + k] = asr_spk[j1 + k]
        elif tag == "equal":
            for k in range(i2 - i1):
                char_spk[i1 + k] = asr_spk[j1 + k]

    # 4) 句级多数投票（未匹配字符按前一个已匹配字符的标签顺延）
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


def main():
    with open("diarize_result.json", encoding="utf-8") as f:
        segs = json.load(f)
    with open(sys.argv[1] if len(sys.argv) > 1 else "iflytek_mock.txt",
              encoding="utf-8") as f:
        iflytek = f.read()

    labeled = align(iflytek, segs)
    cur = None
    for item in labeled:
        if item["speaker"] != cur:
            cur = item["speaker"]
            print(f"\n发言人{cur}：", end="")
        print(item["text"], end="")
    print()


if __name__ == "__main__":
    main()
