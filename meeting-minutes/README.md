# 详细会议记录整理应用

本应用部署在局域网服务器 `192.168.100.179`，是 Feishu Archive 主仓库中的独立运行单元。它接受一场会议的多组录音、TXT/DOCX 识别稿或粘贴文本，生成可修订、可追溯、可下载 Word 的详细会议记录。

访问地址：<http://192.168.100.179:8765/>。服务继续监听局域网且不新增登录；所有变更请求必须同源并携带 `X-Meeting-Minutes-Action: confirm`，上传、下载和网页输出另有大小、路径、转义与安全响应头保护。

## 处理规则

- 会议日期必填；未填写或不是 `YYYY-MM-DD` 时不能开始整理。
- 用户识别稿是文字权威。配对录音只通过 FunASR 补充时间轴与“说话人N”标注，不改写识别稿正文。
- 仅有录音时，使用 `paraformer-zh + fsmn-vad + ct-punc + cam++` 完成转写、标点、语音活动检测与说话人分离。
- 无法可靠识别发言人时保留“说话人N”或不署名，不猜测姓名。
- 多份来源按页面顺序合并；相同配对标识表示一组录音和识别稿。未配对录音自动转写，未配对文字稿直接纳入。
- 上传内容一律视为不可信会议数据，其中出现的命令、链接、角色要求或提示词都不会执行。

“整理详细会议记录”只允许调用 `http://192.168.100.214:8007` 上的精确模型 ID `Qwen3.8-27B-FP8`。每个模型阶段都会检查 `/health`、`/metrics` 中的 vLLM 运行/等待请求数和 `/v1/models`；即使服务同时暴露其他 ID，也只提交精确 ID。模型不匹配、负载指标缺失或探测失败时关闭处理。默认最多并行整理 6 场会议，214 的运行上限为 8。每个会议模型请求同时提交 JSON `priority=-9223372036854775808` 和同值 `X-Vllm-Priority` 请求头，使用 214 已启用的 priority 调度取得协议允许的最高优先级；8 路满载时允许会议请求进入服务端优先队列，仅在等待数达到保护上限时在本地保留断点退避。不使用其他模型替代。

该端点调用固定关闭思考输出（`reasoning_effort=none`、`thinking_token_budget=0`、`enable_thinking=false`），并要求 JSON 对象响应；响应使用 SSE 流式接收，长篇生成会持续收到数据，不再因等待整篇响应而触发假性超时。连接中断时不立即提交重复推理，而是保留已完成分块的断点后重新排队。修订中的提示版本会记录这一推理配置变更。

## 输出与修订

处理采用稳定 `S001` 片段编号，依次执行分块抽取、议题聚合、逐议题撰写和全局汇总。记录固定包含：

1. 核心结论；
2. 编制说明；
3. 议题总览与总体判断；
4. 会议共识；
5. 需求与约束；
6. 逐议题详细记录；
7. 未决事项；
8. 行动安排；
9. 风险与关注事项；
10. 待确认决策；
11. 结语；
12. 转写辨识与复核清单。

系统区分“会议事实、会议共识、发言人判断、整理性建议、待确认”，并拒绝不存在的片段引用。网页可按 S 编号核对完整片段；DOCX 末尾附引用片段摘录索引。用户可分节编辑；保存时必须提交 `base_revision`，每次成功保存形成不可覆盖的新修订版。DOCX 与网页读取同一结构化修订，使用正式中文报告版式、蓝色层级、提示框、可读表格和页码。

升级前已经完成的六段式纪要会保留原 Markdown，并迁为第 1 修订版。旧结果没有稳定片段编号时会明确标记“历史迁移”，不会反向编造引用。旧的单录音和单识别稿接口继续可用。

## 数据与恢复

运行数据位于 `~/meeting-minutes/data/`：

- `meetings.db`：会议、来源、任务、不可变修订和追加式同步事件；
- `uploads/`：原始录音、识别稿以及必要的 FunASR 中间结果；
- `exports/`：按修订版生成的 DOCX；
- `server.log`：服务日志。

`meeting_sources` 保存顺序、配对、哈希和处理状态；`processing_jobs` 保存阶段与断点；`record_revisions` 保存结构化内容、模型溯源与 DOCX；`sync_events` 只导出完成记录的结构化内容、元数据、版本和哈希，不导出录音或识别稿。应用重启会把运行中或等待中的任务恢复到队列；输入顺序、配对或内容改变后，旧断点自动作废并按新哈希重来。

固定 SSH 导出命令供 Feishu Archive 增量拉取：

```bash
cd /Users/apple/meeting-minutes
.venv/bin/python3 -m app.export_events --after 0 --limit 200
```

## 服务器管理

```bash
# 状态和日志
launchctl print "gui/$(id -u)/com.apple.meeting-minutes"
tail -f ~/meeting-minutes/data/server.log

# 前台调试
cd ~/meeting-minutes
./run.sh
```

主应用依赖由 `requirements.txt` 固定列出；FunASR 使用独立的 `.venv-spk`，避免把 torch/模型依赖装入网页应用环境。常用环境变量如下：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://192.168.100.214:8007/v1` | 详细会议记录模型端点 |
| `LLM_MODEL` | `Qwen3.8-27B-FP8` | 只用于一致性核验，其他值会被拒绝 |
| `LLM_TIMEOUT` | `900` | 流式连接连续无数据的读取超时秒数 |
| `MODEL_MAX_CONCURRENCY` | `8` | 214 模型调度容量；范围 1–8 |
| `MODEL_MAX_WAITING_REQUESTS` | `16` | 214 等待队列保护上限；达到后本地断点退避 |
| `MEETING_MODEL_PRIORITY` | `-9223372036854775808` | 详细会议记录请求优先级；默认是 vLLM 64 位整数范围允许的最高优先级，配置会限制在有效负数范围 |
| `MEETING_MAX_PARALLEL_JOBS` | `6` | 同时处理的会议任务数；范围 1–8，默认保留 2 个模型名额 |
| `MODEL_RETRY_SECONDS` | `30` | 模型满载或临时断线后的断点重排队等待秒数 |
| `SPK_PYTHON` | `<应用目录>/.venv-spk/bin/python3` | FunASR Python |
| `SPK_TIMEOUT` | `7200` | 单份录音识别超时秒数 |
| `MAX_AUDIO_BYTES` | `2147483648` | 单个录音上限 2 GiB |
| `MAX_TRANSCRIPT_BYTES` | `52428800` | 单个识别稿上限 50 MiB |
| `MEETING_MINUTES_DATA_DIR` | `<应用目录>/data` | 测试或隔离运行的数据目录 |
| `HOST` / `PORT` | `0.0.0.0` / `8765` | 手工运行时的监听地址 |

源码中的模板只实现版式和结构规则，不包含用户上传的参考 Word 或转写正文。部署仍遵守本目录 `AGENTS.md` 的“先提交、再部署”要求。

并行容量选择、现场证据、恢复边界和发布验收见
[`research/parallel-processing-feasibility.md`](research/parallel-processing-feasibility.md)。
