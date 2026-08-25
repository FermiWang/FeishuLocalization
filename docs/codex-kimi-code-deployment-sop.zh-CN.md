# FeishuLocalization 使用 ChatGPT Codex 或 Kimi Code 部署作业指导书（SOP V1.0）

**文件性质：** AI 编程助手辅助部署标准作业指导书
**适用项目：** FermiWang/FeishuLocalization
**项目程序名称：** Feishu Archive
**文档版本：** V1.0
**适用源码基线：** Feishu Archive v0.5.4 / 与本文位于同一 Git commit
**核验日期：** 2026年8月24日
**适用对象：** 希望借助 ChatGPT Codex 或 Kimi Code 完成本机部署、但不熟悉命令行的普通用户
**标准运行平台：** macOS
**配套文件：** [普通用户手工部署 SOP](manual-deployment-sop.zh-CN.md)

> **版本提示：** AI 助手、安装命令、飞书开放平台页面和本项目代码都会变化。本文只对上述源码基线负责。升级后应让 AI 重新读取当前 README、手工部署 SOP、CLI 帮助和安装脚本，不能把旧提示词直接当作新版本的事实。

---

# 一、先确定本指导书的作用

本指导书不是另一份重复的命令清单，而是说明如何让 ChatGPT Codex 或 Kimi Code 作为**受监督的本机部署助手**，完成以下工作：

- 检查 Mac、Python、Git、磁盘、端口和项目版本；
- 阅读本项目现有文档和实际代码，不凭经验猜测；
- 先验证演示环境，再初始化正式档案；
- 在用户完成飞书后台配置和 OAuth 后，执行聊天、知识库和邮箱同步；
- 安装并验证 macOS 后台任务；
- 输出有证据的验收结果和待处理事项；
- 可选地协助验证每日 Insights，但不得擅自启用模型或改变数据边界。

AI 助手不能代替：

- 飞书企业管理员审批；
- 应用权限发布；
- 用户本人确认 OAuth 授权；
- 数据所有者批准把档案正文交给模型处理；
- 对同步结果进行业务抽样；
- 软件许可证和企业合规判断。

**正确理解：** Codex 或 Kimi Code 可以减少命令行操作错误，但“AI 显示成功”不等于部署已经完成。最终必须用本机服务状态、健康接口、同步状态和内容抽样验收。

---

# 二、推荐路线和禁止路线

## 2.1 标准路线

完整部署请使用：

> **一台开启 FileVault 的 Mac + 本机项目目录 + Codex 本地模式或 Kimi Code CLI + 人工审批敏感操作**

选择工具时：

| 情况 | 推荐工具 |
| --- | --- |
| 不熟悉 Terminal，希望在图形界面中操作 | ChatGPT Codex 桌面应用 |
| 已经习惯 Terminal | Codex CLI 或 Kimi Code CLI |
| 企业已经统一采购或批准 Kimi Code | Kimi Code CLI |
| 企业已经统一采购或批准 ChatGPT/Codex | ChatGPT Codex |

两种工具最终执行的是同一套项目命令，部署结果不应因助手品牌不同而改变。

## 2.2 完整部署必须在实际 Mac 的本机环境中执行

Feishu Archive 的完整功能依赖：

- macOS Keychain；
- 浏览器回调到 `127.0.0.1:8766`；
- `~/Library/Application Support/Feishu Archive`；
- `~/Library/LaunchAgents`；
- 当前 macOS 用户的 `launchctl` 会话；
- 本机 `127.0.0.1:8765` 阅读器。

因此：

- Codex 桌面应用应选择 **Local / 本机**环境；
- 不要使用 Codex Cloud 完成真实授权和完整安装；
- Codex Worktree 可用于隔离修改文档或代码，但不适合作为本机 Keychain、OAuth 和 LaunchAgent 的最终部署环境；
- Kimi Code 应在准备运行 Feishu Archive 的那台 Mac 上启动。

## 2.3 禁止使用无人监管的全自动批准

部署涉及凭据、网络、Keychain、归档正文和后台任务。不要使用：

```text
Kimi Code: --yolo、--auto、/yolo、/auto
```

也不要给 Codex 开放不受限的全盘或长期自动批准。只审批当前步骤确实需要的明确命令和路径。

