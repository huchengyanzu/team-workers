# device-remote（远程设备控制 skill）

一个通用的 Agent Skill，让 AI 助手通过远程控制服务端（hub）的 REST API 操作受控设备：
列设备、查状态、下发命令、查长任务、安全拉取文件、创建/管理设备。纯 Python 标准库实现，
兼容 Claude Code / Cursor / WorkBuddy。

> 本技能只面向控制端：不连真机、不部署服务端、不读取设备私钥文件、不打印 token 明文。

## 目录结构

```
device-remote/
├── SKILL.md            # 技能说明（AI 读取的触发/配置/安全/回执规则）
├── README.md           # 本文件（安装与使用）
└── scripts/
    └── remote.py       # 控制端 CLI（纯标准库，urllib）
```

## 安装

把整个 `device-remote/` 目录拷贝到对应工具的 skills 目录（目录名即技能名 `device-remote`）：

**Claude Code（全局）**

```bash
cp -R device-remote ~/.claude/skills/
```

**Cursor**

Cursor 支持项目级与全局 skills；任选其一：

```bash
cp -R device-remote ~/.cursor/skills/          # 全局
# 或
cp -R device-remote <你的项目>/.cursor/skills/  # 项目级
```

**WorkBuddy**

```bash
cp -R device-remote ~/.workbuddy/skills/
```

安装后，AI 助手在识别到「遥控设备 / 查设备状态 / 下发命令 / 查长任务 / 管理设备」等意图时，
会自动加载 `SKILL.md` 并按其中的安全规则执行。

## 配置

```bash
export REMOTE_SERVER='https://hub.example.com'   # 示例地址，替换为你的服务端
export REMOTE_TOKEN='<运维提供的控制 token>'
```

- `REMOTE_SERVER`：服务端 REST 根地址。脚本自动拼接 `/api/...`。
- `REMOTE_TOKEN`：**每个用户有自己独立的控制 token**（对应请求头 `X-Control-Token`），
  由服务端管理台「用户管理」创建账号时下发（创建/重置时仅显示一次）。
  用谁的 token 就只能看到和控制谁的设备，跨用户的设备返回 404。
  管理员可在「用户管理」里查看或重置自己的控制 token。
  **不要用设备私钥代替控制 token。**
- **去哪里取 token**：登录管理台 `https://hub.example.com/admin.html` → 左侧「用户管理」（仅超管可见）→
  表格「控制 TOKEN」列找到自己账号，复制。普通用户看不到该页时，用下方自助注册或找超管要 token。
  token 在页面上可能截断显示，点「重置 Token」可重新生成并完整显示一次（旧值立即失效）。
- token 不写日志；可用 `--token` 临时传入。若 token 被重置，旧 token 立即失效。
- 免 token 的 exec 环境（如 myclaw）：可把 `{"server":"...","token":"..."}` 写入
  `scripts/config.local.json`，脚本自动读取（该文件已 gitignore，勿提交/勿回显）。
  自助注册成功后也会自动写入这个文件，之后所有命令免 token。

### 新用户自助注册（邮箱验证码）

没有账号时无需 token：

```bash
python3 scripts/remote.py register user@example.com
python3 scripts/remote.py register-confirm user@example.com 123456 'Str0ng!Pass2026'
```

验证码发到邮箱（5 分钟有效、最多试 5 次、一次性），注册成功后专属 token 自动写入
`config.local.json`。需服务端已配置 Resend 邮件（统一发信 `no-reply@notify.example.com`）。

本机测试可用 `--base` / `--token` 覆盖环境变量，无需改动全局配置。

## 运行环境与 TLS / OpenSSL（重要）

