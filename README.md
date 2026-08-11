# Feishu Archive

Feishu Archive 是一个本地优先的飞书离线归档 PoC。它只通过飞书官方授权接口同步当前用户可见的聊天记录、知识空间和飞书邮箱，将消息、目录、文档正文、邮件正文、图片、文件、附件、全文索引与同步状态保存在本机，并提供只监听回环地址的离线网页阅读器。

项目不会读取、复制或尝试解密 `LarkShell`、`messages.db`、`im.db`、`Core.db` 等飞书客户端私有数据。邮箱同步使用飞书 Mail OpenAPI，不通过 IMAP，也不读取飞书客户端的本地邮件数据库。

## 先看平台支持范围

当前版本为 `0.3.4`，完整的“授权、同步、自动增量更新”链路仍以 macOS 为目标平台。Linux 和 Windows 可以复用部分源码，但不能在不改代码的情况下获得与 macOS 相同的完整同步能力。

| 能力 | macOS 原生 | Linux 原生 | Windows 原生 | Windows + WSL2 |
| --- | --- | --- | --- | --- |
| 初始化 SQLite、写入示例数据 | 支持 | 支持 | 不支持 | 支持 |
| 启动本地离线阅读器 | 支持 | 支持 | 不支持 | 支持 |
| 飞书 OAuth、聊天、知识库与邮箱同步 | 支持 | 暂不支持 | 不支持 | 暂不支持 |
| 安全保存应用凭据和用户令牌 | macOS Keychain | 尚未适配 Secret Service | 尚未适配 Credential Manager | 尚未适配 |
| 重复同步进程锁 | `fcntl` | `fcntl` | 尚未适配 | `fcntl` |
| 后台启动和每日计划任务 | LaunchAgent | 可为阅读器配置 systemd；同步尚不可用 | 尚未适配 Task Scheduler | 可运行阅读器 |
| `doctor` 安全检查 | 支持 FileVault | 尚未适配 LUKS | 尚未适配 BitLocker | 尚未适配 |

因此：

- 需要真实授权和完整同步时，请部署在 macOS。
- Linux 可用于开发、测试、示例数据和不触发同步的本地阅读器。
- Windows 当前应使用 WSL2 运行示例和阅读器；原生 Python 会因 `fcntl` 不可用而启动失败。
- 不要把 Linux、Windows 或 WSL2 的演示部署描述为已经支持真实飞书同步。

### 源码授权状态

仓库公开可见不等于已经取得开源许可。当前 `pyproject.toml` 仍声明 `Proprietary - no license granted`，仓库也没有 OSI 开源许可证。第三方目前只能在获得仓库所有者授权后复制、修改或部署；如果计划正式开源，应先添加 `LICENSE`，并同步修改项目元数据和本节说明。

## 主要能力

- OAuth 2.0 用户授权；应用凭据、`access_token` 和 `refresh_token` 保存在 macOS Keychain。
- 发现用户可见的群聊，并通过消息搜索补发现有可见消息的单聊。
- 默认同步全部可获取历史；普通群消息包含 `thread_id` 时，继续同步话题回复。
- SQLite 保存会话、成员、消息、编辑/删除状态、资源清单与同步游标。
- SQLite FTS5 支持按会话、日期、人员、消息类型和正文搜索。
- 同步用户可见的知识空间与目录树；新版文档保存正文、文档块、内嵌图片和附件，普通文件节点保存文件本体。
- 为知识库建立独立 FTS5 索引，并为每篇新版文档生成本地 HTML 副本。
- 知识库阅读采用“左侧空间、右侧节点目录、点击节点查看正文”的三级导航；图片在正文内显示，网页和文件从新窗口打开。
- 未适配的旧版文档、表格、多维表格、思维笔记、幻灯片等保留目录元数据并标记为“仅目录”。
- 将飞书邮箱作为聊天、知识库之外的第三条独立同步通道，通过 Mail OpenAPI 同步收件箱和已发送邮件、正文及附件。
- 邮箱使用独立的 `mail.sqlite3`、`mail/blobs` 内容寻址存储和 `mail-sync.lock`，不会把邮件写入聊天/知识库数据库，也不会共用同步锁。
- 邮箱阅读器使用短期本机会话；邮件 HTML 不直接渲染，附件只以强制下载方式提供；HTML、SVG、脚本和可执行格式还需要二次风险确认。
- 保存本人和其他人发送的图片并直接显示；普通文件只归档其他人或机器人发送的文件。
- 单个资源限制为 100 MB；消息资源和知识库资源分别设置本地总容量上限。
- macOS 每天 03:30 增量同步消息、03:45 增量同步知识库、04:00 增量同步邮箱，阅读器也提供手工同步按钮。
- 离线阅读器不加载 CDN、外部字体、统计脚本或其他网络资源。
- 单个会话可导出 JSON 或自包含 HTML。

