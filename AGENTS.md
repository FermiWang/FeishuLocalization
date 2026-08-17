# 会议纪要整理应用 — 开发约定

## 铁律：先提交，再部署

任何代码改动必须**先在本仓库完成 git 提交，再部署到服务器**。
禁止把未提交的改动直接 rsync 到线上；部署的内容必须能在 git 历史中复现。

## 部署（仅在提交后执行）

服务器：apple@192.168.100.179（Mac-Studio），应用目录 `~/meeting-minutes/`。

```bash
# 1. 提交
git add -A && git commit -m "<说明>"

# 2. 同步代码（排除运行数据与虚拟环境）
rsync -a --exclude data --exclude __pycache__ --exclude .venv --exclude .git \
  ./ apple@192.168.100.179:meeting-minutes/

# 3. 重启服务（launchd 常驻）
ssh apple@192.168.100.179 "
  launchctl unload ~/Library/LaunchAgents/com.apple.meeting-minutes.plist
  sleep 1
  launchctl load ~/Library/LaunchAgents/com.apple.meeting-minutes.plist"

# 4. 验证
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.100.179:8765/
```

## 环境要点

- 主应用 venv（远端）：`~/meeting-minutes/.venv`，py3.14，fastapi/uvicorn/httpx/python-multipart
- 声纹分离 venv（远端）：`~/meeting-minutes/.venv-spk`，py3.11，funasr/torch/torchaudio，
  由 `app/spk.py` 子进程调用 `app/diarize_worker.py`；缺失时自动跳过声纹步骤
- 模型端点：`http://127.0.0.1:11435/v1`（vmlx/qwen3.8-27b-8bit，服务器本机）
- 详细架构与配置见 README.md；声纹选型验证记录见 research/README.md