## 2.4 不要把正式档案目录作为 AI 工作区

AI 工作区只选择项目源码目录，例如：

```text
~/FeishuLocalization
```

不要选择：

```text
~/Library/Application Support/Feishu Archive
```

更不要把整个用户主目录、`~/Library` 或 `~/.ssh` 作为工作区。正式档案包含聊天、文档、邮件、附件、会话密钥和日志，不是部署助手需要浏览的源码。

---

# 三、人机分工和安全边界

## 3.1 AI 助手可以做什么

在每一步都可见、可审批的前提下，可以让 AI：

- 只读检查系统和仓库状态；
- 阅读 README、SOP、脚本和 CLI 帮助；
- 运行项目测试、语法检查和演示环境；
- 运行不包含秘密的 Feishu Archive 命令；
- 解释错误，但不得用删除数据或关闭安全控制来“修好”；
- 安装核心 LaunchAgent，并读取公开的服务状态；
- 提交项目维护者明确要求的文档或代码变更。

## 3.2 必须由用户本人完成什么

下列操作应由用户在飞书网页、浏览器或单独的 Terminal 窗口中完成：

1. 创建飞书企业自建应用；
2. 申请权限、设置可用范围、发布版本并取得管理员批准；
3. 查看、复制和保存 App Secret；
4. 在浏览器中确认 OAuth 授权；
5. 审批 AI 要执行的网络访问和工作区外写入；
6. 决定是否允许 Insights 把档案正文提交给指定模型服务器；
7. 对聊天、Wiki、邮件和 Insights 内容进行人工抽样。

## 3.3 任何时候都不要交给 AI 的内容

不要在 Codex/Kimi 对话、提示词、截图、问题描述或终端输出中暴露：

- 飞书 App Secret；
- OAuth `access_token`、`refresh_token` 或授权回调完整 URL；
- vMLX Bearer token；
- SSH 私钥内容；
- `reader.secret` 或邮箱/Insights 解锁 URL；
- macOS Keychain 中保存的值；
- 正式聊天、文档、邮件正文和附件；
- 含凭据的环境变量或完整进程环境。

即使 Agent 说“为了排障需要”，也不要运行会打印这些内容的命令。可以让 Agent 检查“是否存在”“退出码是什么”或使用项目的 `doctor`，不能读取真实值。

## 3.4 审批时只允许明确范围

Codex 和 Kimi Code 都可能在编辑文件、运行命令、访问网络或写入工作区以外的位置前请求批准。正常的核心安装可能需要：

- 访问 GitHub 和飞书 API；
- 写入 `~/Library/Application Support/Feishu Archive`；
- 写入 `~/Library/LaunchAgents`；
- 调用当前用户的 `launchctl`；
- 在 `127.0.0.1:8765` 和 `127.0.0.1:8766` 监听。

审批前核对命令与本 SOP 一致。不要批准 `sudo`、全盘访问、读取 `~/.ssh`、导出 Keychain、打印全部环境变量、删除归档目录或关闭 SSH Host Key 校验。

---

# 四、部署前准备

请先确认：

- macOS，且准备归档的用户已经登录；
- Python 3.11 或更高版本；
- Git；
- 至少 10 GiB 可用空间；启用邮箱附件时应保留超过 100 GiB 可用空间；
- 端口 `8765` 和 `8766` 可用；
- 有权创建或使用飞书企业自建应用；
- 如要提交 GitHub，GitHub 凭据已由用户本人配置；
- 已确认仓库当前“公开可见但未授予开源许可”的授权边界。

下面的系统检查可以由 AI 执行，不包含秘密：

