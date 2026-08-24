# FeishuLocalization（Feishu Archive）手工部署与飞书配置作业指导书（SOP V1.0）

**文件性质：** 普通用户手工部署与配置标准作业指导书
**适用项目：** FermiWang/FeishuLocalization
**项目程序名称：** Feishu Archive
**依据版本：** GitHub `main` / Feishu Archive v0.5.3
**编制日期：** 2026年8月24日
**适用对象：** 不具备 Python、Git、服务器运维经验的普通飞书用户
**推荐运行平台：** macOS
**目标：** 不依赖 Codex 自动部署，由普通用户通过复制命令、飞书网页配置和少量本机操作，实现当前项目已经具备的全部可用功能。

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
| 普通文件归档 | 支持，但受飞书接口条件限制 |
| 聊天全文搜索 | 支持，SQLite FTS5 |
| 单会话 JSON 导出 | 支持 |
| 单会话自包含 HTML 导出 | 支持 |
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

上述能力范围由当前 README、CLI 和源代码共同确定。

---

# 二、非常重要的四个结论

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

当前安装脚本会分别创建聊天、知识库、邮箱和洞察的本机计划任务。

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

**不要删除或修改**`/usr/bin/python3`**。**

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

输入：

```
cd
```

注意 `cd` 后面有一个空格。

然后直接把刚才的 **FeishuLocalization-main 文件夹拖进 Terminal 窗口**。

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
0.5.3
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

