# 会议纪要整理应用

部署在局域网服务器 **192.168.100.179**（Mac-Studio）上的会议纪要整理工具：
上传讯飞语音转写文字，调用该机本地大模型自动整理出会议纪要、参会人观点、
冲突点、达成一致、未达成一致和待办事项。

## 访问地址

**http://192.168.100.179:8765/** （局域网内任意电脑/手机浏览器直接打开）

服务以 launchd 常驻运行（开机自启、崩溃自动重启），plist：
`~/Library/LaunchAgents/com.apple.meeting-minutes.plist`（远端 apple 用户）。

## 使用流程

1. 左侧「新建会议」：填写标题、日期，可选填会议背景、主要参会人及角色。
2. 进入会议详情：
   - 上传会议录音 mp3（存档与回放，可选）；
   - 上传讯飞识别文字（txt 文件或直接粘贴，**整理所必需**）。
3. 点击「开始整理」，后台自动执行两步：有录音时先做**声纹分离**（FunASR
   paraformer-zh + cam++，把讯飞转写按「发言人N」区分，可在转写卡片查看标注版），
   再调用本地大模型整理；页面自动轮询展示阶段进度与结果。
   声纹分离失败或音频只有一人讲话时，自动回退为无标注整理，不影响使用。
4. 结果包含六个板块：会议纪要 / 主要参会人员观点 / 观点冲突点 /
   会议达成一致 / 会议未达成一致 / 会议待办。
5. 会议详情页右上角「删除会议」（两次确认）：在服务器上永久删除该会议的
   全部记录——数据库中的会议、参会人、纪要，以及 uploads/ 下的录音与转写文件。

## 服务器端管理（在 192.168.100.179 上）

```bash
# 查看状态 / 日志
launchctl list | grep meeting-minutes
tail -f ~/meeting-minutes/data/server.log

# 停止 / 启动
launchctl unload ~/Library/LaunchAgents/com.apple.meeting-minutes.plist
launchctl load   ~/Library/LaunchAgents/com.apple.meeting-minutes.plist

# 手动前台运行（调试用）
cd ~/meeting-minutes && ./run.sh
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://127.0.0.1:11435/v1` | 服务器本机 vmlx 端点 |
| `LLM_MODEL` | `vmlx/qwen3.8-27b-8bit` | 模型 ID |
| `SPK_PYTHON` | `<应用目录>/.venv-spk/bin/python3` | 声纹分离环境（funasr）的 python 路径 |
| `SPK_TIMEOUT` | `7200` | 声纹分离子进程超时（秒） |
| `HOST` / `PORT` | `0.0.0.0` / `8765` | 监听地址（仅 `run.sh` 手动启动时生效；launchd 固定 0.0.0.0:8765） |

## 依赖与数据

- 远端虚拟环境：`~/meeting-minutes/.venv`（fastapi、uvicorn、httpx、python-multipart）
- 声纹分离环境：`~/meeting-minutes/.venv-spk`（python3.11 + funasr + torch + torchaudio，
  由 `app/spk.py` 以子进程方式调用 `app/diarize_worker.py`；该 venv 缺失时自动跳过声纹步骤）
- 声纹模型缓存：`~/.cache/modelscope`（约 1GB，首次运行自动下载）
- 运行数据：`~/meeting-minutes/data/`（`meetings.db` SQLite、`uploads/` 录音与转写、`server.log`）
- 源码在本目录，改动后用 rsync 同步：
  `rsync -a --exclude data --exclude __pycache__ --exclude .venv meeting-minutes/ apple@192.168.100.179:meeting-minutes/`

## 说明

- 转写超过约 2 万字时自动分段提取要点后再汇总，适配长会议。
- 应用本身不做语音识别；mp3 仅用于存档回放，识别请使用讯飞导出文字。
