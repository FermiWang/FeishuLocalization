# FeishuLocalization（Feishu Archive）手工部署与飞书配置作业指导书（SOP V1.2）

**文件性质：** 普通用户手工部署与配置标准作业指导书
**适用项目：** FermiWang/FeishuLocalization
**项目程序名称：** Feishu Archive
**文档版本：** V1.2
**适用源码基线：** Feishu Archive v0.5.4 / 与本文位于同一 Git commit（在 GitHub 文档页面通过“History”查看，或安装后执行 `git rev-parse HEAD` 记录）
**核验日期：** 2026年8月24日
**编制日期：** 2026年8月24日
**适用对象：** 不具备 Python、Git、服务器运维经验的普通飞书用户
**推荐运行平台：** macOS
**目标：** 不依赖 Codex 自动部署，由普通用户通过复制命令、飞书网页配置和少量本机操作，部署上述源码基线中已经实现的功能，并明确飞书 OpenAPI、租户策略和当前适配范围造成的边界。

> **版本提示：** GitHub `main` 会继续变化。本文中的默认时区、模型、端口、计划任务频率和命令均以本文所在 commit 为准。升级源码后，应重新核对 README、`./bin/feishu-archive --help`、`src/feishu_archive/config.py` 和 `scripts/install-local.sh`，不能把本文当作以后所有版本都不变的说明。

---

# 阅读路线

首次部署建议按本文顺序操作，不要跳过首次同步、健康检查和手工验证：

1. 第一至第三阶段：准备 Mac、安装 Python、验证演示阅读器；
2. 第四至第十六阶段：配置飞书主应用和可选的邮箱独立应用；
3. 第十七至第二十五阶段：完成首次聊天、知识库、邮箱同步和阅读器验收；
4. 第二十六至第二十七阶段：只安装并验证不调用 AI 的核心后台任务；
5. 第二十八阶段以后：按需配置并人工验证 vMLX Insights，再显式启用自动洞察和历史回填，最后完成备份与升级准备。

只需要归档功能、不需要 AI 的用户，完成第二十七阶段即可。附录 A 是部署完成后的命令速查卡，不能代替前面的权限、边界和安全说明。

---

# 一、先理解：这个项目最终能实现什么

Feishu Archive 是一个**本地优先的飞书离线归档系统**。它通过飞书官方 OpenAPI 和当前用户 OAuth 授权读取该用户有权访问的数据，在自己的 Mac 上建立独立离线档案，并提供本地网页阅读器。

它不会读取、破解或复制飞书客户端自己的 `LarkShell`、`messages.db`、`im.db`、`Core.db` 等内部数据库；邮箱也不是通过 IMAP，而是使用飞书 Mail OpenAPI。

完成本指导书以后，一台 Mac 可以形成如下工作链：

**飞书账号**

→ OAuth 用户授权
→ 获取当前用户有权限看到的聊天、知识库和邮箱
→ 定期增量同步
→ 本机 SQLite 数据库
→ 图片、文件和附件本地保存
→ 本机全文索引
→ `127.0.0.1:8765` 离线网页阅读器
→ 搜索、阅读、导出
→ 可选接入本地/局域网 vMLX 大模型
→ 自动形成每日工作洞察、待办和商业机会线索
→ 自动历史回填

当前代码实际包含的主要能力包括：

| 功能 | 当前状态 |
| --- | --- |
| 群聊发现 | 支持 |
| 可搜索单聊补发现 | 支持 |
| 群聊历史消息 | 支持 |
| Thread/话题回复 | 支持 |
| 消息编辑/删除状态保存 | 支持 |
| 图片归档与显示 | 支持 |
| 普通文件归档 | 其他人或机器人发送的文件支持；本人发送的普通文件当前只保留消息和资源元数据，不保存文件本体 |
| 聊天全文搜索 | 支持，SQLite FTS5 |
| 单会话 JSON 导出 | 支持 |
| 单会话自包含 HTML 导出 | 支持消息文字和样式；不嵌入图片/附件 |
| 知识空间发现 | 支持 |
| 知识库目录树 | 支持 |
| 新版飞书文档正文 | 支持 |
| 文档 Block | 支持 |
| 文档内嵌图片及附件 | 支持 |
| 普通文件节点 | 支持 |
| 知识库全文索引 | 支持 |
| 本地 HTML 文档副本 | 支持 |
| 飞书邮箱 | 支持 |
| 邮件正文 | 支持 |
| 邮件附件 | 支持 |
| 系统和自定义邮件文件夹 | 支持 |
| 邮箱独立数据库 | 支持 |
| 邮件附件危险格式隔离 | 支持 |
| 自动增量同步 | macOS 支持 |
| 每日工作洞察 | 支持，但需要额外的 vMLX 模型服务器 |
| 历史每日洞察回填 | 支持 |
| Windows 原生完整同步 | 当前不支持 |
| Linux 原生完整同步 | 当前不支持 |

上述能力范围由指定源码基线的 README、CLI 和源代码共同确定；“支持”表示程序存在相应处理路径，不代表飞书 OpenAPI 能返回客户端中曾经出现过的每一项内容。

---

# 二、非常重要的五个结论

## 2.1 要实现“全部现有功能”，请选择 Mac

当前版本真正完成了：

**OAuth → 飞书真实数据同步 → Keychain 安全保存令牌 → 后台自动同步 → 本地阅读器**

这一完整链路的操作系统是 **macOS**。

Linux 可以运行示例数据库和阅读器，但真实 OAuth/同步尚未完成安全凭据存储适配。

Windows 原生 Python 当前还存在 `fcntl` 等兼容性问题；WSL2 也只能用于演示和阅读器，不能等同于完整同步环境。

因此本指导书的标准操作环境统一定义为：

> **macOS + Python 3.11 或更高版本。**

---

## 2.2 不需要配置“事件订阅”或 Webhook

这是普通用户最容易配错的地方。

很多飞书机器人教程会要求：

> 事件订阅 → 配置公网 URL → 接收消息事件。

**本项目当前不采用这种机制。**

当前代码使用的是：

**OAuth + OpenAPI 主动拉取 + macOS LaunchAgent 定时同步。**

因此：

- 不需要服务器公网 IP；
- 不需要域名；
- 不需要 HTTPS Webhook；
- 不需要配置飞书“事件订阅请求地址”；
- 不需要订阅“收到消息”等事件；
- 不需要内网穿透；
- 不需要 ngrok；
- 不需要把 Mac 的 8765 端口暴露给互联网。

当前安装脚本始终创建聊天、知识库、邮箱和详细会议记录的本机计划任务；会议记录每 300 秒通过固定 SSH 命令从 179 增量拉取已完成的结构化修订，不复制录音或识别稿。只有明确使用 `--with-insights` 时才创建洞察和历史回填任务。

---

## 2.3 “GitHub 公开”不等于当前已经采用开源许可证

当前仓库虽然公开可见，但 `pyproject.toml` 中仍写明：

`Proprietary - no license granted`

并且当前仓库没有标准 OSI 开源许可证。

因此，如果是仓库所有者本人部署没有问题；如果准备提供给第三方单位复制、修改、分发或正式商业部署，应先处理 `LICENSE` 与项目元数据。

---

## 2.4 “全部功能”中的 AI 每日洞察需要额外模型服务器

聊天、知识库、邮箱、搜索、归档、阅读器等功能**不需要大模型**。

但是：

> 昨日小结
> 今日工作规划
> 待办识别
> 商业机会识别
> 历史洞察回填

需要能够访问一个 **vMLX/OpenAI-compatible 模型服务器**。

因此本系统可以理解成两层：

**第一层：Feishu Archive**

飞书 → 本地完整档案

**第二层：Insights**

本地档案 → 本地/受控大模型 → 工作洞察

模型出问题不会阻止飞书原始数据继续同步。

---

## 2.5 “全部历史”只表示 OpenAPI 当前允许取得的全部历史

Feishu Archive 不是飞书客户端数据库的镜像，也不能绕过租户的数据保留、可见范围、防泄密和权限策略。本文后面命令中的“全历史”应统一理解为：

> **当前 OAuth 用户、当前应用权限和飞书 OpenAPI 在同步时点允许返回的全部可获取历史。**

主要限制如下：

