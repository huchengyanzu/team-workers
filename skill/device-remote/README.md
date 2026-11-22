# device-remote（远程设备控制 skill）

一个通用的 Agent Skill，让 AI 助手通过远程控制服务端（hub）的 REST API 操作受控设备：
列设备、查状态、下发命令、查长任务、创建/管理设备。纯 Python 标准库实现，
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
export REMOTE_SERVER='https://hub.example.com'   # 可省略，此为默认值
export REMOTE_TOKEN='<运维提供的控制 token>'
```

- `REMOTE_SERVER`：服务端 REST 根地址。脚本自动拼接 `/api/...`。
- `REMOTE_TOKEN`：**每个用户有自己独立的控制 token**（对应请求头 `X-Control-Token`），
  由服务端管理台「用户管理」创建账号时下发（创建/重置时仅显示一次）。
  用谁的 token 就只能看到和控制谁的设备，跨用户的设备返回 404。
  管理员的 token 即服务端 `.env` 里的 `CONTROL_TOKEN`（旧部署迁移后归管理员，保持不变）。
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
`config.local.json`。需服务端已配置 SMTP 邮件。

本机测试可用 `--base` / `--token` 覆盖环境变量，无需改动全局配置。

## 命令示例

```bash
# 列设备（ID / 名称 / 平台 / 状态 / 最后心跳）
python3 scripts/remote.py list

# 下发 exec 命令：用 -- 分隔，使 -la 等参数不被当成脚本选项
python3 scripts/remote.py exec dev-xxxx -- echo hi
python3 scripts/remote.py exec dev-xxxx -- ls -la /tmp

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

仅 `exec` / `start_job` / `query_job` / `cancel_job` 可用；其它工具服务端返回 400。
`exec` 的 `arguments` 为 `{"command": ["命令名", "参数1", ...]}`。

### 错误码提示

- `401`：控制 token 错误 → 检查 `REMOTE_TOKEN` / `--token`。
- `409`：设备离线 → 明确回「离线」，附设备 ID，不静默。
- `400`：tool 不在白名单或参数错误。
- 请求超时：提示服务端不可达 / 网络过慢。

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
设备离线必须明确回复「离线」，零静默。

## 设备端程序使用说明

本 skill 控制的是**被控机器**上运行的设备端程序。管理台「下载中心」可下载三端单文件程序（无需 Python）：

- **Windows**：`nexus-device-win.exe`，双击运行。
- **macOS**：`Nexus Remote Device.app`（双击）或 `nexus-device-mac`（终端运行）。
- **Linux**：`chmod +x nexus-device-linux && ./nexus-device-linux`。

**首次配置**：运行后自动打开本地配置页 `http://127.0.0.1:18770/`（只绑回环），
填入服务端地址 `wss://hub.example.com/ws`、设备 ID 与私钥（管理台「设备管理 → 创建设备」下发，私钥只显示一次），
点「保存并连接」显示「在线」即可。配置页还有「开机自启动」开关和「退出程序」按钮。

设备上线后用 `python3 scripts/remote.py list` 找到它的 device_id，即可用本 skill 遥控。
注意：设备端填的是**私钥**，本 skill 填的是**控制 token**，二者不同、不要混用。
服务端地址必须是 `/ws` 结尾，写成其它路径会 404 离线。更多细节见 `SKILL.md` 的「设备端程序使用说明」。