```bash
sw_vers
python3 --version
python3 -c 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)'
git --version
fdesetup status
df -h "$HOME"
lsof -nP -iTCP:8765 -sTCP:LISTEN
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

如果 `lsof` 没有输出，通常表示端口空闲，不是错误。不要因为检查结果不理想就让 AI 自动安装未知软件、关闭 FileVault 或结束不认识的进程。

---

# 五、取得一份干净源码

用户可以在普通 Terminal 中执行：

```bash
cd "$HOME"
git clone https://github.com/FermiWang/FeishuLocalization.git
cd FeishuLocalization
git status --short
git rev-parse HEAD
git remote -v
```

`git status --short` 在新克隆中应没有输出。记录 `git rev-parse HEAD` 的结果，作为部署基线。

如果目录已经存在，让 AI **先只读检查**：

```bash
git status --short
git branch --show-current
git log -1 --oneline
git remote -v
```

发现本地修改、未跟踪文件或分支分叉时，不要让 AI 使用 `git reset --hard`、`git clean` 或删除文件。最安全的处理是保留旧目录，在另一个明确的新目录重新克隆。

---

# 六、启动 ChatGPT Codex

## 6.1 Codex 桌面应用

1. 从 [Codex 官方页面](https://developers.openai.com/codex/app)安装应用；
2. 使用组织批准的 ChatGPT 账号登录；
3. 选择 **Open folder / 打开文件夹**；
4. 只选择 `FeishuLocalization` 源码目录；
5. 新建 Codex 对话，环境选择 **Local / 本机**；
6. 先使用第八章的“只读预检提示词”。

不要先让 Codex“直接全部部署”。分阶段执行可以在飞书权限未发布、磁盘不足或源码不干净时及时停止。

## 6.2 Codex CLI

使用 OpenAI 当前官方安装方式：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex --version
cd "$HOME/FeishuLocalization"
codex
```

首次运行按屏幕提示使用 ChatGPT 登录。使用交互式会话，不要从一开始就调用无人监督的批处理模式。

安装脚本来自网络。执行前应确认域名是 `chatgpt.com`，并遵守本单位的软件安装和网络访问制度。

---

# 七、启动 Kimi Code

Kimi Code 当前正式 CLI 使用 Node.js 技术栈。使用其官方安装方式：

```bash
curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash
kimi --version
cd "$HOME/FeishuLocalization"
kimi --plan
```

进入界面后按提示执行：

```text
/login
```

使用 Kimi Code OAuth 登录，或按本单位规定使用平台 API Key。部署规划阶段保持 Plan 模式；准备执行明确步骤后再按界面提示切换普通模式。

如果单位不允许管道安装脚本，可先安装 Node.js 22.19 或更高版本，再使用官方包：

```bash
node --version
npm install -g @moonshot-ai/kimi-code
kimi --version
```

不要运行 `/init` 自动生成 `AGENTS.md`，除非项目维护者明确要求并会审查新增文件；部署本项目不需要修改仓库指令文件。

---

# 八、第一轮：只读预检

把下面提示词完整发给 Codex 或 Kimi Code：

```text
你正在本机 macOS 的 FeishuLocalization 仓库中协助部署。

本轮只读检查，禁止修改文件、安装软件、运行同步、写入 Keychain、启动后台服务或执行 Git 提交。

请完整阅读 README.md、docs/manual-deployment-sop.zh-CN.md、pyproject.toml、bin/feishu-archive、scripts/install-local.sh 和 scripts/uninstall-local.sh，并用当前 CLI --help 核对文档命令。

请检查并报告：
1. 仓库绝对路径、当前分支、HEAD、remote 和 git status；
2. macOS、Python 版本、Python 路径、Git、FileVault、磁盘空间；
3. 8765、8766 端口是否占用；
4. 项目版本和 macOS 完整功能边界；
5. 安装器会写入哪些目录、建立哪些 LaunchAgent；
6. 当前还缺哪些必须由用户在飞书网页或浏览器完成的步骤。

安全要求：
- 不读取或输出 Keychain、剪贴板、环境变量、OAuth token、App Secret、SSH 私钥、reader.secret；
- 不浏览正式 Feishu Archive 档案正文或附件；
- 不使用 sudo、git reset、git clean、rm 或 kill；
- 不以“测试通过”代替真实部署验收。

输出“通过 / 阻塞 / 需人工确认”三栏检查表，并给出每项证据。完成后停止，等待用户决定下一步。
```

只有以下项目通过后继续：

- 仓库路径正确；
- 工作区没有意外修改；
- Python 至少 3.11；
- FileVault 已开启；
- 磁盘符合要求；
- 端口没有未知服务占用；
- Agent 已识别 macOS 是完整同步的目标平台；
- Agent 没有读取任何秘密或正式档案内容。