[飞书开放平台](https://open.feishu.cn/)

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

飞书官方开发者资料也将企业自建应用的机器人能力作为调用相关群聊/消息接口的标准配置步骤。

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

飞书 OAuth 要求实际使用的 redirect URL 预先登记在应用安全设置中。

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

当前项目支持两种方式：

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

### 方式B

一个应用承担全部权限。

虽然方式B少一个应用，但方式A具有明显优点：

> 邮件权限可以单独授权、撤销和审计。

因此本指导采用**两个应用**。

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

# 十九、第十六阶段：配置邮箱 App ID / App Secret

在开放平台打开：

```
Feishu Archive Mail
```

复制 Mail App ID：

```
pbpaste | ./bin/feishu-archive mail-configure --app-id-stdin
```

复制 Mail App Secret：

```
pbpaste | ./bin/feishu-archive mail-configure --app-secret-stdin
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

---

# 二十八、第二十四阶段：启动本地阅读器

测试：

```
./bin/feishu-archive serve
```

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

到目前为止，所有功能还是“手工执行”。

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
./scripts/install-local.sh
```

**不要使用：**

```
sudo ./scripts/install-local.sh
```

项目使用的是当前用户自己的 LaunchAgent、Keychain 和档案目录，不需要 root 权限。

---

# 三十一、安装以后系统会自动做什么

当前安装脚本会建立以下后台服务：

| 时间/条件 | 工作 |
| --- | --- |
| 登录后 | 启动本地阅读器 |
| 03:30 | 聊天增量同步 |
| 03:45 | 知识库增量同步 |
| 04:00 | 邮箱增量同步 |
| 04:30 | 每日 Insights |
| 05:00 | 未完成 Insights 重试 |
| 05:30 | 未完成 Insights 再次重试 |
| 周期唤醒 | 历史 Insights 回填 |

聊天和邮箱的日常增量默认会采用约2天重叠窗口，用来捕获延迟出现、编辑或状态发生变化的内容。

安装脚本还会：

- 将稳定运行代码复制到档案目录的 `runtime`；
- 把日志统一放入 `logs`；
- 在更新失败时恢复上一版 runtime；
- 只有新版阅读器通过健康检查以后才删除回滚副本。

### 关于历史洞察回填的唤醒频率

README 的安装说明中仍有一处旧描述写成“每30分钟”。

但是当前实际源代码：

```
DEFAULT_INSIGHTS_BACKFILL_INTERVAL_SECONDS = 60
```

而安装脚本直接读取该参数生成 `StartInterval`。

所以当前 `main` 的实际行为应按：

> **每60秒唤醒一次回填调度器**

理解，而不是30分钟。

这并不等于每60秒就一定调用大模型；它还会检查模型是否空闲等条件。

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

检查邮箱同步：

```
launchctl print "gui/$(id -u)/com.fermiwang.feishu-archive-mail-sync"
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
```

这些参数来自当前 `config.py`。

这意味着：

> 把项目下载到任意一台新的 Mac 后，飞书功能可以按照本指导配置；但是如果那台机器没有访问 `192.168.100.179` 的条件，AI Insights 不会凭空工作。

---

# 三十五、普通部署用户应向模型管理员取得五项信息

如果单位已经有 vMLX 模型服务器，请模型管理员提供：

```
1. MODEL_HOST
2. SSH_USER
3. MODEL_ID
4. MODEL_PORT
5. SSH访问权限
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

不建议普通用户通过“关闭 StrictHostKeyChecking”等方法绕过安全检查，因为当前源代码本身就是故意禁止这种降级。

---

# 三十八、如果模型服务器和当前源码默认值不同

执行：

```
open -a TextEdit src/feishu_archive/config.py
```

找到：

```
DEFAULT_INSIGHTS_TIMEZONE = "Europe/Amsterdam"
DEFAULT_VMLX_HOST = "192.168.100.179"
DEFAULT_VMLX_USER = "apple"
DEFAULT_VMLX_MODEL = "vmlx/gemma-4-31b-it-8bit"
DEFAULT_VMLX_REMOTE_PORT = 11435
```

只修改双引号里面的值。

例如时区可以修改成实际所在地对应的 IANA 时区：

```
DEFAULT_INSIGHTS_TIMEZONE = "Asia/Shanghai"
```

或：

```
DEFAULT_INSIGHTS_TIMEZONE = "Asia/Singapore"
```

假设模型服务器为：

```
192.168.1.50
```

SSH 用户：

```
modeluser
```

模型：

```
vmlx/qwen3-32b-8bit
```

则改为类似：

```
DEFAULT_VMLX_HOST = "192.168.1.50"
DEFAULT_VMLX_USER = "modeluser"
DEFAULT_VMLX_MODEL = "vmlx/qwen3-32b-8bit"
```

保存。

### 为什么必须改这里？

因为手工 `insights-run` 可以通过命令行临时指定参数，但是当前 `install-local.sh` 创建的凌晨计划任务直接采用程序默认参数。

因此：

> 如果希望换一台机器以后仍然能够无人值守自动生成 Insights，应在运行 `install-local.sh` 之前把默认模型参数配置正确。

---

# 三十九、先测试数据抽取，不调用模型

执行：

```
./bin/feishu-archive insights-run --no-model --dry-run
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

模型连接配置好以后执行：

```
./bin/feishu-archive insights-run
```

然后：

```
./bin/feishu-archive insights-status
```

打开：

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

- [ ] 邮箱独立应用已建立
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
- [ ] 文件能够归档
- [ ] 关键词全文搜索有效

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
- [ ] Insights LaunchAgent 存在
- [ ] Insights Backfill LaunchAgent 存在

## H. Insights

- [ ] `insights-run --no-model --dry-run` 正常
- [ ] SSH 模型连接正常
- [ ] 实际模型 ID 与配置一致
- [ ] `insights-run` 成功
- [ ] `insights-status` 正常
- [ ] `/ ?mode=insights` 页面能够打开
- [ ] Yesterday Summary 有内容
- [ ] Today Plan 有内容
- [ ] Commercial Opportunities 有内容
- [ ] 历史回填已经启动

只有 A—H 全部满足，才可以称为：

> **“FeishuLocalization 当前版本全部现有功能已经完成部署。”**

---

# 四十五、常见故障及处理

| 现象 | 最常见原因 | 处理 |
| --- | --- | --- |
| `0个会话 · 0条消息` | 还没有真实同步 | 检查 `auth` → `discover` → `sync --all-discovered` |
| OAuth 打不开 | 浏览器没有自动启动 | 使用 `auth --no-open` |
| OAuth 回调失败 | 8766被占用或URL不一致 | 检查 `http://127.0.0.1:8766/oauth/callback` |
| 新增权限仍报缺权限 | 只勾选了权限，没有重新发布 | 创建新版本→发布→重新 `auth` |
| `mail-auth` 后缺权限 | 邮箱权限未发布 | 发布邮箱应用新版本→重新 `mail-auth` |
| 自动邮箱任务跳过 | 没有有效 Mail OAuth | 运行 `mail-auth`、`mail-doctor` |
| 群很少 | 机器人没有加入相关群 | 将应用机器人加入目标群 |
| 单聊比客户端少 | 飞书搜索/保留期限制 | 确认 `search:message` 权限 |
| 图片或附件缺少 | Bot不在群/超过100MB/DLP限制 | 加机器人，执行 `attachments --workers 4` |
| 文档只有标题没有正文 | 内容类型当前未适配 | 新版Docx支持；Sheet/Base等可能只有目录 |
| Wiki页面显示旧格式 | 本地正文渲染版本旧 | 执行 `wiki-rebuild` |
| 邮件正文有、附件没有 | 磁盘或附件上限触发 | 检查可用磁盘和 `mail-doctor` |
| 邮箱API 401 | 阅读器没有解锁 | `mail-reader-url --open` |
| HTML/SVG/脚本附件不能直接开 | 被安全隔离 | 在阅读器确认风险后二次下载 |
| `Address already in use` | 8765或8766已占用 | 停止旧实例 |
| TLS证书错误 | Python证书环境异常 | 使用 Python.org 正式版本并安装证书 |
| SQLite locked | 多进程或复制中写入 | 停止服务后再操作 |
| Insights只有统计没有智能结论 | 模型不可用 | 检查 SSH、模型、11435 |
| Insights模型不匹配 | 配置的 MODEL ID 与服务器不同 | 修改 `DEFAULT_VMLX_MODEL` |
| Insights凌晨不工作，手工指定参数却可以 | LaunchAgent仍用源码默认参数 | 修改 `config.py` 后重新执行安装脚本 |
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

在复制之前应先停止相关服务，避免 SQLite 正在写入。

Mac Keychain 中的 App Secret 和 OAuth Token 不会跟着这个目录一起迁移。

所以换新 Mac 后仍然需要重新：

```
configure
auth
mail-configure
mail-auth
```

---

# 四十九、升级 GitHub 版本

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

然后重新：

```
./scripts/install-local.sh
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
PORT
```

否则 Git 更新可能产生冲突或覆盖本地修改。

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

## 6. 初始化

```
./bin/feishu-archive init
```

## 7. 发现聊天

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

## 14. 邮箱解锁

```
./bin/feishu-archive mail-reader-url --open
```

## 15. AI数据测试

```
./bin/feishu-archive insights-run --no-model --dry-run
```

## 16. AI正式运行

```
./bin/feishu-archive insights-run
```

## 17. AI状态

```
./bin/feishu-archive insights-status
```

## 18. 自动运行

```
./scripts/install-local.sh
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