- 获取指定消息和资源通常要求应用开启机器人能力，且机器人位于消息所属会话；
- 群列表接口不返回 P2P 单聊，程序会用消息搜索补发现，但不可见或已超出租户保留期的单聊仍无法发现；
- 普通话题群按会话查询主要取得根消息，回复还要按 thread 查询；thread 接口不接受起止时间，程序取得后再在本地过滤；
- 消息和知识库资源单文件受 100 MB 上限约束，部分卡片、合并转发子消息、表情包和防泄密资源不支持下载；
- 本人发送的图片可以归档；本人发送的普通文件当前只保留消息和资源元数据，不下载文件本体，其他人或机器人发送的普通文件仍受会话成员身份、100 MB 和防泄密限制；
- 已删除、撤回、超过保留期限或受历史可见性限制的内容无法补救性恢复；
- 知识库只同步当前用户可见空间和节点；暂时不可见的旧节点会在本地标记为 `missing`，不会因一次权限变化静默删除已有正文；
- 当前完整正文适配覆盖新版文档和普通文件；旧版文档、电子表格、多维表格、思维笔记、幻灯片及第三方组件主要保存目录元数据；
- 邮箱只覆盖 Mail OpenAPI 对当前 OAuth 用户可见的数据；已删除、超出接口可见范围或受租户策略限制的邮件无法恢复；
- 远端永久删除邮件目前不会自动映射为本地删除或墓碑记录，因此本地档案可能继续保留已经从飞书永久删除的邮件。

因此，本项目适合本地检索、留存和辅助分析，但不能在未经抽样比对和制度评估的情况下被宣称为法律意义上的完整备份、电子取证镜像或灾难恢复系统。飞书官方接口链接见附录 E。

---

# 三、部署前准备

在开始之前准备以下条件。

| 项目 | 要求 |
| --- | --- |
| 电脑 | Mac |
| macOS 用户 | 使用自己的普通登录账号即可 |
| Python | 3.11或更高 |
| Git | 推荐；不会使用也可以下载 ZIP |
| 飞书账号 | 能正常登录所在企业 |
| 飞书应用权限 | 能创建企业自建应用，或请管理员协助 |
| 磁盘空间 | 普通使用至少预留10 GiB |
| 邮箱完整附件 | 推荐保持100 GiB以上可用空间 |
| 网络 | 可访问飞书开放平台 |
| 本地端口8765 | 阅读器使用 |
| 本地端口8766 | OAuth 回调使用 |
| FileVault | 强烈建议开启 |

程序默认数据目录为：

```
~/Library/Application Support/Feishu Archive
```

当前代码定义的阅读器端口为：

```
8765
```

OAuth 回调端口为：

```
8766
```

相关默认配置来自当前 `config.py`。

---

# 四、第一阶段：安装 Python

## 4.1 检查 Mac 有没有合适的 Python

打开：

**应用程序 → 实用工具 → 终端 Terminal**

复制下面一行：

```
python3 --version
```

如果显示类似：

```
Python 3.11.x
```

或：

```
Python 3.12.x
Python 3.13.x
```

即可继续。

如果低于 3.11，则安装新版 Python。

可以从 Python 官方 macOS 下载页面安装：

