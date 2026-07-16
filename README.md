# Feishu Archive

Feishu Archive 是一个面向 macOS 的飞书离线归档 PoC：只通过飞书官方授权接口同步用户可见的消息，将消息、同步状态、图片与文件保存在本机，并通过只监听 `127.0.0.1` 的阅读器进行搜索和导出。

> 项目不会读取、复制或尝试解密 `LarkShell`、`messages.db`、`im.db`、`Core.db` 等飞书客户端私有数据。

## 当前 PoC 能力

- OAuth 2.0 用户授权，`access_token` 与 `refresh_token` 仅存 macOS 钥匙串。
- 使用用户令牌发现群聊和有可见消息的单聊，默认同步全部可获取历史。
- 普通群消息包含 `thread_id` 时，自动二次同步话题回复。
- SQLite 保存会话、成员、消息、编辑/删除状态、资源清单与同步运行记录。
- SQLite FTS5 全文搜索，支持会话、日期、人员和消息类型筛选。
- 完整归档本人和其他人发送的图片，并在消息页面中直接显示；普通文件只归档其他人或机器人发送的文件。
- 下载前检查单个资源 100 MB 上限和本地总容量上限。
- 每天 03:30 自动执行增量同步，阅读器也提供“立即同步”按钮。
- 完全本地的离线网页阅读器；不加载 CDN、字体、统计或其他网络资源。
- 单个会话导出为 JSON 或自包含 HTML。
- 示例数据模式，无飞书应用凭据也能验证完整阅读闭环。

## 安全边界

- 阅读器只允许绑定 `127.0.0.1`、`::1` 或 `localhost`。
- OAuth `state` 会校验，授权码只在本地回调服务内交换。
- 应用凭据和令牌由 macOS Keychain 保存，日志和 SQLite 中不写入这些敏感值。
- SQLite、FTS 索引和附件由本机 FileVault 提供静态加密保护；`doctor` 会检查 FileVault 状态。
- App Secret 只从环境变量或 macOS Keychain 读取，不写入配置文件。
- 导出文件是明文副本，用户需要自行控制其保存位置和传播范围。

## 1. 运行

```bash
./bin/feishu-archive --version
```

项目运行时只依赖 macOS 自带能力和 Python 3.11+，不需要联网安装第三方包。需要标准 Python 命令入口的开发者也可以使用 `python -m pip install -e .`。
网络请求在 macOS 上显式使用系统 CA 证书包 `/etc/ssl/cert.pem`，避免独立 Python 安装缺少默认证书路径时误报 TLS 校验失败。

## 2. 无凭据验证

```bash
./bin/feishu-archive init
./bin/feishu-archive demo
./bin/feishu-archive serve
```

浏览器打开 <http://127.0.0.1:8765>。默认档案目录为：

```text
~/Library/Application Support/Feishu Archive
```

可通过 `--archive-dir` 指向独立测试目录。

### 安装为本机后台服务

```bash
./scripts/install-local.sh
```

安装后会在当前 macOS 用户登录时自动启动，只监听 <http://127.0.0.1:8765>。另一个 LaunchAgent 每天 03:30 自动发现会话并同步；新发现会话完整回溯，已有会话重叠同步最近 2 天。稳定运行副本位于档案目录的 `runtime/`，阅读器和同步日志均位于 `logs/`。

阅读器左侧的“立即同步”按钮执行同一套增量流程，并在界面显示进行中、成功、部分完成或失败状态。跨进程锁会阻止手工同步与计划任务重复运行。

移除后台服务但保留档案数据：

```bash
./scripts/uninstall-local.sh
```

## 3. 创建并配置飞书自建应用

在飞书开放平台创建企业自建应用，开启机器人能力，并申请、发布下列最小权限：

