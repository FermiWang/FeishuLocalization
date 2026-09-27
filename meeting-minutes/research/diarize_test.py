# 原型验证：FunASR paraformer + cam++ 对多说话人音频做句级说话人标注
import json

from funasr import AutoModel

model = AutoModel(
    model="paraformer-zh",
    vad_model="fsmn-vad",
    punc_model="ct-punc",
    spk_model="cam++",
    disable_update=True,
)

res = model.generate(
    input="meeting3.wav",
    batch_size_s=300,
)

segs = res[0]["sentence_info"]
with open("diarize_result.json", "w", encoding="utf-8") as f:
    json.dump(segs, f, ensure_ascii=False, indent=1)

for seg in segs:
    print(f"[{seg['start']/1000:7.2f} - {seg['end']/1000:7.2f}] spk{seg['spk']}: {seg['text']}")