- **服务端地址由你的部署决定**：文档示例统一写作 `hub.example.com`；设备端填 `wss://<你的域名>/ws`。
- **控制端 `remote.py`**：Python 3 + 标准库，无第三方依赖。Windows 上如果 TLS 握手失败，换用 **Python 3.13+（OpenSSL 3.5.x）** 或 **Anaconda Python（Python 3.11.15 + OpenSSL 3.5.5）**。查看版本：`python -c "import ssl; print(ssl.OPENSSL_VERSION)"`。
- **设备端单文件程序**：官方下载包已内置 OpenSSL 3.5.5（Windows 随包带 `libssl-3-x64.dll` / `libcrypto-3-x64.dll`），**无需另装，也不要删除随包 DLL**。不要自行用系统 Python 重打包：系统 Python 3.13.12 + OpenSSL 3.0.18（`libssl-3.dll`）在本链路会随机握手失败。
- **典型症状**：`ConnectionResetError [WinError 64/10054]`、`ssl.SSLError`、`Tunnel connection failed: 502`、设备一直 offline。先确认 OpenSSL 版本，再确认网络出口/代理放行 `hub.example.com:443`；代理拦截该域名时任何版本都连不上。
- **不要降级到 OpenSSL 3.0.x**：那正是会失败的栈；应升到 3.5.x 或直接使用官方包。

## 命令示例

```bash
# 列设备（ID / 名称 / 平台 / 状态 / 最后心跳）
python3 scripts/remote.py list

# 下发 exec：设备可用 ID 或唯一显示名；expect-hostname 必须精确匹配
python3 scripts/remote.py exec --expect-hostname EXAMPLE-HOST dev-xxxx -- cmd /c ver
python3 scripts/remote.py exec dev-xxxx -- ls -la /tmp
# --json 输出统一回执：stdout/退出码/设备耗时/端到端耗时/错误分类
python3 scripts/remote.py --json exec dev-xxxx -- cmd /c ver

# 安全拉取文件到 ~/Downloads：先预检，再严格解码与字节校验，最后原子落盘
python3 scripts/remote.py pull --expect-hostname EXAMPLE-HOST dev-xxxx 'C:\Users\user\Desktop\demo.txt'
# 默认名为 demo_<task_id>.txt；--name 仅接受单个 basename
python3 scripts/remote.py pull dev-xxxx 'C:\Users\user\Desktop\demo.txt' --name demo.txt
# 同名文件默认冲突；确认覆盖时显式加 --overwrite
python3 scripts/remote.py pull dev-xxxx 'C:\Users\user\Desktop\demo.txt' --overwrite
python3 scripts/remote.py --json pull dev-xxxx 'C:\Users\user\Desktop\demo.txt'

# 长任务：15s 内无结果会返回 task_id，用 task 查询
python3 scripts/remote.py task task-xxxxxxxx

# 创建设备（打印一次性私钥，只显示一次）
python3 scripts/remote.py create "客厅 Mac mini" macos

# 启用 / 禁用 / 重置私钥 / 删除（delete 必须带 --yes）
python3 scripts/remote.py enable  dev-xxxx
python3 scripts/remote.py disable dev-xxxx
python3 scripts/remote.py reset-key dev-xxxx
python3 scripts/remote.py delete  dev-xxxx --yes

# 输出原始 JSON，便于脚本处理
python3 scripts/remote.py --json list
```

### 工具白名单

可用工具为 `exec` / `start_job` / `query_job` / `cancel_job` / `file_read` / `file_write` / `file_list`；其它工具服务端返回 400。
`exec` 的 `arguments` 为 `{"command": ["命令名", "参数1", ...]}`。

### exec / pull 结构化回执