## 通用准备

无论使用哪种系统，都应先准备：

1. 64 位操作系统和 Python 3.11 或更高版本。
2. Git，或从 GitHub 下载源码 ZIP。
3. 至少 10 GiB 可用磁盘空间；启用邮箱附件同步时，运行保护要求保留超过 100 GiB 可用空间。
4. 本机端口 `8765` 用于阅读器；真实 OAuth 授权还需要 `8766`。
5. 只通过 `127.0.0.1` 或 `localhost` 访问阅读器，不要配置公网反向代理。

当前开发分支尚未合并到 `main` 时，可明确克隆该分支：

```bash
git clone --branch agent/feishu-archive-poc --single-branch \
  https://github.com/FermiWang/FeishuLocalization.git
cd FeishuLocalization
```

已经合并到 `main` 后，可改用普通的 `git clone`。Python 官方文档说明了 [macOS 安装方式](https://docs.python.org/3/using/mac.html) 和 [虚拟环境用法](https://docs.python.org/3/library/venv.html)。

## macOS：完整部署

macOS 是当前唯一支持真实 OAuth、聊天同步、知识库同步、邮箱同步和每日计划任务的完整平台。

### 1. 安装并确认 Python

从 [Python.org macOS 下载页](https://www.python.org/downloads/macos/) 安装受支持版本，或使用已有的稳定 Python 3.11+。不要修改 Apple 管理的 `/usr/bin/python3`。

```bash
python3 --version
python3 -c 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)'
```

项目运行时没有第三方 Python 依赖。直接从源码执行即可：

```bash
./bin/feishu-archive --version
```

也可以为开发测试创建虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
feishu-archive --version
```

`scripts/install-local.sh` 会把当时找到的 `python3` 路径写入 LaunchAgent。正式安装后台服务前，应退出一次性虚拟环境，并确保 `command -v python3` 指向长期保留的 Python；否则删除或移动源码目录后，后台服务可能找不到解释器。

### 2. 先用隔离目录验证阅读器

不要把示例数据写进真实档案目录：

```bash
DEMO_ARCHIVE="$HOME/Library/Application Support/Feishu Archive Demo"
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" init
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" demo
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" serve
```

浏览器打开 <http://127.0.0.1:8765>。确认页面正常后按 `Ctrl+C` 停止演示服务。

### 3. 创建飞书企业自建应用

在飞书开放平台创建企业自建应用，开启机器人能力，并申请、发布下列权限：

- `im:message:readonly`
- `im:message.p2p_msg:get_as_user`
- `im:message.group_msg:get_as_user`
- `im:chat:readonly` 或开放平台当前显示的等价群信息只读权限
- `im:chat.members:read`
- `search:message`
- `wiki:wiki:readonly`
- `docx:document:readonly`
- `drive:drive:readonly`
- `offline_access`

在应用安全设置中添加回调地址：

```text
http://127.0.0.1:8766/oauth/callback
```

权限变更必须发布新版本，然后重新执行 OAuth；只在后台勾选但未发布，不会改变已授权令牌。

邮箱同步还需要在飞书开放平台为应用申请并发布以下只读权限；此列表与源码中的 `MAIL_SCOPES` 保持一致：

- `mail:user_mailbox:readonly`
- `mail:user_mailbox.folder:read`
- `mail:user_mailbox.message:readonly`
- `mail:user_mailbox.message.subject:read`
- `mail:user_mailbox.message.address:read`
- `mail:user_mailbox.message.body:read`
- `offline_access`

推荐为邮箱准备独立的企业自建应用，使邮件授权和聊天/知识库授权保持边界清晰。也可以复用主应用，但必须在主应用中保留原有权限、追加上述邮箱权限、发布新版本，并重新执行 `mail-auth`；原有 `auth` 令牌不会自动取得新增邮箱权限。

### 4. 保存凭据并完成 OAuth

先在开放平台复制 App ID：

```bash
pbpaste | ./bin/feishu-archive configure --app-id-stdin
```

再复制 App Secret：

```bash
pbpaste | ./bin/feishu-archive configure --app-secret-stdin
```

命令不会回显凭据。也可以临时设置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和 `FEISHU_REDIRECT_URI`，但 OAuth 用户令牌仍会写入 macOS Keychain。

启动授权：

```bash
./bin/feishu-archive auth
```

浏览器完成授权后，终端应显示授权成功和实际权限范围。若浏览器没有自动打开：

```bash
./bin/feishu-archive auth --no-open
```

复制命令输出的授权链接到浏览器，并保持当前终端运行，直到 `8766` 回调完成。

如使用独立邮箱应用，按相同方式把它的 App ID 和 App Secret 保存到邮箱专用 Keychain 项：

```bash
pbpaste | ./bin/feishu-archive mail-configure --app-id-stdin
pbpaste | ./bin/feishu-archive mail-configure --app-secret-stdin
./bin/feishu-archive mail-auth
```

如复用主应用，不需要执行 `mail-configure`；发布邮箱权限后直接执行 `mail-auth`。程序会以“原聊天/知识库权限 + 邮箱只读权限”的并集发起授权，但邮箱令牌固定写入 `{app_id}:mail:*` Keychain 命名空间，不会覆盖聊天/知识库使用的 `{app_id}:*` 令牌。独立邮箱应用仍是推荐方案，因为它还能在开放平台的应用、权限发布和撤销层面保持隔离。浏览器不能自动打开时可使用 `mail-auth --no-open`。

### 5. 首次发现和完整同步

首次回溯可能需要较长时间，并受租户保留期、机器人群成员身份、历史消息可见性和接口限流影响。

```bash
# 发现群聊和有可见消息的单聊
./bin/feishu-archive discover

# 同步全部已发现会话的全部可获取历史
./bin/feishu-archive sync --all-discovered

# 同步全部可见知识空间
./bin/feishu-archive wiki-sync

# 检查数据库、磁盘、FileVault 和授权状态
./bin/feishu-archive doctor

# 同步全部系统与自定义邮件文件夹的全部可获取历史
./bin/feishu-archive mail-sync

# 检查独立邮件库、授权范围、容量和本机安全边界
./bin/feishu-archive mail-status
./bin/feishu-archive mail-doctor
```

常用的范围和容量参数：

```bash
# 只同步最近 30 天
./bin/feishu-archive sync --all-discovered --days 30

# 只同步指定会话
./bin/feishu-archive sync --chat-id oc_xxx

# 不下载消息资源
./bin/feishu-archive sync --all-discovered --skip-attachments

# 续传资源，最多 4 路并发
./bin/feishu-archive attachments --workers 4

# 分别把消息和知识库资源总量限制为 10 GiB
./bin/feishu-archive sync --all-discovered --max-attachment-gib 10
./bin/feishu-archive wiki-sync --max-asset-gib 10

# 只同步一个知识空间，或强制重新生成文档
./bin/feishu-archive wiki-sync --space-id spc_xxx
./bin/feishu-archive wiki-sync --force

# 只使用本地原始内容块升级正文显示，不访问飞书也不重新下载附件
./bin/feishu-archive wiki-rebuild

# 排查或开发时强制重建，即使渲染版本未变化
./bin/feishu-archive wiki-rebuild --force

# 只同步指定文件夹；可重复使用系统 ID、自定义文件夹 ID、名称或父/子路径
./bin/feishu-archive mail-sync --folder INBOX
./bin/feishu-archive mail-sync --folder DRAFT --folder 7421369296749756417

# 邮箱元数据与正文照常同步，但不下载附件
./bin/feishu-archive mail-sync --skip-attachments

# 如需临时限时同步，可显式限定最近 30 天并调整容量上限
./bin/feishu-archive mail-sync --days 30 --max-mail-gib 10 --max-attachment-mib 1024

# 手工执行与每日任务相同的 2 天重叠增量同步
./bin/feishu-archive mail-scheduled-sync --days 2
```

`wiki-rebuild` 用于阅读器升级后的本地迁移。它会保留已有消息、知识空间、正文原始块和资源文件，只重新生成正文 HTML 与离线导出。
macOS 的 `scripts/install-local.sh` 会在启动新版阅读器前自动执行一次；渲染版本没有变化时会直接跳过。

手工邮箱同步默认按飞书原始文件夹 ID 分页枚举收件箱、已发送、草稿、定时发送、垃圾箱、垃圾邮件、归档和全部自定义文件夹，覆盖 Mail OpenAPI 当前可获取的全部历史；定时发送按 `SCHEDULED` 标签枚举。只有显式传入 `--days N` 时才按飞书要求的系统别名或 `父目录/子目录` 路径执行限时搜索。阅读器中的“同步邮箱”同样执行全历史枚举；每天 04:00 的计划任务仍只重叠回看最近 2 天，用于补收延迟出现或状态变化的邮件。邮箱分页总预算默认为 5000 页。邮箱内容与附件合计默认最多写入 10 GiB，单个附件默认最多 1 GiB。可用空间低于 75 GiB 或磁盘使用率达到 97% 时，邮箱同步硬停止；可用空间低于 100 GiB 或使用率达到 95% 时，暂停下载附件并保留邮件同步结果。容量门槛是保护本机磁盘的运行条件，不应通过修改状态库或删除锁文件绕过。

### 6. 安装 macOS 后台服务

确认前台命令正常后再安装：

```bash
deactivate 2>/dev/null || true
command -v python3
./scripts/install-local.sh
```

安装脚本会：

- 在停止现有服务前，用候选运行时预检邮件 schema、FTS 和解锁密钥；
- 把稳定运行副本保存到 `~/Library/Application Support/Feishu Archive/runtime`；
- 注册阅读器 LaunchAgent `com.fermiwang.feishu-archive`；
- 注册每天 03:30 的消息同步任务；
- 注册每天 03:45 的知识库同步任务；
- 注册每天 04:00 的独立邮箱同步任务 `com.fermiwang.feishu-archive-mail-sync`；
- 把日志写入 `~/Library/Application Support/Feishu Archive/logs`；
- 只让阅读器监听 <http://127.0.0.1:8765>。

候选运行时通过预检后，脚本会临时保留上一版运行时和 LaunchAgent；如果后续正文重建、plist 安装、服务启动或健康检查失败，会自动恢复上一版并重新注册原服务。只有新版阅读器通过 `/api/status` 健康检查后，回滚副本才会删除。

邮箱计划任务使用独立的 `mail-sync.lock`。开放平台权限尚未发布、尚未执行 `mail-auth` 或授权已经失效时，任务会安全跳过，不会降级到 IMAP，也不会影响 03:30 的聊天同步或 03:45 的知识库同步。

验证服务：

```bash
curl --fail http://127.0.0.1:8765/api/status
curl --fail http://127.0.0.1:8765/api/wiki/status
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive"
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-mail-sync"
./bin/feishu-archive mail-status
./bin/feishu-archive mail-doctor
```

邮箱 API 在未建立本机会话时应返回 `401`。使用下列命令生成并打开带 URL fragment 的解锁地址；fragment 不会作为 HTTP 请求的一部分发送给服务端，页面只用它交换短期、仅驻留内存的本机会话：

```bash
curl -i http://127.0.0.1:8765/api/mail/status
./bin/feishu-archive mail-reader-url --open
```

查看日志：

```bash
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/service.error.log"
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/sync.error.log"
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/wiki-sync.error.log"
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/mail-sync.error.log"
```

移除后台服务但保留档案数据：

```bash
./scripts/uninstall-local.sh
```

## Linux：演示和离线阅读部署

Linux 当前可以运行源码、SQLite、FTS5 和本地网页阅读器，但真实 OAuth 和同步会因为 macOS Keychain 依赖而失败。以下步骤只适用于示例数据、界面开发或阅读已经兼容的本地数据。

### 1. 准备 Python 和源码

使用发行版包管理器安装 Python 3.11+、`venv` 和 Git，然后执行：

```bash
git clone --branch agent/feishu-archive-poc --single-branch \
  https://github.com/FermiWang/FeishuLocalization.git
cd FeishuLocalization

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
feishu-archive --version
```

### 2. 使用 Linux 数据目录启动

项目默认目录仍是 macOS 风格，因此 Linux 必须显式指定目录：

```bash
export FEISHU_ARCHIVE_DIR="$HOME/.local/share/feishu-archive"
feishu-archive init
feishu-archive demo
feishu-archive serve --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。不要点击同步按钮；它会尝试访问尚未适配的 macOS Keychain。

### 3. 可选：配置 systemd 用户服务

该服务只保持离线阅读器运行，不会启用真实同步。将下列内容保存到 `~/.config/systemd/user/feishu-archive-reader.service`，并确保仓库路径为 `~/FeishuLocalization`：

```ini
[Unit]
Description=Feishu Archive local reader
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/FeishuLocalization
Environment=FEISHU_ARCHIVE_DIR=%h/.local/share/feishu-archive
ExecStart=%h/FeishuLocalization/.venv/bin/python -m feishu_archive serve --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

启用并验证：

```bash
systemctl --user daemon-reload
systemctl --user enable --now feishu-archive-reader.service
systemctl --user status feishu-archive-reader.service
curl --fail http://127.0.0.1:8765/api/status
journalctl --user -u feishu-archive-reader.service -f
```

没有 systemd 的发行版可直接运行 `feishu-archive serve`，或使用本机现有的用户级进程管理器。

## Windows：通过 WSL2 运行演示和阅读器

Windows 原生 Python 当前不能导入 `fcntl`，因此不要在 PowerShell 中直接运行 `python -m feishu_archive`。现阶段可使用 WSL2 获得 Linux 兼容环境，但 OAuth 和真实同步仍不可用。

### 1. 安装 WSL2

以管理员身份打开 PowerShell：

```powershell
wsl --install
```

按提示重启并创建 Linux 用户。具体要求和旧版 Windows 的处理方式见 [Microsoft WSL 安装文档](https://learn.microsoft.com/windows/wsl/install)。

### 2. 在 WSL 发行版内安装

下面的命令必须在 Ubuntu、Debian 等 WSL 终端内执行，不是在 PowerShell 内执行：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv

cd ~
git clone --branch agent/feishu-archive-poc --single-branch \
  https://github.com/FermiWang/FeishuLocalization.git
cd FeishuLocalization

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

export FEISHU_ARCHIVE_DIR="$HOME/.local/share/feishu-archive"
feishu-archive init
feishu-archive demo
feishu-archive serve --host 127.0.0.1 --port 8765
```

通常可在 Windows 浏览器打开 <http://localhost:8765>。源码、虚拟环境和档案目录建议放在 WSL 的 Linux 文件系统中，不要放到 `/mnt/c`；这可以减少权限语义差异和 SQLite 文件访问问题。

如果 WSL 已启用 systemd，可复用上一节的用户服务配置。WSL 停止或注销后是否持续运行取决于本机 WSL 和 systemd 设置，不能等同于 Windows 原生服务。

## 在另一台机器上迁移已有档案

档案不是只复制一个 SQLite 文件就能完整迁移。应处理聊天与知识库数据库、独立邮件数据库、WAL、附件、知识库资源、邮件 blob、HTML 导出和凭据边界。

### 安全迁移流程

1. 在源机器停止阅读器和聊天、知识库、邮箱三个同步任务，避免复制过程中继续写入。
2. 复制整个档案根目录，而不是只复制 `archive.sqlite3`。
3. 不复制 `.venv`；在目标机器重新创建虚拟环境。
4. macOS Keychain 项不会随档案目录复制；目标 Mac 需要重新配置应用凭据并执行 OAuth。
5. 将目标目录权限限制为仅当前用户可读写，并确认磁盘加密已开启。
6. 启动前执行 SQLite 完整性检查；在 macOS 上可直接运行 `doctor`。

### 当前迁移限制

- 运行中直接复制 SQLite 可能遗漏 `archive.sqlite3-wal` 中的数据。
- 邮件保存在独立的 `mail.sqlite3` 和 `mail/blobs`；只复制其中一项会得到不完整的邮件档案，运行中复制也可能遗漏 `mail.sqlite3-wal`。
- 聊天附件主要使用档案根目录下的相对路径，整目录迁移后通常仍可读取。
- 知识库资源数据库目前保存绝对本机路径。跨用户名、跨目录或跨系统复制后，阅读器中的知识库图片和附件可能无法打开。
- `knowledge/exports` 中的 HTML 使用相对资源路径，更适合在保持目录结构时独立迁移和查看。
- 不要把 macOS Keychain、明文 App Secret 或 OAuth 令牌打包进公开备份。

## Windows 和 Linux 完整同步所需改造

要把平台表中的“暂不支持”升级为完整支持，至少需要完成并测试：

1. 把 `KeychainStore` 抽象为多后端凭据存储：macOS Keychain、Windows Credential Manager/DPAPI、Linux Secret Service；无安全存储时应拒绝保存，而不是退化为明文文件。
2. 用跨平台实现替换或封装 `fcntl.flock`，并验证多进程重复同步保护。
3. 按平台选择默认数据目录：macOS Application Support、Windows Local AppData、Linux XDG Data Home。
4. 把知识库资源路径改为相对档案根目录，并提供旧数据库迁移。
5. 将 `doctor` 分为 FileVault、BitLocker 和 LUKS/发行版磁盘加密检查。
6. 增加 systemd timer 和 Windows Task Scheduler 安装/卸载脚本。
7. 在 GitHub Actions 中增加 macOS、Ubuntu 和 Windows 测试矩阵，再把 README 支持状态改为“支持”。

在这些改造进入代码和自动化测试之前，不应对外承诺 Windows 或 Linux 的完整飞书归档能力。

## 安全边界

- 阅读器只允许绑定 `127.0.0.1`、`::1` 或 `localhost`。
- OAuth `state` 会校验，授权码只在本地回调服务中交换。
- macOS 上的应用凭据和令牌保存在 Keychain，日志和 SQLite 不写入这些值。
- 邮箱 API 需要短期、仅驻留服务进程内存的本机会话；解锁密钥通过 URL fragment 交给页面，不写入查询字符串或服务访问日志。
- 邮件 HTML 正文不在阅读器中直接渲染，避免远程资源、脚本和跟踪像素被激活；附件响应固定为强制下载，不以内联方式打开。
- HTML、SVG、脚本、XML 和可执行附件标记为 `quarantined`；直接下载会被拒绝，只有阅读器显示风险提示并由用户二次确认后才提供强制下载。
- 邮箱数据、内容 blob、同步状态和进程锁分别位于 `mail.sqlite3`、`mail/blobs` 和 `mail-sync.lock`，不与聊天/知识库通道混写。
- 邮件内容使用 SHA-256 内容寻址；重叠同步会复核已有文件的大小与摘要并修复损坏对象，`mail-doctor` 会扫描索引中的 blob 完整性。
- SQLite、FTS 索引、附件和 HTML 导出仍是本机文件；macOS 建议启用 FileVault。
- App Secret 只从环境变量或 macOS Keychain 读取，不写入项目配置文件。
- 导出文件是明文副本，需要自行控制保存位置、备份和传播范围。
- 不要把阅读器改为监听 `0.0.0.0`，也不要直接暴露到局域网或公网。

## 常见故障

| 现象 | 原因与处理 |
| --- | --- |
| `0 个会话 · 0 条消息` | 服务可能正常，但真实档案还没有数据。依次检查应用凭据、OAuth、`discover` 和 `sync --all-discovered`。不要用 demo 数据冒充真实同步。 |
| 浏览器没有出现飞书登录页 | 运行 `auth --no-open` 并手工打开链接；同时保持终端运行，确认 `8766` 未被占用。 |
| OAuth 后仍提示缺少权限 | 在开放平台发布新增权限后重新执行 `auth`；旧令牌不会自动获得新权限。 |
| `mail-auth` 后仍提示缺少邮箱权限 | 核对 `MAIL_SCOPES` 对应的只读权限是否已经发布，而不只是保存在开放平台草稿中；随后重新执行 `mail-auth`。复用主应用时也必须重新授权。 |
| 邮箱计划任务显示跳过 | 尚未配置/发布邮箱权限或没有有效邮箱 OAuth 令牌时属于安全行为；完成 `mail-auth` 后再运行 `mail-doctor`，聊天和知识库同步不受影响。 |
| 邮件正文存在但附件未下载 | 检查 1 GiB 单附件上限、10 GiB 邮件总量上限，以及 100 GiB/95% 的附件暂停阈值；达到 75 GiB/97% 硬停止阈值时应先释放磁盘空间。 |
| 风险格式附件不能直接下载 | `quarantined` 附件需要在本机阅读器中阅读风险提示并二次确认；不要通过改数据库状态或绕过确认链接来打开不可信文件。 |
| 直接访问 `/api/mail/*` 返回 `401` | 邮箱阅读器要求短期本机会话；运行 `mail-reader-url --open` 重新解锁，不要把 fragment 中的密钥复制到日志或聊天中。 |
| 单聊数量明显少于飞书客户端 | 群列表接口不返回单聊；需要 `search:message`，且超出租户保留期或不可搜索的单聊仍无法发现。 |
| 图片或附件缺失 | 检查机器人是否在会话内、资源是否超过 100 MB、是否受防泄密策略限制，并运行 `attachments --workers 4`。 |
| 知识空间有节点数量但正文显示方式仍旧 | 运行 `wiki-rebuild`，使用已保存的原始内容块重建正文；无需执行 `wiki-sync --force`。 |
| `Address already in use` | `8765` 或 `8766` 已被占用；停止旧进程，或为前台测试指定其他端口并同步修改飞书回调地址。 |
| Linux 提示找不到 `/usr/bin/security` | 当前真实 OAuth 不支持 Linux；只能运行演示或阅读器，不能用明文脚本伪造钥匙串。 |
| Windows 提示没有 `fcntl` | 原生 Windows 尚未支持；改用 WSL2 运行演示，或先完成跨平台锁改造。 |
| macOS TLS 证书错误 | 使用受支持的 Python 安装，并完成其证书安装步骤；必要时通过 `SSL_CERT_FILE` 指向可信 CA 文件。 |
| SQLite 被锁定或迁移后损坏 | 停止服务后复制整个档案目录，不要在同步运行时只复制主数据库文件。 |

## 检查与测试

macOS 运行安全和授权检查：

```bash
./bin/feishu-archive doctor
```

在 macOS 或 Linux 开发环境运行测试：

```bash
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
sh -n scripts/install-local.sh
sh -n scripts/uninstall-local.sh
```

当前测试主要在 macOS 验证；在完成 Windows 兼容改造前，不能把测试通过解释为原生 Windows 支持。

## 飞书官方覆盖限制

- 获取指定消息和资源仍要求应用开启机器人能力，机器人需要位于消息所属会话。
- 群列表接口不包含单聊；程序使用消息搜索的空查询结果补发现 `p2p` 会话，但不可见或已超出租户保留期的单聊仍无法发现。
- 普通对话群中，按 `chat` 查询只能取得话题根消息；回复需要再按 `thread` 查询。
- `thread` 查询不支持 `start_time` 和 `end_time`，程序拉取后在本地按时间范围过滤。
- 资源接口限制单文件不超过 100 MB，不支持部分卡片、合并转发子消息、表情包和防泄密资源。
- 被删除、撤回、超过租户保留期限或受历史消息可见性限制的内容无法补救性恢复。
- 知识库只保存 OAuth 用户当前可见的空间和节点；暂时不可见的旧节点会标记为 `missing`，不会因一次权限变化静默删除本地正文。
- 当前完整正文适配覆盖新版文档和普通文件；旧版文档、电子表格、多维表格、思维笔记、幻灯片及第三方组件只保存目录元数据。
- 知识库附件同样限制单文件不超过 100 MB；超限或受防泄密策略限制时，正文仍会保留并记录资源错误。
- 邮箱通道只覆盖 Mail OpenAPI 和当前 OAuth 用户可见的数据，不尝试从 IMAP 或飞书客户端私有数据库补齐；被删除、超出接口可见范围或受租户策略限制的邮件无法恢复。
- 邮箱手工同步默认按文件夹 ID 覆盖 7 个系统目录与 Mail OpenAPI 返回的全部自定义文件夹的全部可获取历史；每天 04:00 仍按 2 天重叠窗口增量同步。可用范围仍受租户保留策略、当前用户权限和 Mail OpenAPI 可见性约束；远端永久删除尚不会自动映射为本地删除或墓碑记录。

飞书接口参考：

- [获取会话历史消息](https://open.feishu.cn/document/server-docs/im-v1/message/list)
- [话题概述](https://open.feishu.cn/document/im-v1/message/thread-introduction)
- [获取用户或机器人所在的群列表](https://open.feishu.cn/document/server-docs/group/chat/list)
- [搜索消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/search)
- [获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)
- [浏览器网页授权接入指南](https://open.feishu.cn/document/sso/web-application-end-user-consent/guide)
- [获取知识空间列表](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space/list)
- [获取知识空间子节点列表](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/list)
- [获取新版文档纯文本内容](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/raw_content)
- [获取新版文档所有块](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/list)
- [飞书邮箱 Mail OpenAPI](https://open.feishu.cn/document/server-docs/mail-v1/user_mailbox)
- [获取邮箱文件夹列表](https://open.feishu.cn/document/mail-v1/user_mailbox-folder/list)
- [获取文件夹或标签中的邮件列表](https://open.feishu.cn/document/mail-v1/user_mailbox-message/list)
- [搜索邮件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/mail-v1/user_mailbox/search)

## 项目阶段

当前已经完成 macOS 上的聊天历史回溯、知识库新版文档与文件节点离线化，以及基于飞书 Mail OpenAPI 的第三条独立邮箱同步通道；聊天、知识库、邮箱分别执行每日增量同步。邮箱投入真实运行前，仍须先在飞书开放平台发布 `MAIL_SCOPES` 对应权限并完成 `mail-auth`，未授权的计划任务只会安全跳过。下一阶段若要面向任何 macOS、Windows 和 Linux 机器发布，应优先完成多平台凭据存储、锁、数据目录、资源路径、磁盘加密检查、后台任务安装器和 CI 测试矩阵，再发布新的平台支持声明。