---

# 九、先做项目自身检查和隔离演示

允许 Agent 在项目目录中运行：

```bash
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
sh -n scripts/install-local.sh
sh -n scripts/uninstall-local.sh
git diff --check
```

再使用隔离目录验证阅读器：

```bash
DEMO_ARCHIVE="$HOME/Library/Application Support/Feishu Archive Demo"
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" init
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" demo
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" serve
```

用户在浏览器打开 <http://127.0.0.1:8765>，确认示例聊天可见，然后回到运行服务的终端按 `Control+C`。

演示档案和正式档案必须分开。Agent 不得为了重试而删除正式目录。

---

# 十、飞书开放平台配置必须由用户完成

请以[手工部署 SOP 第四至第十三阶段](manual-deployment-sop.zh-CN.md#七第四阶段创建主飞书应用)为逐屏操作依据。AI 只能显示检查清单，不能替用户批准权限。

## 10.1 主应用权限

核对当前源码要求的权限：

```text
im:message:readonly
im:message.p2p_msg:get_as_user
im:message.group_msg:get_as_user
im:chat:readonly
im:chat.members:read
search:message
wiki:wiki:readonly
docx:document:readonly
drive:drive:readonly
offline_access
```

如果开放平台显示的名称变化，以权限代码、平台说明和后续 `doctor` 为准，不让 AI 猜一个“差不多”的权限。

## 10.2 邮箱应用权限

推荐另建 `Feishu Archive Mail` 应用，申请：

```text
mail:user_mailbox:readonly
mail:user_mailbox.folder:read
mail:user_mailbox.message:readonly
mail:user_mailbox.message.subject:read
mail:user_mailbox.message.address:read
mail:user_mailbox.message.body:read
offline_access
```

## 10.3 两个应用都要完成的事项

- 回调地址精确设置为 `http://127.0.0.1:8766/oauth/callback`；
- 设置应用可用范围；
- 创建并发布版本；
- 完成企业管理员审批；
- 主应用机器人加入需要归档的群；
- 不配置事件订阅、Webhook、域名、内网穿透或公网端口。

权限只保存在草稿中不算完成。新增权限后必须重新发布，并重新执行对应 OAuth。

---

# 十一、在单独 Terminal 中保存凭据

**本章命令不要让 Codex/Kimi 代为运行。** AI 终端的输入和输出可能进入会话上下文。用户应打开一个普通 Terminal，进入同一源码目录后亲自执行。

保存主应用 App ID：

```bash
pbpaste | ./bin/feishu-archive configure --app-id-stdin
printf '' | pbcopy
```

再从开放平台复制 App Secret，保存并清空剪贴板：

```bash
pbpaste | ./bin/feishu-archive configure --app-secret-stdin
printf '' | pbcopy
```

使用独立邮箱应用时，分别复制其 App ID 和 App Secret：

```bash
pbpaste | ./bin/feishu-archive mail-configure --app-id-stdin
printf '' | pbcopy
pbpaste | ./bin/feishu-archive mail-configure --app-secret-stdin
printf '' | pbcopy
```

每次必须先复制正确的单个值，再立即执行对应命令。不要把凭据放入 shell 变量、命令参数、文本文件、源码、README 或 Git commit。

## 11.1 OAuth 也由用户亲自完成

主应用：

```bash
./bin/feishu-archive auth
```

邮箱应用：

```bash
./bin/feishu-archive mail-auth
```

浏览器打开后，用户本人核对应用名称、权限和账号，再确认授权。不要把浏览器地址栏中的完整授权 URL、回调 URL 或错误页截图发给 AI。

完成后只告诉 Agent：

```text
主应用和邮箱应用已发布并由本人完成 OAuth。请只用 doctor 和 mail-doctor 检查是否可用，不读取任何凭据值。
```

---

# 十二、让 Agent 执行核心部署

把下面提示词发给 Codex 或 Kimi Code：

```text
用户已经在单独 Terminal 中保存飞书凭据，并亲自完成主应用和邮箱 OAuth。现在按当前 README 和手工部署 SOP 执行“核心归档部署”，不得启用 Insights。

执行边界：
- 不修改任何程序源码或默认配置；
- 不读取 Keychain、剪贴板、环境变量、SSH 文件、reader.secret 或正式档案正文；
- 不使用 sudo，不删除数据库、档案、锁文件或现有 Git 修改；
- 长任务一次只运行一个；失败时保留原始错误并先诊断，不叠加重试；
- 每个会改变状态的步骤先说明命令、作用和预期，再等待审批；
- 安装后台任务时必须明确使用 --without-insights；
- 不提交或推送 Git。

依次完成：
1. ./bin/feishu-archive init
2. ./bin/feishu-archive doctor
3. ./bin/feishu-archive discover
4. ./bin/feishu-archive sync --all-discovered
5. ./bin/feishu-archive attachments --workers 4
6. ./bin/feishu-archive wiki-discover
7. ./bin/feishu-archive wiki-sync
8. ./bin/feishu-archive mail-sync
9. ./bin/feishu-archive mail-status
10. ./bin/feishu-archive mail-doctor
11. 确认没有手工 serve 占用 8765，且 command -v python3 指向长期保留的 Python
12. ./scripts/install-local.sh --without-insights

同步可能很久。不要把工具超时等同于任务失败；用进程、锁、日志时间和状态命令判断。完成后按“成功 / 失败 / 未验证”报告每一步和证据，然后停止。
```

首次同步时间取决于飞书可返回的数据量、附件量和接口限流。不要因为长时间无新输出就结束进程，也不要并行启动第二个相同同步。

如果当前不使用飞书邮箱，应保留邮件同步通道的安全跳过行为；不要让 Agent 改为 IMAP，也不要通过修改代码伪造“通过”。

---

# 十三、核心部署验收

让 Agent 运行以下不含秘密的检查：

```bash
curl --fail http://127.0.0.1:8765/api/status
curl --fail http://127.0.0.1:8765/api/wiki/status
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive"
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-sync"
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-wiki-sync"
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-mail-sync"
./bin/feishu-archive doctor
./bin/feishu-archive mail-status
./bin/feishu-archive mail-doctor
git status --short
git diff --check
```

因为本次明确使用 `--without-insights`，下面两个服务应不存在：

```bash
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights"
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights-backfill"
```

“服务不存在”在这里是预期安全结果，不是部署失败。

## 13.1 用户本人进行内容抽样

用户打开 <http://127.0.0.1:8765>，至少确认：

- 能看到预期会话，发送者、时间和消息正文合理；
- 搜索一个已知关键词能命中；
- 抽查一个 Thread/话题回复；
- 抽查一张图片和一个允许下载的文件；
- 能看到预期知识空间和新版文档正文；
- 文档图片或附件可以打开；
- 邮箱文件夹、邮件主题、正文和允许下载的附件合理；
- 页面只通过 `127.0.0.1` 或 `localhost` 访问。

邮箱默认锁定。解锁命令由用户在普通 Terminal 中运行，不要把生成的 URL 发给 AI：

```bash
./bin/feishu-archive mail-reader-url --open
```

使用完毕可恢复锁定并撤销现有会话：

```bash
./bin/feishu-archive mail-reader-url --lock
```

只有命令通过而没有内容抽样，结论必须写成“技术检查通过，业务完整性未验收”。

---

# 十四、可选：每日 Insights 的受控部署

聊天、知识库、邮箱、搜索和阅读器不需要大模型。只需要核心归档的用户到第十三章即完成部署。

Insights 会读取本地归档中的聊天、知识库和邮件正文，并发送给指定的本地或局域网模型服务。启用前必须由数据所有者明确确认：

- 模型主机和运营方；
- SSH 用户及专用私钥路径；
- Host Key 指纹；
- 模型名称；
- 端口和认证方式；
- 日报时区；
- 模型服务是否记录、保留或再利用输入；
- 哪些数据允许进入模型处理。

不要让 Agent 自动发现局域网主机、读取 SSH 私钥、接受未知 Host Key、关闭 `StrictHostKeyChecking`，或把凭据写进 `config.py`。

## 14.1 先做不调用模型的干跑

允许 Agent 运行：

```bash
./bin/feishu-archive insights-run --timezone Asia/Shanghai --no-model --dry-run
```

这只验证数据窗口和确定性统计，不调用模型，也不写正式日报。

## 14.2 再做一次有参数的人工测试

参数必须由模型管理员提供。下面只是格式示例，不能照抄主机、用户和模型：

```bash
./bin/feishu-archive insights-run \
  --timezone Asia/Shanghai \
  --host 192.168.1.50 \
  --user modeluser \
  --identity-file "$HOME/.ssh/id_ed25519_feishu_archive" \
  --model vmlx/qwen3-32b-8bit \
  --local-port 18135 \
  --remote-port 11435
```

用户再运行 `mail-reader-url --open`，打开 <http://127.0.0.1:8765/?mode=insights>，人工核对摘要、计划、机会、证据、置信度、日期和模型身份。

## 14.3 最后才启用自动洞察

当前源码的后台 Insights 使用 `src/feishu_archive/config.py` 中的默认模型参数。实际环境与默认值不一致时，必须按[手工部署 SOP 第三十八至四十阶段](manual-deployment-sop.zh-CN.md#三十八如果模型服务器和当前源码默认值不同)由管理员受控配置、测试和记录。

只有以下条件全部满足，才执行：

```bash
./scripts/install-local.sh --with-insights
```

- 数据处理边界已书面确认；
- 专用 SSH 和 Host Key 已配置；
- `--no-model --dry-run` 通过；
- 一次真实模型报告通过人工抽样；
- 后台默认参数与刚才成功测试的参数完全一致；
- 用户理解该命令会立即启动历史回填，并建立每日自动任务。

启用后检查：

```bash
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights"
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights-backfill"
./bin/feishu-archive insights-status
```

如果没有取得数据处理授权，始终使用 `--without-insights`。

---

# 十五、最终只读验收提示词

部署结束后，发给 Agent：

```text
现在只做最终验收，不修改、不修复、不重装、不重启、不提交 Git。

依据当前 README、docs/manual-deployment-sop.zh-CN.md 和 docs/codex-kimi-code-deployment-sop.zh-CN.md，检查：
- 当前源码版本、工作树；
- /api/status 和 /api/wiki/status；
- reader、chat sync、wiki sync、mail sync、meeting records sync 的 LaunchAgent；
- doctor、mail-status、mail-doctor；
- 当前是否应安装 Insights；
- 最近同步状态与日志中是否有当前时间段的新错误。

禁止读取 Keychain、秘密、reader.secret、SSH 私钥或正式正文；禁止用健康接口推断内容完整性。

以表格输出每项：结论只能是 PASS、FAIL 或 NOT VERIFIED；同时列证据、风险和需要用户人工抽样的内容。最后给出“核心部署是否完成”和“Insights 是否获授权并完成”两个独立结论。
```

接受的最终结论格式应类似：

| 项目 | 结论 | 证据 |
| --- | --- | --- |
| 核心阅读器 | PASS | `/api/status` 返回成功，LaunchAgent 已加载 |
| 聊天业务完整性 | NOT VERIFIED | 需要用户抽查已知会话和关键词 |
| Insights | NOT VERIFIED | 未取得数据处理授权，因此保持未安装 |

不要接受“整体看起来正常”或“测试全过，所以部署成功”这种无证据结论。

---

# 十六、常见问题

| 现象 | 正确处理 |
| --- | --- |
| Codex 提示没有网络权限 | 核对目标域名和命令，只批准本步骤需要的 GitHub/飞书访问，不切换不受限模式 |
| Agent 不能写 `~/Library` | 核对是否正在运行官方安装命令，只批准项目规定的 Application Support 和 LaunchAgents 路径 |
| Kimi 每条命令都要求批准 | 属于正常安全行为；不要改用 `/yolo` 或 `/auto` |
| `kimi` 或 `codex` 找不到 | 重新打开 Terminal，核对官方安装输出和 PATH；不要下载来源不明的同名程序 |
| `git status` 有修改 | 先保存状态和差异；不要强制清理，改用新的干净克隆或请维护者判断 |
| `Address already in use` | 查明 8765/8766 的进程；如果是手工 `serve`，按 `Control+C` 停止；不要结束未知进程 |
| `doctor` 提示权限缺失 | 回飞书开放平台补权限、发布新版本、取得审批，再重新 OAuth |
| `mail-auth` 后仍缺权限 | 核对 7 项 Mail 权限是否已发布；复用主应用也必须重新执行 `mail-auth` |
| 同步很久没有结束 | 查看进程、锁、日志时间和状态；不要并行重启相同同步，不要删除 `.lock` |
| AI 说“单元测试通过” | 继续检查 OAuth、真实同步、健康接口、LaunchAgent 和用户内容抽样 |
| Insights SSH 失败 | 由模型管理员核对主机、用户、专用私钥和 Host Key；不得关闭严格校验 |
| Agent 建议把日志全部贴进聊天 | 先脱敏，只提供最小错误片段；不上传正文、Token、路径中的秘密或解锁 URL |

---

# 十七、升级、停止和回退

## 17.1 升级前让 Agent 只读检查

```bash
git status --short
git branch --show-current
git log -1 --oneline
git fetch origin
git log --oneline --decorate HEAD..origin/main
```

工作区必须清楚后才考虑：

```bash
git pull --ff-only
```

不要允许 Agent 用强制重置处理冲突。升级后重新运行测试，再按原模式显式执行：

```bash
./scripts/install-local.sh --without-insights
# 仅在原先已获授权且重新验收模型边界时使用 --with-insights
```

## 17.2 停止后台服务但保留数据

```bash
./scripts/uninstall-local.sh
```

此脚本移除当前用户 LaunchAgent，不删除正式档案。Agent 不得继续删除：

```text
~/Library/Application Support/Feishu Archive
```

## 17.3 出现故障时的红线

禁止让 AI：

- `git reset --hard` 或 `git clean -fd`；
- 删除正式数据库、归档目录、`runtime` 或 `.lock`；
- 用 `sudo` 重装本项目；
- 将阅读器监听地址改为 `0.0.0.0`；
- 建立公网反向代理或端口转发；
- 关闭 FileVault、邮件危险附件隔离或 SSH Host Key 校验；
- 把正式档案上传到 GitHub、ChatGPT、Kimi 或公共文件服务；
- 把“恢复服务”与“确认内容完整”混为一谈。

---

# 十八、完成记录表

部署负责人应保存下面的非敏感记录；不要记录 Secret、Token 或解锁 URL：

```text
部署日期：
Mac 资产编号或受控名称：
macOS 版本：
Python 版本及路径：
Feishu Archive 版本：
Git commit SHA：
AI 助手：ChatGPT Codex / Kimi Code
AI 助手版本（如界面可见）：
主应用版本已发布：是 / 否
邮箱应用版本已发布：是 / 否 / 不使用邮箱
FileVault：已开启 / 未开启
核心安装模式：--without-insights / --with-insights
核心健康检查：PASS / FAIL
聊天人工抽样：PASS / FAIL / NOT VERIFIED
知识库人工抽样：PASS / FAIL / NOT VERIFIED
邮箱人工抽样：PASS / FAIL / NOT VERIFIED
Insights 数据处理授权：有 / 无
Insights 人工抽样：PASS / FAIL / NOT APPLICABLE
未解决问题：
验收人：
```

核心部署只有在健康接口、五个核心 LaunchAgent、`doctor`/`mail-doctor`、`meeting-records-status` 和用户内容抽样都达到预期后才算完成。Insights 必须单独授权、单独验收，不能因核心归档成功而默认启用。

---

# 十九、官方参考资料

以下资料已于 2026年8月24日核对：

- [OpenAI：Codex 桌面应用](https://developers.openai.com/codex/app)
- [OpenAI：Codex CLI](https://developers.openai.com/codex/cli)
- [OpenAI：本机环境](https://learn.chatgpt.com/docs/environments/local-environment)
- [OpenAI：Agent 审批与安全](https://learn.chatgpt.com/docs/agent-approvals-security)
- [Kimi Code：Getting Started](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started.html)
- [Kimi Code：交互与审批模式](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/interaction.html)
- [Kimi Code：命令参考](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html)
- [Feishu Archive：普通用户手工部署 SOP](manual-deployment-sop.zh-CN.md)

如果官方工具说明与本文不一致，应先停止部署，核对工具的新版本行为，再由项目维护者更新本指导书。