- `im:message:readonly`
- `im:message.p2p_msg:get_as_user`
- `im:message.group_msg:get_as_user`
- `im:chat:readonly` 或当前后台显示的等价群信息只读权限
- `im:chat.members:read`（用于把消息发送者 ID 映射为成员姓名）
- `search:message`（通过新版消息搜索接口补发现单聊 `chat_id`）
- `offline_access`

在安全设置中添加回调地址：

```text
http://127.0.0.1:8766/oauth/callback
```

推荐通过系统剪贴板把应用凭据分别保存到 macOS 钥匙串；命令不会回显凭据：

```bash
# 在开发者后台复制 App ID 后
pbpaste | ./bin/feishu-archive configure --app-id-stdin

# 再复制 App Secret
pbpaste | ./bin/feishu-archive configure --app-secret-stdin
```

也可以临时使用 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 环境变量；环境变量优先于钥匙串。

执行授权：

```bash
./bin/feishu-archive auth
```

授权完成后，应用凭据和用户令牌均不会写入仓库或档案数据库。

## 4. 发现与同步

```bash
# 发现群聊，并通过空关键词消息搜索补发现有可见消息的单聊
./bin/feishu-archive discover

# 默认同步全部已发现会话的全部可获取历史
./bin/feishu-archive sync --all-discovered

# 也可只同步指定会话，或显式限制为最近 30 天
./bin/feishu-archive sync --chat-id oc_xxx
./bin/feishu-archive sync --all-discovered --days 30

# 不下载图片和文件，仅核对消息覆盖率
./bin/feishu-archive sync --all-discovered --skip-attachments

# 续传未完成或失败的图片与收到的文件（默认 4 路并发）
./bin/feishu-archive attachments --workers 4

# 手工执行与每日计划任务相同的增量流程
./bin/feishu-archive scheduled-sync --days 2
```

图片会保存所有发送者的资源，包括本人发送的图片，并在离线阅读器的消息卡片中直接显示。普通文件只保存其他用户或机器人发送的文件；本人上传的普通文件不会下载，升级后首次同步还会删除此前已归档的本人普通文件。外部群、保密群或机器人不在群内时，程序会把接口错误写入同步运行记录，不会把它解释成空会话。

## 5. 容量控制

图片与普通文件的总容量默认上限为 20 GiB，可在命令中调整：

```bash
./bin/feishu-archive sync --all-discovered --max-attachment-gib 10
```

单个资源始终限制为 100 MB。达到总上限后，消息仍会保存，资源状态标记为 `skipped_capacity`。

## 6. 检查与测试

```bash
./bin/feishu-archive doctor
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 官方覆盖限制

- 获取指定消息和资源仍要求应用开启机器人能力，机器人需要位于消息所属会话。
- 群列表接口不包含单聊；程序改用新版消息搜索接口的空查询结果补发现 `p2p` 会话。没有可见消息、已超出租户保留期或被搜索可见性限制的单聊仍无法发现。
- 普通对话群中，按 `chat` 查询只能取得话题根消息；回复需要再按 `thread` 查询。
- `thread` 查询不支持 `start_time` / `end_time`，程序拉取后会在本地按时间范围过滤。
- 资源接口限制单文件不超过 100 MB，不支持部分卡片、合并转发子消息、表情包和防泄密资源。
- 被删除、撤回、超过租户保留期限或因历史消息可见性设置不可见的内容无法补救性恢复。

参考：

- [获取会话历史消息](https://open.feishu.cn/document/server-docs/im-v1/message/list)
- [话题概述](https://open.feishu.cn/document/im-v1/message/thread-introduction)
- [获取用户或机器人所在的群列表](https://open.feishu.cn/document/server-docs/group/chat/list)
- [搜索消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/search)
- [获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)
- [浏览器网页授权接入指南](https://open.feishu.cn/document/sso/web-application-end-user-consent/guide)

## 项目阶段

当前是已完成真实全历史回溯和每日增量调度的本机 PoC。后续阶段可继续增加应用级加密、签名安装包和企业级集中管理。