[Python macOS 下载页](https://www.python.org/downloads/macos/)

安装以后关闭 Terminal，再重新打开一次。

再次执行：

```
python3 --version
```

然后执行：

```
python3 -c 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)'
```

只要没有出现错误，并打印一个 Python 路径，即通过。

**不要删除或修改 `/usr/bin/python3`。**

---

# 五、第二阶段：取得 FeishuLocalization 源代码

官方仓库：

[FermiWang/FeishuLocalization](https://github.com/FermiWang/FeishuLocalization)

普通用户有两种办法。

---

## 5.1 方法A：下载 ZIP——最适合不会 Git 的用户

在 GitHub 项目页面点击：

**Code → Download ZIP**

下载完成以后解压。

得到类似：

```
FeishuLocalization-main
```

文件夹。

打开 Terminal。

在 Terminal 中手工输入 `cd`，再按一次空格键，先不要按 Enter。

然后直接把刚才的 **FeishuLocalization-main 文件夹拖进 Terminal 窗口**。Terminal 会自动填入文件夹的完整路径。

按 Enter。

接下来执行：

```
chmod +x bin/feishu-archive scripts/install-local.sh scripts/uninstall-local.sh
```

再执行：

```
./bin/feishu-archive --version
```

正确情况下应该看到：

```
0.5.4
```

---

## 5.2 方法B：Git——推荐长期使用

如果以后准备经常更新项目，建议用 Git。

执行：

```
cd ~
git clone https://github.com/FermiWang/FeishuLocalization.git
cd FeishuLocalization
```

然后：

```
./bin/feishu-archive --version
```

如果看到版本号，即成功。

---

# 六、第三阶段：先不要连接真实飞书，测试本地阅读器

这是非常重要的安全步骤。

先证明：

> Python正常
> 程序正常
> SQLite正常
> 浏览器页面正常

再连接自己的真实飞书。

依次执行：

```
DEMO_ARCHIVE="$HOME/Library/Application Support/Feishu Archive Demo"
```

```
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" init
```

```
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" demo
```

```
./bin/feishu-archive --archive-dir "$DEMO_ARCHIVE" serve
```

浏览器打开：

```
http://127.0.0.1:8765
```

如果可以看到示例会话，说明本机程序基本正常。

测试结束以后返回 Terminal，按：

```
Control + C
```

停止服务。

这个测试使用的是：

```
Feishu Archive Demo
```

与后面的真实档案目录完全分离，所以不会污染正式数据。

---

# 七、第四阶段：创建“主飞书应用”

主应用负责：

> 飞书登录授权
> 群聊
> 单聊
> 群成员
> 消息搜索
> 知识库
> 新版文档
> 云盘文件

打开：

[飞书开放平台应用控制台](https://open.feishu.cn/app)

OAuth 网页授权及回调地址可同时参考飞书官方的[浏览器网页授权接入指南](https://open.feishu.cn/document/sso/web-application-end-user-consent/guide)。

飞书当前的一般应用创建流程是：

**创建企业自建应用 → 凭证与基础信息 → 应用功能 → 权限管理 → 安全设置 → 版本管理与发布 → 管理员审批。** 飞书官方开发者资料也说明，新增权限只有在创建版本、发布并完成相应审批后才正式生效。

---

## 7.1 创建应用

选择：

**创建企业自建应用**

例如命名：

```
Feishu Archive
```

描述可以填写：

```
用于本人飞书聊天、知识库和文件的本地离线归档。
```

---

# 八、第五阶段：取得 App ID 和 App Secret

进入：

**凭证与基础信息**

找到：

```
App ID
App Secret
```

现在暂时不要把它们粘贴到微信、邮件、聊天机器人或普通文档。

尤其不要：

- 把 App Secret 写入 README；
- 上传到 GitHub；
- 写进 `.env` 后又提交 Git；
- 发到群里；
- 截图公开。

后面程序会把这些信息安全写入 **macOS Keychain**。

---

# 九、第六阶段：给主应用开启机器人能力

进入：

**应用功能 → 机器人**

点击：

**启用机器人**

机器人与会话/消息接口的实际覆盖条件可参考官方[获取会话历史消息](https://open.feishu.cn/document/server-docs/im-v1/message/list)和[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)文档。

注意：

> 本项目开启机器人不是为了让用户在群里 @机器人聊天。

它主要是为了满足飞书部分 IM 消息及资源接口的应用条件。

---

# 十、第七阶段：配置主应用权限

进入：

**权限管理**

逐项搜索下面的权限代码。

不要凭感觉选择“差不多”的权限。

应尽量按以下代码核对：

```
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

这是当前代码中 `DEFAULT_SCOPES` 的实际定义。

如果飞书后台没有直接显示：

```
im:chat:readonly
```

则寻找当前开放平台对应的：

> **群信息只读 / 获取群信息**

等价权限，并以后续 `doctor` 实际检查结果为准。

---

# 十一、第八阶段：配置 OAuth 回调地址

进入应用：

**安全设置**

找到：

**重定向 URL / Redirect URL**

增加：

```
http://127.0.0.1:8766/oauth/callback
```

必须完全一致。

特别注意：

- 是 `http`，不是 `https`；
- 是 `127.0.0.1`；
- 端口是 `8766`；
- 最后是 `/oauth/callback`。

飞书 OAuth 要求实际使用的 redirect URL 预先登记在应用安全设置中，详见[浏览器网页授权接入指南](https://open.feishu.cn/document/sso/web-application-end-user-consent/guide)。

---

# 十二、第九阶段：不要配置事件订阅

看到下面页面：

```
事件与回调
事件订阅
事件配置
```

**全部跳过。**

当前 Feishu Archive 不需要：

```
接收消息事件
机器人被加入群事件
消息已读事件
HTTP Webhook
长连接事件
```

也不要填写任何公网服务器地址。

当前项目的数据同步是**程序主动向飞书 OpenAPI 查询**，不是飞书把数据推送到 Mac。

---

# 十三、第十阶段：配置应用可用范围并发布

进入：

**应用发布 → 版本管理与发布**

创建版本。

至少保证：

> 你自己处在应用“可用范围”之内。

如果准备让其他同事也分别用自己的 OAuth 做本机归档，则将这些用户纳入可用范围。

然后：

**申请发布**

如果企业要求管理员审核，由管理员在飞书管理后台批准。

飞书开发者资料说明，新建应用默认可用范围可能仅包含创建者，并且应用正式版本及新增权限通常需要发布和相应审核。

---

# 十四、第十一阶段：把机器人加入需要归档的群

对于你希望尽可能完整归档的群：

打开群聊：

**群设置 → 机器人 → 添加机器人**

找到刚才建立的：

```
Feishu Archive
```

加入群。

为什么要做？

因为飞书部分“指定消息”和资源下载接口要求应用机器人位于相应会话中。

如果不加入：

- 某些群历史可能不完整；
- 图片可能取不到；
- 文件可能取不到；
- 部分资源 API 会返回权限错误。

但单聊的发现机制不同。

飞书的群列表 API 本身不会返回 P2P 单聊，因此当前程序还会使用：

```
search:message
```

去补发现当前用户可搜索的单聊。

---

# 十五、第十二阶段：建议再创建一个“邮箱专用应用”

当前项目支持两种方式。两种方式都会把邮箱 OAuth token 存在独立的 Keychain 命名空间，不会覆盖聊天/知识库 token。

### 方式A——推荐

创建：

```
Feishu Archive
```

负责聊天和知识库。

再创建：

```
Feishu Archive Mail
```

专门负责邮箱。

### 方式B——复用主应用

在主应用原有 10 项权限之外，再添加第十六阶段列出的 7 项 Mail 权限，创建并发布新版本，然后重新执行 `mail-auth`。不要删除原有聊天和知识库权限。

复用时不要执行 `mail-configure`；程序在没有单独 Mail App 凭据时会复用主应用，并为 `mail-auth` 请求主权限与 Mail 权限的并集。原来的 `auth` token 不会自动取得新加的 Mail 权限，因此 `mail-auth` 仍不可省略。

虽然方式 B 少一个应用，但方式 A 具有明显优点：

> 邮件权限可以单独授权、撤销和审计。

因此本文正文默认采用**两个应用**；无法创建第二个应用时，可以按方式 B 完成同等的邮箱读取路径。

---

# 十六、第十三阶段：配置 Feishu Archive Mail

新建企业自建应用。

例如：

```
Feishu Archive Mail
```

邮箱应用不需要事件订阅。

添加相同 OAuth 回调地址：

```
http://127.0.0.1:8766/oauth/callback
```

然后申请：

```
mail:user_mailbox:readonly
mail:user_mailbox.folder:read
mail:user_mailbox.message:readonly
mail:user_mailbox.message.subject:read
mail:user_mailbox.message.address:read
mail:user_mailbox.message.body:read
offline_access
```

这是当前程序 `MAIL_SCOPES` 的实际定义。

完成：

**创建版本 → 发布 → 管理员审批。**

---

# 十七、第十四阶段：将主应用凭据安全写入 Mac Keychain

返回 Terminal。

进入项目文件夹。

如果使用 Git 下载：

```
cd ~/FeishuLocalization
```

如果使用 ZIP，则进入对应解压目录。

---

## 17.1 保存 App ID

在飞书开放平台复制主应用的：

```
App ID
```

不要粘贴到 Terminal。

复制以后直接运行：

```
pbpaste | ./bin/feishu-archive configure --app-id-stdin
printf '' | pbcopy
```

确认命令成功后清空剪贴板：

```
printf '' | pbcopy
```

---

## 17.2 保存 App Secret

回到飞书后台复制：

```
App Secret
```

然后执行：

```
pbpaste | ./bin/feishu-archive configure --app-secret-stdin
printf '' | pbcopy
```

确认命令成功后立即清空剪贴板：

```
printf '' | pbcopy
```

这样做的好处是：

> App Secret 不会显示在 Terminal 命令历史里，也不会直接显示在屏幕上。

---

# 十八、第十五阶段：完成主应用 OAuth 授权

执行：

```
./bin/feishu-archive auth
```

程序会打开浏览器。

使用准备归档的飞书账号登录并授权。

授权成功以后回到 Terminal。

如果浏览器没有自动打开：

```
./bin/feishu-archive auth --no-open
```

Terminal 会输出一个授权地址。

把它复制进浏览器。

**不要关闭这个 Terminal 窗口。**

因为本机正在：

```
127.0.0.1:8766
```

等待 OAuth 回调。

当前代码会校验 OAuth `state`，并将 access token 和 refresh token 保存到 Keychain。

---

# 十九、第十六阶段：保存邮箱凭据并授权

## 19.1 使用独立邮箱应用（推荐）

在开放平台打开：

```
Feishu Archive Mail
```

复制 Mail App ID：

```
pbpaste | ./bin/feishu-archive mail-configure --app-id-stdin
printf '' | pbcopy
```

确认成功后执行 `printf '' | pbcopy` 清空剪贴板，再复制 Mail App Secret：

复制 Mail App Secret：

```
pbpaste | ./bin/feishu-archive mail-configure --app-secret-stdin
printf '' | pbcopy
```

确认成功后再次执行：

```
printf '' | pbcopy
```

然后：

```
./bin/feishu-archive mail-auth
```

完成浏览器授权。

如果浏览器没有自动打开：

```
./bin/feishu-archive mail-auth --no-open
```

邮箱 OAuth 使用独立 Keychain 命名空间，不会覆盖聊天/知识库的 token。

## 19.2 复用主应用

确认已经在主应用中追加全部 Mail 权限、发布新版本并完成审批。不要运行 `mail-configure`，直接执行：

```
./bin/feishu-archive mail-auth
```

如果浏览器没有自动打开：

```
./bin/feishu-archive mail-auth --no-open
```

授权页面应同时包含聊天/知识库和邮箱所需范围。授权完成后，用后文的 `mail-doctor` 验证实际取得的邮箱权限。

---

# 二十、第十七阶段：初始化正式数据库

执行：

```
./bin/feishu-archive init
```

正式数据默认存放到：

```
~/Library/Application Support/Feishu Archive
```

不要把这个目录放进：

```
Dropbox
OneDrive
GitHub
公开NAS同步目录
```

在发起可能持续很久的首次同步前，先执行：

```
./bin/feishu-archive doctor
```

当前版本只有在 App ID、主 OAuth 刷新令牌、全部聊天/知识库授权范围、SQLite、磁盘、FileVault 和阅读器绑定检查都通过时才返回成功。它核对的是 Keychain 中保存的授权范围，不能替代后续真实接口调用；如有 `!` 项，先按输出修复并重新授权，不要直接开始长时间同步。

---

# 二十一、第十八阶段：发现聊天

执行：

```
./bin/feishu-archive discover
```

该命令主要完成两件事：

1. 获取当前 OAuth 用户可见的群；
2. 通过消息搜索补发现有可见消息的 P2P 单聊。

执行结束后再同步。

---

# 二十二、第十九阶段：第一次完整聊天同步

执行：

```
./bin/feishu-archive sync --all-discovered
```

不要加 `--days`。

因为：

```
不加 --days
```

代表：

> 尽可能同步飞书接口允许取得的全部历史。

如果只想测试最近30天，才使用：

```
./bin/feishu-archive sync --all-discovered --days 30
```

正式第一次建库建议不限制日期。

首次全量同步可能持续数小时。期间保持 Mac 唤醒和网络稳定，不要同时启动第二个聊天同步。若因休眠、网络、接口限流或关机中断，可重新执行同一命令；数据库写入是幂等的，已保存消息和内容寻址资源会复用。重跑前先确认上一进程已经退出，不要删除 `.lock` 文件。

---

# 二十三、第二十阶段：补下载聊天附件

第一次完整聊天同步结束后执行：

```
./bin/feishu-archive attachments --workers 4
```

它会继续处理：

- 尚未下载的图片；
- 尚未下载的文件；
- 前一次因为短时网络问题失败的资源。

`workers` 范围是 1—8。

普通用户使用：

```
4
```

即可。

---

# 二十四、第二十一阶段：同步知识库

首先可以执行：

```
./bin/feishu-archive wiki-discover
```

然后：

```
./bin/feishu-archive wiki-sync
```

程序会处理当前 OAuth 用户有权限看到的知识空间。

当前完整正文支持：

- 新版飞书文档；
- 文档内容块；
- 内嵌图片；
- 文档附件；
- 普通文件节点。

当前以下类型主要保留目录信息：

- 旧版文档；
- 电子表格；
- 多维表格；
- 思维笔记；
- 幻灯片；
- 部分第三方组件。

因此：

> “全部现有功能”并不代表“飞书所有文件格式全部离线解析”。

这是飞书接口和当前适配范围共同决定的边界。

知识库同步也可以在中断后重跑同一命令；不要为了“续传”直接使用 `--force`，因为 `--force` 会要求重新处理全部可见文档。

---

# 二十五、第二十二阶段：第一次完整邮箱同步

执行：

```
./bin/feishu-archive mail-sync
```

不加 `--days` 的情况下，当前程序会按飞书 Mail OpenAPI 返回的文件夹进行全历史枚举。

包括：

- 收件箱；
- 已发送；
- 草稿；
- 定时发送；
- 垃圾箱；
- 垃圾邮件；
- 归档；
- 自定义文件夹。

邮箱使用独立：

```
mail.sqlite3
mail/blobs
mail-sync.lock
```

不会与聊天/知识库数据库混写。

邮箱首次同步同样可能很久。保持 Mac 唤醒，不要并行启动第二个邮箱同步；中断后直接重跑 `mail-sync`，程序会复用已经校验的内容寻址 blob。

---

# 二十六、特别注意邮箱磁盘空间保护

当前代码为了防止完整邮箱附件把磁盘写满，设置了保护机制。

默认：

```
邮箱内容和附件总量：10 GiB
单个邮箱附件：1 GiB
最大分页预算：5000页
```

并且：

### 剩余磁盘低于约100 GiB，或者磁盘使用率达到95%

程序会：

> 暂停新附件下载，但继续尽量保存邮件正文和元数据。

### 剩余磁盘低于约75 GiB，或者磁盘使用率达到97%

程序会：

> 停止邮箱同步。

不要通过修改数据库、删除锁文件等方法绕过这个保护。

---

# 二十七、第二十三阶段：运行健康检查

执行：

```
./bin/feishu-archive doctor
```

然后：

```
./bin/feishu-archive mail-status
```

```
./bin/feishu-archive mail-doctor
```

重点确认：

- 飞书 OAuth 有效；
- 必要权限存在；
- SQLite 正常；
- 磁盘空间正常；
- FileVault 状态正常；
- 邮箱授权正常。

这里的 `doctor` 是同步完成后的再次验收；它现在会把缺 App 配置、缺主令牌或缺任一主 OAuth 权限都视为失败。`mail-doctor` 负责独立邮箱数据库、权限、blob 完整性和本机安全边界，两者不能互相替代。

---

# 二十八、第二十四阶段：启动本地阅读器

测试：

```
./bin/feishu-archive serve
```

该命令会一直占用当前 Terminal，这是正常现象。保持它运行，并新开第二个 Terminal 窗口执行邮箱解锁等命令。完成前台验收后，回到运行 `serve` 的窗口按 `Control + C` 停止；否则 8765 端口仍被占用，后台阅读器无法启动。

浏览器打开：

```
http://127.0.0.1:8765
```

可以查看：

- 聊天；
- 消息；
- 图片；
- 文件；
- 知识库；
- 邮箱；
- 搜索结果；
- 后续每日洞察。

阅读器设计上只允许本机 loopback 地址，不应改成：

```
0.0.0.0
```

也不要把 8765 做：

- 路由器端口映射；
- Cloudflare Tunnel；
- ngrok；
- 公网反向代理。

---

## 28.1 验证聊天搜索和单会话导出

在阅读器中进入“消息”，选择一个已同步的会话：

1. 用一个已知关键词验证全文搜索；
2. 点击“导出 JSON”，确认浏览器成功下载 JSON 文件；
3. 点击“导出 HTML”，断开网络后打开该文件，确认会话文字仍可阅读。

当前 HTML 把样式和消息文字包含在单一文件中，但**不会把聊天图片或文件附件嵌入 HTML**；JSON 会包含会话、消息和资源记录，便于后续程序处理，但本地资源文件仍是独立文件。两者都是脱离 Keychain 和阅读器保护的**明文副本**，不要放入公共网盘、GitHub 或无访问控制的共享目录。

## 28.2 验证日常检索与手工同步

继续在阅读器中完成一次实际操作验收：

1. 在“消息”中分别使用会话、关键词、发送人、消息类型和起止日期筛选；点击“立即同步”后确认状态更新；
2. 在“知识库”中选择空间、展开目录、打开文档，并用已知关键词搜索；点击知识库同步按钮后确认没有重复异常；
3. 打开一张图片和一个由其他人或机器人发送的普通文件；本人发送的普通文件当前只保留消息和资源元数据，不把文件本体作为验收要求；
4. 断开外网后刷新一个已经同步的聊天和知识库文档，确认正文与本地资源仍可读取。

---

# 二十九、第二十五阶段：使用邮箱阅读器

邮件属于更敏感的数据。

因此默认情况下直接访问邮箱 API 可能看到：

```
401
```

这是正常的安全设计。

执行：

```
./bin/feishu-archive mail-reader-url --open
```

程序会生成一个安全的本机解锁链接，并打开浏览器。

短期会话大约15分钟，解锁密钥通过 URL fragment 传递，不作为普通 HTTP 查询参数进入服务日志。

解锁后在“邮箱”中完成以下日常验收：选择收件箱、已发送和一个自定义文件夹；用已知主题、发件人或正文关键词搜索；翻页并打开一封邮件；下载一个普通附件；对 HTML、SVG、脚本、XML 或可执行格式只在确认来源和风险后进行二次确认。阅读器中的“同步邮箱”会执行一次全历史手工枚举，不等同于每天 04:00 的 2 天重叠增量任务。

---

## 29.1 如果这是个人专用且开启 FileVault 的 Mac

可以选择永久本机解锁：

```
./bin/feishu-archive mail-reader-url --permanent --open
```

以后重新启动服务也可以阅读邮箱。

但是：

> 本机其他有能力访问 loopback 端口的程序、浏览器扩展或本机用户，也可能读取邮件。

因此共享 Mac 不推荐永久解锁。

恢复锁定：

```
./bin/feishu-archive mail-reader-url --lock
```

---

# 三十、第二十六阶段：安装自动后台服务

到目前为止，所有功能还是“手工执行”。先回到运行 `serve` 的 Terminal，按 `Control + C` 释放 8765 端口。

验证没有问题后执行：

```
deactivate 2>/dev/null || true
```

然后：

```
command -v python3
```

确认显示的是稳定存在的 Python。

最后：

```
./scripts/install-local.sh --without-insights
```

**不要使用：**

```
sudo ./scripts/install-local.sh --without-insights
```

项目使用的是当前用户自己的 LaunchAgent、Keychain 和档案目录，不需要 root 权限。

---

# 三十一、安装以后系统会自动做什么

这些任务是当前 macOS 登录用户的 LaunchAgent，不是系统级守护进程。Mac 需要处于开机状态并保留该用户会话；表中的计划时间按 Mac 当前系统时区解释，而日报的自然日边界按 `DEFAULT_INSIGHTS_TIMEZONE` 解释。电脑关机、休眠或用户会话不可用时，不应把表中的固定时间理解为有服务器保证的准点执行。

使用 `--without-insights` 时建立以下核心后台服务：

| 时间/条件 | 工作 |
| --- | --- |
| 登录后 | 启动本地阅读器 |
| 03:30 | 聊天增量同步 |
| 03:45 | 知识库增量同步 |
| 04:00 | 邮箱增量同步 |
| 每 300 秒 | 详细会议记录增量同步 |

聊天和邮箱的日常增量默认会采用约2天重叠窗口，用来捕获延迟出现、编辑或状态发生变化的内容。

安装脚本还会：

- 将稳定运行代码复制到档案目录的 `runtime`；
- 把日志统一放入 `logs`；
- 在更新失败时恢复上一版 runtime；
- 只有新版阅读器通过健康检查以后才删除回滚副本。

只有在第三十三至四十阶段完成数据边界确认、专用 SSH 配置、干跑和一次真实日报验收后，才执行 `./scripts/install-local.sh --with-insights`。届时额外增加：04:30 每日洞察、05:00 和 05:30 未完成重试，以及周期性历史回填。未明确同意把本地归档正文提交给指定模型服务时，不要启用此模式。

安装器不带选项时，新安装默认只安装核心服务；已有安装会根据现存 Insights plist 保留原模式。为保证作业记录清晰，本文始终要求明确写 `--without-insights` 或 `--with-insights`。

### 关于历史洞察回填的唤醒频率

当前实际源代码：

```
DEFAULT_INSIGHTS_BACKFILL_INTERVAL_SECONDS = 60
```

该值作为 KeepAlive 进程异常退出后的 `ThrottleInterval`，不是正常工作的唤醒周期。

所以启用 Insights 后的实际行为应按：

> **回填进程常驻；异常退出后至少等待 60 秒再由 launchd 重启**

理解。

常驻进程仍会检查模型是否空闲；繁忙时按程序内置间隔轮询，不会每 60 秒固定提交正文。

---

# 三十二、第二十七阶段：验证后台服务

执行：

```
curl --fail http://127.0.0.1:8765/api/status
```

再执行：

```
curl --fail http://127.0.0.1:8765/api/wiki/status
```

检查主服务：

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive"
```

检查聊天同步：

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-sync"
```

检查知识库同步：

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-wiki-sync"
```

检查邮箱同步：

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-mail-sync"
```

当前按 `--without-insights` 安装，下面两个命令应提示服务不存在；这证明归档正文不会在尚未确认模型边界时自动进入 AI 流程：

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights"
```

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights-backfill"
```

再执行：

```
./bin/feishu-archive mail-status
```

```
./bin/feishu-archive mail-doctor
```

只要阅读器正常、授权正常、同步状态正常，就完成了飞书核心归档部分。

---

# 三十三、第二十八阶段：配置每日 AI 洞察

这一部分是“全部功能”里唯一不能只靠飞书开放平台完成的部分。

当前系统默认每日洞察会从：

> 聊天 + 知识库 + 邮箱

抽取证据，形成：

### Yesterday Summary

昨日发生了什么。

### Today Plan

今天应该处理什么。

### Commercial Opportunities

哪些内容可能形成商业机会。

模型输出不是直接写回飞书，而是进入独立：

```
insights.sqlite3
```

因此 AI 不能：

- 自动向别人发飞书；
- 自动修改邮件；
- 自动修改知识库；
- 自动创建飞书任务。

这是一个**只读分析层**。

---

# 三十四、当前默认 vMLX 参数

当前源码默认：

```
时区：Europe/Amsterdam
模型服务器：192.168.100.179
SSH用户：apple
模型：vmlx/gemma-4-31b-it-8bit
本机隧道端口：18135
远端模型端口：11435
SSH专用私钥：默认未指定
```

这些参数来自当前 `config.py`。

这意味着：

> 把项目下载到任意一台新的 Mac 后，飞书功能可以按照本指导配置；但是如果那台机器没有访问 `192.168.100.179` 的条件，AI Insights 不会凭空工作。

---

# 三十五、普通部署用户应向模型管理员取得六项信息

如果单位已经有 vMLX 模型服务器，请模型管理员提供：

```
1. MODEL_HOST
2. SSH_USER
3. MODEL_ID
4. MODEL_PORT
5. SSH专用私钥在本机的绝对路径
6. SSH访问权限和主机指纹
```

推荐使用当前代码的：

```
MODEL_PORT = 11435
```

模型服务器需要在其自身：

```
127.0.0.1:11435
```

提供兼容服务。

当前客户端实际使用：

```
/v1/models
/v1/chat/completions
/health
```

也就是说，仅仅“安装一个 Ollama”不一定自动等于兼容，必须满足当前 Feishu Archive 期望的模型 API 和健康检查结构。

---

# 三十六、为什么模型连接采用 SSH Tunnel

项目不会直接把飞书邮件、聊天正文发到：

```
http://192.168.x.x:11435
```

而是建立：

```
Mac
127.0.0.1:18135
       │
       │ SSH加密隧道
       ▼
模型服务器
127.0.0.1:11435
```

所以模型服务器本身仍可以只监听：

```
127.0.0.1
```

不必把大模型 API 暴露给整个局域网。

当前 SSH 客户端还强制：

- BatchMode；
- StrictHostKeyChecking；
- 不使用密码认证；
- 不依赖 SSH Agent；
- 不读取用户 `~/.ssh/config`；
- 使用公钥认证。

---

# 三十七、模型服务器 SSH 应由管理员预配置

为了让凌晨04:30的自动任务在没人操作 Mac 时也能运行，SSH 必须做到：

> **无需人工输入密码即可完成公钥认证。**

最稳妥的企业部署方式是由 IT/模型服务器管理员提前完成：

1. 为 Feishu Archive Mac 准备专用 SSH Key；
2. 将公钥加入模型主机；
3. 限制该账号权限；
4. 将服务器 Host Key 指纹提供给用户核验；
5. 在 Mac `known_hosts` 中建立可信记录；
6. 确认模型 API 只监听远端 loopback；
7. 不开放公网模型端口。

专用私钥可以放在例如 `$HOME/.ssh/id_ed25519_feishu_archive`，权限应为 `0600`。当前隧道使用 `-F /dev/null` 和 `IdentitiesOnly=yes`，所以自定义名称的私钥不会从 `~/.ssh/config` 或 SSH agent 自动取得，必须在手工命令中用 `--identity-file` 明确指定，并为无人值守任务配置默认路径。

不建议普通用户通过“关闭 StrictHostKeyChecking”等方法绕过安全检查，因为当前源代码本身就是故意禁止这种降级。

---

# 三十八、如果模型服务器和当前源码默认值不同

不要把“修改 `config.py`”作为普通用户验证模型的第一步。先使用命令行参数验证，确认时区、主机、用户、专用私钥、模型和端口全部正确后，再决定是否配置无人值守任务。

## 38.1 手工验证：不修改源码

下面仅为示例；必须把地址、SSH 用户和模型名替换为模型管理员提供的真实值：

```
./bin/feishu-archive insights-run \
  --timezone Asia/Shanghai \
  --host 192.168.1.50 \
  --user modeluser \
  --identity-file "$HOME/.ssh/id_ed25519_feishu_archive" \
  --model vmlx/qwen3-32b-8bit \
  --local-port 18135 \
  --remote-port 11435
```

`--identity-file` 展开后必须是现有的绝对文件路径；相对路径和不存在的文件会被拒绝。这些参数只影响本次运行，不修改代码，也不会自动改变凌晨任务。正式调用模型前，先完成第三十九章的 `--no-model --dry-run` 验证。

## 38.2 无人值守任务：管理员确认后再改默认值

本文适用源码基线的 `install-local.sh` 生成的 Insights 和历史回填任务不附加模型参数，运行时会读取所安装 runtime 内 `config.py` 的默认值；当前没有单独的用户配置文件。因此，如果实际模型与源码默认值不同，无人值守部署需要由熟悉 Git 和模型服务的管理员完成以下受控操作。

先备份：

```
cp src/feishu_archive/config.py "$HOME/Desktop/feishu-archive-config.py.backup"
```

再打开：

```
open -a TextEdit src/feishu_archive/config.py
```

只修改下面这些默认值，不要把密码、SSH 私钥、Bearer token 或飞书凭据写入该文件：

```
DEFAULT_INSIGHTS_TIMEZONE = "Europe/Amsterdam"
DEFAULT_VMLX_HOST = "192.168.100.179"
DEFAULT_VMLX_USER = "apple"
DEFAULT_VMLX_MODEL = "vmlx/gemma-4-31b-it-8bit"
DEFAULT_VMLX_IDENTITY_FILE = None
DEFAULT_VMLX_LOCAL_PORT = 18135
DEFAULT_VMLX_REMOTE_PORT = 11435
```

如果使用自定义名称的专用私钥，把 `DEFAULT_VMLX_IDENTITY_FILE` 改为该 Mac 上的绝对路径，例如 `"/Users/yourname/.ssh/id_ed25519_feishu_archive"`。这里可以保存**路径字符串**，绝不能把私钥内容粘进源码。

保存后先做语法检查：

```
python3 -m py_compile src/feishu_archive/config.py
```

Git 用户还应检查实际差异：

```
git diff -- src/feishu_archive/config.py
```

然后依次运行第三十九章的数据 dry-run、第四十章的真实模型测试。只有全部通过并确认允许模型处理本地档案正文后，才按第四十章执行 `./scripts/install-local.sh --with-insights`。

修改源码会使 `git pull --ff-only` 可能因本地差异而失败，也可能在以后换版本时需要重新应用。升级前必须记录和复核这些参数；更稳妥的长期做法是由项目维护者提供正式的外部配置能力或维护经过审核的部署分支。

## 38.3 可选的 8067 Bearer 代理路线

默认受控路线为 SSH 隧道直连远端 loopback 的 `11435`。如果模型管理员明确提供了 8067 代理和独立 Bearer token，先复制 token，然后通过标准输入保存到 macOS Keychain：

```
pbpaste | ./bin/feishu-archive insights-configure --bearer-token-stdin
```

确认成功后立即执行 `printf '' | pbcopy` 清空剪贴板。

管理员通过环境变量注入 token 时，也可以使用：

```
printf '%s' "$VMLINUX_BEARER_TOKEN" | ./bin/feishu-archive insights-configure --bearer-token-stdin
```

手工测试指定日期：

```
./bin/feishu-archive insights-run \
  --date 2026-08-12 \
  --timezone Asia/Shanghai \
  --host 192.168.1.50 \
  --user modeluser \
  --identity-file "$HOME/.ssh/id_ed25519_feishu_archive" \
  --model vmlx/qwen3-32b-8bit \
  --local-port 18135 \
  --remote-port 8067
```

不要把 Bearer token 直接写在命令参数、`config.py`、README 或截图中。本文适用源码基线的 `insights-backfill-step` 只接受 `11435`，因此 8067 是手工日报的受支持替代路线，不是完整历史回填路线。

---

# 三十九、先测试数据抽取，不调用模型

如果使用源码默认时区，执行：

```
./bin/feishu-archive insights-run --no-model --dry-run
```

如果实际日报时区不同，应在测试中明确指定，例如：

```
./bin/feishu-archive insights-run --timezone Asia/Shanghai --no-model --dry-run
```

这个命令会验证：

- 聊天数据能否读取；
- 知识库数据能否读取；
- 邮箱数据能否读取；
- 日期窗口是否正确；
- 数据是否能够组成每日分析源。

但是：

> 不调用模型，也不把测试结果写入正式 Insights 数据库。

这是最安全的第一步。

---

# 四十、测试真实 Insights

如果已经按 38.2 节把经过审核的值写成默认值，执行：

```
./bin/feishu-archive insights-run
```

如果只做了 38.1 节的手工验证，必须重复使用**同一整组参数**，不要改回无参数命令，否则会悄悄恢复源码默认主机、用户、模型、端口和身份文件：

```
./bin/feishu-archive insights-run \
  --timezone Asia/Shanghai \
  --host 192.168.1.50 \
  --user modeluser \
  --identity-file "$HOME/.ssh/id_ed25519_feishu_archive" \
  --model vmlx/qwen3-32b-8bit \
  --local-port 18135 \
  --remote-port 11435
```

然后：

```
./bin/feishu-archive insights-status
```

Insights API 与邮箱共用本机阅读会话。先执行：

```
./bin/feishu-archive mail-reader-url --open
```

再打开：

```
http://127.0.0.1:8765/?mode=insights
```

检查是否出现：

- Yesterday Summary；
- Today Plan；
- Commercial Opportunities；
- 证据来源；
- 置信度；
- 当前模型和生成状态。

在页面中切换到刚才生成的日期，再切换到一个没有报告的日期，确认日期选择和空状态都正确。只有日报内容、证据引用、置信度和模型身份都通过人工抽样，并且数据所有者明确同意指定模型服务处理聊天、知识库和邮件正文后，才启用无人值守任务。若手工参数与源码默认值不同，还必须先完成 38.2 节的默认值配置和语法检查，否则后台任务仍会使用错误的旧默认值：

```
./scripts/install-local.sh --with-insights
```

启用后验证：

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights"
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-insights-backfill"
```

`--with-insights` 会立即启动历史回填并按计划运行日报；它不是单纯“保存设置”。如尚未取得数据处理授权，只保留 `--without-insights`，手工测试结束后不要启用自动任务。

---

# 四十一、为什么模型失败不会破坏档案

当前 Insights 采用：

> 原始档案 → 只读数据抽取 → 模型分析 → 独立 Insights DB

模型接收的数据还被明确标记为：

```
UNTRUSTED_DATA
```

模型看不到工具权限，也没有写飞书的能力。

程序不会向模型发送：

- OAuth token；
- App Secret；
- raw JSON；
- BCC；
- 原始 MIME；
- 附件二进制。

附件目前仅进入类似：

- 文件名；
- 类型；
- 大小；
- Hash；
- 归档状态

等元数据。

这使模型分析层和原始档案层保持分离。

---

# 四十二、历史洞察回填

当每日洞察正常以后，系统还会从当前本地档案中观察到的**最早日期**开始：

```
最早日期
↓
第二天
↓
第三天
↓
……
↓
昨天
```

逐日回放和分析历史。

它不是只分析最近30天。

如果某一天失败：

> 不会自动跨过去造成一个看不见的历史空洞。

它会保存：

```
backfill-state.json
```

以及分片 checkpoint。

下一轮从未完成位置继续。

查看：

```
./bin/feishu-archive insights-status
```

也可以人工触发一步：

```
./bin/feishu-archive insights-backfill-step
```

---

# 四十三、历史回填不会无限抢占模型

虽然当前调度器约每60秒唤醒一次，但真正提交分析前会检查模型引擎。

当前机制要求包括：

- 模型 ID 匹配；
- 当前没有正在运行的任务；
- 等待队列为空；
- 模型已经空闲达到门槛；
- Map/Reduce 请求前再次检查。

因此：

> 60秒是“检查是否适合工作”，不是“每60秒强行跑一个大模型请求”。

---

# 四十四、最终完整验收

全部配置以后，按下面清单逐项验收。

## A. 基础环境

- [ ] Mac 使用 Python 3.11+
- [ ] `./bin/feishu-archive --version` 正常
- [ ] FileVault 已开启
- [ ] 8765 没有暴露公网
- [ ] 8766 仅用于本地 OAuth

## B. 飞书主应用

- [ ] 企业自建应用已创建
- [ ] 机器人能力已启用
- [ ] 10项主权限已配置
- [ ] OAuth redirect URL 已配置
- [ ] 应用已发布
- [ ] 管理员审批已完成
- [ ] 当前 OAuth 用户在可用范围
- [ ] 需要归档的重点群已添加机器人
- [ ] 没有配置不必要的事件订阅

## C. 邮箱应用

- [ ] 已建立邮箱独立应用，或已在主应用中追加全部 Mail 权限
- [ ] 邮箱只读权限已添加
- [ ] OAuth redirect URL 已添加
- [ ] 应用已经发布
- [ ] `mail-auth` 成功

## D. 聊天

- [ ] `discover` 成功
- [ ] 至少能看到一个已知群
- [ ] 至少能看到一个已知单聊
- [ ] 历史消息能够读取
- [ ] Thread 回复能够读取
- [ ] 图片能够显示
- [ ] 至少一个由其他人或机器人发送的普通文件能够归档；本人发送的普通文件只核对消息和资源元数据
- [ ] 关键词全文搜索有效
- [ ] 单会话 JSON 能够导出
- [ ] 单会话 HTML 能够离线打开

## E. 知识库

- [ ] 知识空间能够列出
- [ ] 目录树正确
- [ ] 新版文档正文能够读取
- [ ] 内嵌图片能够显示
- [ ] 普通文件能够访问
- [ ] 知识库搜索有效

## F. 邮箱

- [ ] 收件箱可见
- [ ] 已发送可见
- [ ] 自定义文件夹可见
- [ ] 邮件正文可见
- [ ] 普通附件能够下载
- [ ] 风险附件出现二次确认
- [ ] `mail-doctor` 正常

## G. 自动任务

- [ ] 阅读器登录后自动运行
- [ ] 聊天同步 LaunchAgent 存在
- [ ] Wiki 同步 LaunchAgent 存在
- [ ] Mail 同步 LaunchAgent 存在
- [ ] Meeting Records 同步 LaunchAgent 存在，周期为 300 秒
- [ ] 若只部署核心功能，Insights 和 Backfill LaunchAgent 不存在
- [ ] 若明确启用全部功能，Insights 和 Backfill LaunchAgent 均存在

## H. Insights

- [ ] `insights-run --no-model --dry-run` 正常
- [ ] SSH 模型连接正常
- [ ] 专用私钥通过 `--identity-file` 或审核后的 `DEFAULT_VMLX_IDENTITY_FILE` 明确指定
- [ ] 实际模型 ID 与配置一致
- [ ] `insights-run` 成功
- [ ] `insights-status` 正常
- [ ] `meeting-records-status` 正常，会议修订后已有日报只显示“待人工刷新”
- [ ] 已用 `mail-reader-url --open` 建立本机阅读会话
- [ ] `/?mode=insights` 页面能够打开
- [ ] Yesterday Summary 有内容
- [ ] Today Plan 有内容
- [ ] Commercial Opportunities 有内容
- [ ] 历史回填已经启动

## I. 覆盖边界记录

- [ ] 已记录租户的数据保留期限和应用可用范围
- [ ] 已抽样比对至少一个已知群、单聊、知识库和邮箱文件夹
- [ ] 已记录机器人未加入、撤回/删除、100 MB、DLP 和未适配文档类型造成的缺口
- [ ] 已记录本人发送的普通文件当前不保存文件本体
- [ ] 没有把“同步成功”表述为飞书客户端的完整备份或电子取证镜像

只有 A—I 全部满足，才可以称为：

> **“FeishuLocalization 当前版本全部现有功能已经完成部署。”**

---

# 四十五、常见故障及处理

| 现象 | 最常见原因 | 处理 |
| --- | --- | --- |
| `0个会话 · 0条消息` | 还没有真实同步 | 检查 `auth` → `discover` → `sync --all-discovered` |
| OAuth 打不开 | 浏览器没有自动启动 | 使用 `auth --no-open` |
| OAuth 回调失败 | 8766被占用或URL不一致 | 检查 `http://127.0.0.1:8766/oauth/callback` |
| 新增权限仍报缺权限 | 只勾选了权限，没有重新发布 | 创建新版本→发布→重新 `auth` |
| `doctor` 返回失败 | App 配置、主令牌、任一主权限或本机安全检查未通过 | 按每个 `!` 项修复；发布权限后重新 `auth`，不要把非零退出当作警告忽略 |
| `mail-auth` 后缺权限 | 邮箱权限未发布 | 发布邮箱应用新版本→重新 `mail-auth` |
| 复用主应用后邮箱缺权限 | 只保留了主权限，或新增 Mail 权限后没有发布/重新授权 | 追加全部 Mail 权限→发布→重新 `mail-auth`；不要运行 `mail-configure` |
| 自动邮箱任务跳过 | 没有有效 Mail OAuth | 运行 `mail-auth`、`mail-doctor` |
| 群很少 | 机器人没有加入相关群 | 将应用机器人加入目标群 |
| 单聊比客户端少 | 飞书搜索/保留期限制 | 确认 `search:message` 权限 |
| 图片或附件缺少 | Bot不在群/超过100MB/DLP限制 | 加机器人，执行 `attachments --workers 4` |
| 本人发送的普通文件没有本体 | 当前有意跳过本人普通文件下载 | 消息和资源元数据仍应存在；这属于已知覆盖边界，不要反复强制同步 |
| 文档只有标题没有正文 | 内容类型当前未适配 | 新版Docx支持；Sheet/Base等可能只有目录 |
| Wiki页面显示旧格式 | 本地正文渲染版本旧 | 执行 `wiki-rebuild` |
| 邮件正文有、附件没有 | 磁盘或附件上限触发 | 检查可用磁盘和 `mail-doctor` |
| 邮箱API 401 | 阅读器没有解锁 | `mail-reader-url --open` |
| HTML/SVG/脚本附件不能直接开 | 被安全隔离 | 在阅读器确认风险后二次下载 |
| `Address already in use` | 手工 `serve` 仍在运行，或8765/8766被其他进程占用 | 回到前台 Terminal 按 `Control + C`；确认旧实例退出后再安装或授权 |
| TLS证书错误 | Python证书环境异常 | 使用 Python.org 正式版本并安装证书 |
| SQLite locked | 多进程或复制中写入 | 停止服务后再操作 |
| Insights只有统计没有智能结论 | 模型不可用 | 检查 SSH、模型、11435 |
| Insights 页面或 API 401 | 尚未建立邮箱/Insights 共用的本机阅读会话 | 执行 `mail-reader-url --open` 后从新打开的页面进入 Insights |
| SSH 提示找不到身份或公钥拒绝 | 专用私钥名称非默认，且 SSH 配置/agent 被安全策略忽略 | 手工命令加 `--identity-file`；后台任务配置 `DEFAULT_VMLX_IDENTITY_FILE` 后用 `--with-insights` 重装 |
| Insights模型不匹配 | 配置的 MODEL ID 与服务器不同 | 先用 `--model` 手工验证；计划任务参数由管理员按 38.2 节受控更新 |
| Insights凌晨不工作，手工指定参数却可以 | LaunchAgent仍用源码默认参数 | 核对 38.2 节默认值、语法检查和差异，再重新执行安装脚本 |
| 8067 提示缺 Bearer | 尚未把代理 token 保存到 Keychain | 按 38.3 节用标准输入执行 `insights-configure` |
| `git pull --ff-only` 因 `config.py` 停止 | 本地模型默认值修改形成未提交差异 | 不要强制覆盖；先保存差异并由管理员决定迁移或维护部署分支 |
| Linux报 `/usr/bin/security` | 当前真实OAuth不支持Linux | 改用macOS完整部署 |
| Windows报 `fcntl` | Windows原生尚未支持 | 不能作为当前完整部署平台 |

这些故障类型与当前仓库 README 中列出的运行边界基本一致。

---

# 四十六、查看日志

服务日志：

```
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/service.error.log"
```

聊天同步：

```
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/sync.error.log"
```

知识库：

```
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/wiki-sync.error.log"
```

邮箱：

```
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/mail-sync.error.log"
```

历史洞察：

```
tail -f "$HOME/Library/Application Support/Feishu Archive/logs/insights-backfill.error.log"
```

普通用户排障时，建议：

> 一次只看一个日志。

不要一次打开多个 Terminal 后同时运行同步命令。

---

# 四十七、不要手工删除 `.lock` 文件

项目分别使用：

```
sync.lock
wiki-sync.lock
mail-sync.lock
insights.lock
```

这些锁是为了避免两个同步进程同时修改 SQLite。

如果看到“busy”“locked”等提示：

> 首先查是否已有同步正在运行。

不要把：

```
rm *.lock
```

作为普通故障处理办法。

---

# 四十八、数据备份

真正重要的数据不是 GitHub 代码，而是：

```
~/Library/Application Support/Feishu Archive
```

如果要备份，应复制**整个目录**。

不要只复制：

```
archive.sqlite3
```

因为还有：

```
archive.sqlite3-wal
attachments
knowledge
mail.sqlite3
mail.sqlite3-wal
mail/blobs
insights.sqlite3
insights
exports
```

在复制之前应先停止相关服务，避免 SQLite 正在写入。对于已经按本文安装后台任务的 Mac，推荐使用以下可恢复流程：

1. 先记录当前模式是核心归档（`--without-insights`）还是已获授权的完整 Insights（`--with-insights`）；
2. 执行 `./scripts/uninstall-local.sh`，卸载当前用户的 LaunchAgent；这会暂停服务，但不会删除档案；
3. 确认没有手工 `sync`、`wiki-sync`、`mail-sync`、`insights-run` 或 `serve` 进程，再用 Finder 将整个 `Feishu Archive` 目录复制到受访问控制且容量足够的备份位置；
4. 确认备份中同时存在数据库、附件/资源、邮件 blob、洞察目录和可能存在的 `-wal` 文件；
5. 回到经过验证的项目源码目录，按第1步记录显式执行 `./scripts/install-local.sh --without-insights` 或 `./scripts/install-local.sh --with-insights`；
6. 访问 `http://127.0.0.1:8765/api/status`，并运行 `doctor`、`mail-doctor` 确认服务和数据完整性恢复。

备份期间同步和阅读器会暂停。不要一边复制数据库，一边手工运行 `sync`、`wiki-sync` 或 `mail-sync`。

Mac Keychain 中的 App Secret 和 OAuth Token 不会跟着这个目录一起迁移。

换新 Mac 或恢复到不同用户名/目录时，按以下顺序处理：

1. 在目标 Mac 把整个目录恢复为 `~/Library/Application Support/Feishu Archive`，执行 `chmod 700 "$HOME/Library/Application Support/Feishu Archive"`，并确认 FileVault 已开启；
2. 用第十七、十八章的完整命令重新执行 `configure --app-id-stdin`、`configure --app-secret-stdin` 和 `auth`；Keychain 凭据不会随备份复制；
3. 独立邮箱应用还要重新执行两个 `mail-configure` 命令和 `mail-auth`；复用主应用时只重新执行 `mail-auth`；
4. 重新配置 SSH 专用私钥、`known_hosts` 和可选 Insights Bearer token；这些凭据不在档案目录内；
5. 执行 `wiki-rebuild --force`。0.5.4 新数据使用相对路径，0.5.3 及更早的知识库绝对路径会按保留的 `knowledge/assets` 结构自动重映射并重新生成相对链接 HTML；
6. 执行 `doctor`、`mail-doctor`，再抽查一个聊天附件、一张知识库图片、一个知识库文件、一个邮件附件和一个导出 HTML；
7. 备份可能含 `reader.secret` 和永久解锁策略。目标 Mac 上先执行 `mail-reader-url --lock` 撤销旧会话，再按需要重新执行 `mail-reader-url --open`；不要默认沿用永久解锁。

知识库兼容映射依赖完整保留 `knowledge/assets` 子目录结构。只复制数据库、单独改名资源目录或只复制导出 HTML，都不能视为完整恢复。

---

# 四十九、升级 GitHub 版本

升级前先阅读新版本 README 和变更记录，并重新核对本文顶部列出的版本敏感项。不要在未经备份和验证时直接覆盖正在使用的源码目录。

## 49.1 Git 用户

如果采用 Git：

```
cd ~/FeishuLocalization
```

```
git pull --ff-only
```

```
./bin/feishu-archive --version
```

然后按升级前记录的模式显式重新安装；未获 Insights 数据处理授权时必须选择核心模式：

```
./scripts/install-local.sh --without-insights
# 或：./scripts/install-local.sh --with-insights
```

安装器会先测试候选 runtime。

新版启动失败时，当前脚本具有恢复上一版 runtime 和 LaunchAgent 的设计。

### 特别注意

如果为了自己的 vMLX 地址修改过：

```
src/feishu_archive/config.py
```

升级前请记录：

```
TIMEZONE
HOST
USER
MODEL
IDENTITY_FILE
PORT
```

否则 Git 更新可能产生冲突或覆盖本地修改。

不要用强制重置或删除本地修改来绕过冲突；先保存 `git status` 和 `git diff` 的结果，再由维护者决定如何迁移。

## 49.2 ZIP 用户

1. 从 GitHub 下载新 ZIP，并解压到一个**新的文件夹**；不要覆盖旧的 `FeishuLocalization-main`；
2. 在新文件夹运行 `./bin/feishu-archive --version`，核对版本；
3. 重新检查飞书权限、默认模型参数和本文列出的 OpenAPI 边界；
4. 在新文件夹按原模式执行 `./scripts/install-local.sh --without-insights` 或 `./scripts/install-local.sh --with-insights`；安装器会验证候选 runtime；
5. 检查阅读器、聊天、Wiki、Mail 和 Insights 状态后，再决定是否保留旧源码文件夹。

升级不会自动替代数据备份。旧源码文件夹至少应保留到新 runtime 健康检查和一次代表性同步通过为止。

---

# 五十、停止后台程序但保留数据

执行：

```
./scripts/uninstall-local.sh
```

它用于移除后台服务。

不是删除档案。

档案仍保存在：

```
~/Library/Application Support/Feishu Archive
```

除非明确确认不再需要，否则不要手工删除这个目录。

---

# 五十一、安全操作红线

普通用户应始终遵守以下规则：

**第一，不把 App Secret 发给任何聊天机器人。**

包括部署排障时，也不要把真实 Secret 粘贴给 AI。

**第二，不把 OAuth token 发给别人。**

**第三，不把 Feishu Archive 数据目录上传 GitHub。**

**第四，不把阅读器监听地址改成**`0.0.0.0`**。**

**第五，不做公网端口转发。**

**第六，建议开启 FileVault。**

**第七，不要关闭邮件危险附件隔离。**

**第八，不要为了让 SSH 成功而关闭 StrictHostKeyChecking。**

**第九，不要通过删除数据库状态或锁文件绕过容量和并发保护。**

**第十，导出的 HTML、JSON 或邮件附件都是明文文件，离开 Feishu Archive 后需要自行控制传播。**

当前项目本身也明确将本机 loopback、Keychain、邮件隔离、危险附件确认和磁盘加密作为安全边界。

---

# 附录A：普通用户最简命令卡

假定已经进入：

```
FeishuLocalization
```

目录。

## 1. 测试

```
./bin/feishu-archive --version
```

## 2. 主应用凭据

```
pbpaste | ./bin/feishu-archive configure --app-id-stdin
```

```
pbpaste | ./bin/feishu-archive configure --app-secret-stdin
```

## 3. 主应用授权

```
./bin/feishu-archive auth
```

## 4. 邮箱应用凭据

仅使用独立邮箱应用时执行：

```
pbpaste | ./bin/feishu-archive mail-configure --app-id-stdin
```

```
pbpaste | ./bin/feishu-archive mail-configure --app-secret-stdin
```

## 5. 邮箱授权

```
./bin/feishu-archive mail-auth
```

复用主应用时跳过第 4 项，但仍必须执行本项。

## 6. 初始化

```
./bin/feishu-archive init
```

## 7. 发现聊天

先执行同步前预检：

```
./bin/feishu-archive doctor
```

```
./bin/feishu-archive discover
```

## 8. 全历史聊天

```
./bin/feishu-archive sync --all-discovered
```

## 9. 补附件

```
./bin/feishu-archive attachments --workers 4
```

## 10. Wiki

```
./bin/feishu-archive wiki-sync
```

## 11. 邮箱

```
./bin/feishu-archive mail-sync
```

## 12. 健康检查

```
./bin/feishu-archive doctor
```

```
./bin/feishu-archive mail-doctor
```

## 13. 手工阅读器

```
./bin/feishu-archive serve
```

该命令占用当前窗口；在第二个 Terminal 做验收，安装后台服务前回到此窗口按 `Control + C`。

## 14. 聊天导出

在阅读器“消息”页面选择一个会话，点击“导出 HTML”或“导出 JSON”。

## 15. 邮箱解锁

```
./bin/feishu-archive mail-reader-url --open
```

## 16. AI数据测试

```
./bin/feishu-archive insights-run --no-model --dry-run
```

## 17. AI正式运行

```
./bin/feishu-archive insights-run
```

仅当审核后的模型参数已经写成源码默认值时使用无参数命令；否则完整重复第 38.1 节的 `--timezone`、`--host`、`--user`、`--identity-file`、`--model`、`--local-port` 和 `--remote-port`。

## 18. AI状态

```
./bin/feishu-archive insights-status
```

## 19. 可选的 8067 Bearer 路线

复制模型管理员提供的 Bearer token 后：

```
pbpaste | ./bin/feishu-archive insights-configure --bearer-token-stdin
printf '' | pbcopy
```

```
./bin/feishu-archive insights-run \
  --date 2026-08-12 \
  --timezone Asia/Shanghai \
  --host 192.168.1.50 \
  --user modeluser \
  --identity-file "$HOME/.ssh/id_ed25519_feishu_archive" \
  --model vmlx/qwen3-32b-8bit \
  --local-port 18135 \
  --remote-port 8067
```

## 20. 自动运行（核心归档）

```
./scripts/install-local.sh --without-insights
```

日报已经人工验收、模型默认参数已配置且数据处理授权已确认后，才执行：

```
./scripts/install-local.sh --with-insights
```

---

# 附录B：飞书主应用配置卡

**应用类型**

```
企业自建应用
```

**能力**

```
机器人：开启
事件订阅：不配置
```

**Redirect URL**

```
http://127.0.0.1:8766/oauth/callback
```

**Permissions**

```
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

**最后操作**

```
创建版本
→ 设置可用范围
→ 申请发布
→ 管理员批准
→ 重新执行 auth
```

---

# 附录C：飞书 Mail 应用配置卡

本卡默认用于独立邮箱应用。如果复用主应用，则把下面权限追加到主应用、发布新版本，跳过 `mail-configure`，仍执行 `mail-auth`。

**应用类型**

```
企业自建应用
```

**事件订阅**

```
不配置
```

**Redirect URL**

```
http://127.0.0.1:8766/oauth/callback
```

**Permissions**

```
mail:user_mailbox:readonly
mail:user_mailbox.folder:read
mail:user_mailbox.message:readonly
mail:user_mailbox.message.subject:read
mail:user_mailbox.message.address:read
mail:user_mailbox.message.body:read
offline_access
```

**最后操作**

```
创建版本
→ 设置可用范围
→ 申请发布
→ 管理员批准
→ 执行 mail-auth
```

---

# 附录D：部署完成后的标准数据流

```
                          飞书
                            │
              ┌─────────────┼─────────────┐
              │             │             │
             IM            Wiki          Mail
              │             │             │
              └────── OAuth User Token ───┘
                            │
                            ▼
                 Feishu Archive on Mac
                            │
        ┌───────────────────┼────────────────────┐
        │                   │                    │
 archive.sqlite3       mail.sqlite3       attachments/blobs
        │                   │                    │
        └───────────────────┼────────────────────┘
                            │
                        SQLite FTS5
                            │
                            ▼
                  127.0.0.1:8765
                     本地阅读器
                            │
              ┌─────────────┴────────────┐
              │                          │
         搜索/阅读/导出             Daily Insights
                                         │
                                    SSH Tunnel
                                         │
                                         ▼
                                 vMLX模型服务器
                                         │
                         ┌───────────────┼───────────────┐
                         │               │               │
                      昨日总结        今日计划        商业机会
```

这也是部署和故障排查时最重要的逻辑：

> **飞书归档层和AI分析层是两个相互分离的层。**

即使模型服务器停机：

> 聊天、知识库和邮件仍应继续同步。

即使飞书暂时无法访问：

> 已经保存到本机的档案仍然可以离线阅读和搜索。

这正是 Feishu Archive 当前架构的核心价值。

---

# 附录E：飞书官方接口参考

以下链接于 2026年8月24日按本文适用源码基线核验。飞书开放平台的页面名称、权限名称或接口限制以后可能调整；部署新版源码时应重新核对。

**OAuth**

- [浏览器网页授权接入指南](https://open.feishu.cn/document/sso/web-application-end-user-consent/guide)

**聊天与资源**

- [获取会话历史消息](https://open.feishu.cn/document/server-docs/im-v1/message/list)
- [话题概述](https://open.feishu.cn/document/im-v1/message/thread-introduction)
- [获取用户或机器人所在的群列表](https://open.feishu.cn/document/server-docs/group/chat/list)
- [搜索消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/search)
- [获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)

**知识库与新版文档**

- [获取知识空间列表](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space/list)
- [获取知识空间子节点列表](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/list)
- [获取新版文档纯文本内容](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/raw_content)
- [获取新版文档所有块](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/list)

**飞书邮箱**

- [飞书邮箱 Mail OpenAPI](https://open.feishu.cn/document/server-docs/mail-v1/user_mailbox)
- [获取邮箱文件夹列表](https://open.feishu.cn/document/mail-v1/user_mailbox-folder/list)
- [获取文件夹或标签中的邮件列表](https://open.feishu.cn/document/mail-v1/user_mailbox-message/list)
- [搜索邮件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/mail-v1/user_mailbox/search)