- 目标解析：精确 `device_id` 优先，否则匹配唯一 `display_name`；设备必须在线且启用。`--expect-hostname` 精确比对 `info.hostname`，不匹配不下发。
- `exec`：回执含 `task_id`、`target`、`command`、`status`、`stdout`、`stderr`、`exit_code`、`duration_ms`、`elapsed_ms`、`error`。命令非零退出保留真实退出码；连接/超时/协议错误有独立状态；任何失败 CLI 都非零退出。
- `pull`：回执含 `task_id`、`preflight_task_id`、`target`、`source_path`、`destination_path`、`status`、`expected_bytes`、`actual_bytes`、`elapsed_ms`、`error`。来源先经 `file_list` 确认为普通文件并记录大小，再经 `file_read` 严格 base64 解码；解码长度、读取大小、预检大小不一致均不落盘。
- 落盘安全：默认根为解析后的 `~/Downloads`，默认名 `<stem>_<task_id><suffix>`；`--name` 拒绝路径分隔符、`.`、`..` 和空名。目标限制在下载根内，拒绝最终软链接与目录；同目录临时件写盘/fsync/复核后原子发布。
- 防覆盖：同名文件默认 `conflict`，展示源/目标/大小。交互模式只接受精确“确认”，其它输入为 `cancel`；非交互或 `--json` 模式需显式 `--overwrite`。确认前不写临时文件；默认发布 no-replace，`--overwrite` 才用原子替换。

### 错误码提示

- `401` → `auth_error`：控制 token 错误 → 检查 `REMOTE_TOKEN` / `--token`。
- `403` → `permission_error`；`404` → `not_found`；`409` → `offline`，明确回「离线」并附设备 ID。
- `400` → `rejected`；`429` → `rate_limited`；其它 HTTP 错误 → `http_error`。
- 网络失败 → `connect_error`；传输/回执/执行超时 → `timeout`；缺字段或类型非法 → `protocol_error`。
- 命令非零退出或设备返回 `ok=false` → `command_failed`，保留真实退出码（如有）。
- `pull` 另有 `source_not_found` / `source_changed` / `conflict` / `cancel` / `io_error`；失败均不静默。

## 安全规则摘要

下发命令前由 AI 判断风险等级：

- 🟢 **绿**（读文件、查状态、列目录）：可直接执行。
- 🟡 **黄**（覆盖/移动、写非登记目录）：执行前先告知用户并获得同意。
- 🔴 **红**（删除、关机、系统目录、不可逆批量）：**必须二次确认**——
  文案含操作对象+数量，只接受「确认」/「取消」精确回复，2 分钟超时按取消处理。

精确路径/文件名/命令串建议用户打字提供而非语音；日志与回执不打印 token 与私钥明文；
本 skill 不存储私钥、不绕过多机确认。完整规则见 `SKILL.md`。

## 回执格式

每次操作后按三段式回执：①做了什么 ②对象+数量 ③失败原因+≤2 条建议（无失败写"无失败"）。
`exec` / `pull` 还必须保留任务编号、固定目标、状态及输出/退出码/双耗时或源/目标/字节数，不得只回“完成”。
设备离线必须明确回复「离线」，零静默；token 与设备私钥不得进入日志或回执。

## 设备端程序使用说明

本 skill 控制的是**被控机器**上运行的设备端程序。管理台「下载中心」可下载三端单文件程序（无需 Python）：

- **Windows**：`team-workers-device-win.exe`，双击运行。
- **macOS**：`team-workers-device.app`（双击）或 `team-workers-device-mac`（终端运行）。
- **Linux**：`chmod +x team-workers-device-linux && ./team-workers-device-linux`。

**首次配置**：运行后自动打开本地配置页 `http://127.0.0.1:18770/`（只绑回环），
填入服务端地址 `wss://hub.example.com/ws`、设备 ID 与私钥（管理台「设备管理 → 创建设备」下发，私钥只显示一次），
点「保存并连接」显示「在线」即可。配置页还有「开机自启动」开关和「退出程序」按钮。

设备上线后用 `python3 scripts/remote.py list` 找到它的 device_id，即可用本 skill 遥控。
注意：设备端填的是**私钥**，本 skill 填的是**控制 token**，二者不同、不要混用。
服务端地址必须是 `/ws` 结尾，写成其它路径会 404 离线。Windows 上若自编译，必须遵守上文「运行环境与 TLS / OpenSSL」要求；官方 exe 已内置正确 OpenSSL，无需处理。更多细节见 `SKILL.md` 的「设备端程序使用说明」。
