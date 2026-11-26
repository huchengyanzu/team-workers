---
name: device-remote
description: >
  远程设备控制。当用户要求「遥控某台设备」「看看某台设备在不在线 / 查设备状态 / 列出设备」、
  「在某台机器上执行一条命令 / 下发指令 / 跑个命令看输出」、「查询已下发的长任务结果」、
  「把设备上的文件安全取到本机」、「新增一台受控设备 / 重置设备私钥 / 禁用或删除设备」时使用。
  通过 WebSocket 远程控制服务端（hub）的 REST API 操作受控设备；
  所有控制端请求用 X-Control-Token 鉴权。涉及设备侧命令的风险分级与二次确认规则见正文。
---

# device-remote（远程设备控制）

控制端 skill：经 hub 的 REST API 遥控已登记设备。不连真机、不读设备私钥、不部署服务端。
脚本 `scripts/remote.py` 仅依赖 Python 3 标准库。

统一前缀：`python3 scripts/remote.py [--json] <子命令> ...`。`exec` / `pull` 的 `--json` 输出统一结构化回执；其它子命令保持原始 JSON。

## 配置 token

每个用户 token 独立，只能控名下设备（跨用户返回 404）；重置后旧值失效。优先级从高到低：

```bash
--token '<token>'                          # 命令行临时
export REMOTE_TOKEN='<token>'              # 环境变量
# scripts/config.local.json（推荐，权限 600）：{"server":"https://hub.example.com","token":"<token>"}
```

获取：管理台 `https://hub.example.com/admin.html` →「用户管理」→ 控制 TOKEN 列点「复制」（仅超管可见；普通用户找超管要）。
token 不得出现在日志、回执、命令行回显中。**设备私钥 ≠ 控制 token**：私钥填在设备端（create/reset-key 时只回显一次），token 填本 skill。

无账号时自助注册（无需现成 token）：

```bash
python3 scripts/remote.py register user@example.com
python3 scripts/remote.py register-confirm user@example.com <6位验证码> 'Str0ng!Pass2026'
```

验证码 5 分钟有效、最多 5 次、一次性、不回显；注册后 token 自动写入 config.local.json。

## 网络与 TLS 前置检查（连不上先看这里）

- 默认 hub 是 `hub.example.com`；旧域名 `old-hub.example.com` 已停用且不再解析。文档/配置里若出现旧地址，一律替换为 `https://hub.example.com`（设备端为 `wss://hub.example.com/ws`）。
- 本 skill 脚本只用 Python 3 标准库，但 HTTPS 依赖 Python/系统的 OpenSSL。**Windows 推荐 Python 3.13+（OpenSSL 3.5.x）或 Anaconda Python（3.11.15 + OpenSSL 3.5.5）**；用 `python -c "import ssl; print(ssl.OPENSSL_VERSION)"` 确认。不要降级到 OpenSSL 3.0.x（`libssl-3.dll`），那是已知会随机握手失败的栈。
- 设备端官方单文件程序已内置 OpenSSL 3.5.5（Windows 随包 `libssl-3-x64.dll` / `libcrypto-3-x64.dll`），无需单独安装或删除 DLL；自编译时必须用 Anaconda Python 并带上这两个 DLL。
- 典型失败：`ConnectionResetError [WinError 64/10054]`、`ssl.SSLError`、`Tunnel connection failed: 502`、设备持续 offline。先查 OpenSSL 版本，再查网络/代理是否放行 `hub.example.com:443`；代理拦截 `*.example.com` 时任何版本都连不上。
- token 错误是 401、设备离线是 409；出现上述 TLS/网络报错时不要误判为 token 问题。

## 命令清单

| 命令 | 用法 | 要点 |
|---|---|---|
| `list` | `list` | 设备 ID/名称/平台/状态/心跳；在线只看 status |
| `exec` | `exec [--expect-hostname HOST] <设备ID或名称> -- <命令> [参数...]` | `--` 后为设备命令；统一回执含 stdout/退出码/双耗时；超时返回 task_id；失败非零退出 |
| `pull` | `pull [--expect-hostname HOST] <设备ID或名称> <远程路径> [--name 文件名] [--overwrite]` | 来源预检 + 字节校验后原子保存到 `~/Downloads`；默认防覆盖 |
| `task` | `task task-xxxx` | 查长任务：queued/dispatched/done/failed/timeout |
| `file-read` | `file-read dev-xxxx <路径> [-o 本地文件]` | 旧式 base64 直读；安全下载请用 `pull`；单文件上限 8MB |
| `file-write` | `file-write dev-xxxx <路径> --from 本地文件` | 覆盖写 |
| `file-list` | `file-list dev-xxxx <路径>` | 列目录 |
| `create` | `create "显示名" macos\|win\|linux` | 返回一次性私钥，不落盘 |
| `enable` / `disable` | `enable dev-xxxx` / `disable dev-xxxx` | 禁用会断开长连接 |
| `reset-key` | `reset-key dev-xxxx` | 重置私钥，一次性回显 |
| `delete` | `delete dev-xxxx --yes` | 不可逆，强制 --yes |
| `register` / `register-confirm` | 见上 | 邮箱注册 |

`exec` 走设备命令白名单；`file-*` 是设备端原生文件原语，不受 exec 白名单限制，支持二进制与分块传输。

## A/B：可信 exec 回执与安全 pull

```bash
# 绿级只读命令直接执行；主机名不匹配会拒发
python3 scripts/remote.py exec --expect-hostname EXAMPLE-HOST dev-xxxx -- cmd /c ver
python3 scripts/remote.py --json exec --expect-hostname EXAMPLE-HOST dev-xxxx -- cmd /c ver

# 先预检桌面文件，再严格解码并原子保存到 ~/Downloads
python3 scripts/remote.py pull --expect-hostname EXAMPLE-HOST dev-xxxx 'C:\Users\user\Desktop\demo.txt'
# 默认文件名：demo_<task_id>.txt；需要指定名字时仅可传单个 basename
python3 scripts/remote.py pull dev-xxxx 'C:\Users\user\Desktop\demo.txt' --name demo.txt
# 已存在同名文件：非交互模式默认冲突；确认覆盖必须显式加 --overwrite
python3 scripts/remote.py pull dev-xxxx 'C:\Users\user\Desktop\demo.txt' --overwrite
```

- 设备目标支持精确 `device_id` 或唯一 `display_name`，必须在线且启用；`--expect-hostname` 精确比对 `info.hostname`。
- `exec` 回执包含 `task_id`、`target{device_id,display_name,hostname}`、`command`、`status`、`stdout`、`stderr`、`exit_code`、`duration_ms`、`elapsed_ms`、`error`。非零退出码保留原值，CLI 以非零状态结束；连接失败、离线、超时、协议错误不会伪装成命令失败。
- `pull` 回执包含 `task_id`、`preflight_task_id`、`target`、`source_path`、`destination_path`、`status`、`expected_bytes`、`actual_bytes`、`elapsed_ms`、`error`。base64 严格解码，传输大小与预检大小都必须一致，否则不落盘。
- 默认落地根为解析后的 `~/Downloads`；默认名 `<stem>_<task_id><suffix>`。目标被限制在下载根内，拒绝最终软链接和目录；临时文件同目录写盘、fsync、字节复核后原子发布。
- 同名文件默认冲突并展示源/目标/大小。交互模式只接受精确“确认”，其它输入均取消；非交互或 `--json` 模式必须显式 `--overwrite`。确认前不创建临时文件，默认发布使用 no-replace，绝不安静覆盖。

## 安全规则：指令分级（下发前必判）

按命令**实际影响**判断，不以字面为准：

| 级别 | 典型操作 | 处理 |
|---|---|---|
| 🟢 绿 | 只读：cat/head/tail/ls/pwd/ps、状态与磁盘网络查询 | 直接执行，按三段式回执 |
| 🟡 黄 | 覆盖/移动文件、写非登记目录、改配置、装依赖；`pull --overwrite` 覆盖本地同名文件 | 先告知对象与预计影响，获同意再下发，不得扩大范围 |
| 🔴 红 | 删除、关机重启、写系统目录（/etc、/usr、/System、C:\Windows）、不可逆批量、清空日志/数据库 | 强制二次确认，见下 |

**红级二次确认（强制）**

1. 不下发，先展示文案，必须含**对象 + 数量**，例：「即将在 dev-xxxx 删除 3 个文件：/data/a.log、/data/b.log、/data/c.tmp。不可逆，请回复「确认」或「取消」。」
2. 只认字面「确认」/「取消」（"好""OK""嗯"一律不算）。
3. 发出后开始计时，**2 分钟内**未确认按取消，不下发，回执「已超时取消」。
4. 取消或超时不下发；确认后立即执行并三段式回执。

其它：路径/ID/命令串建议用户粘贴文本而非语音转录；一台设备的红级授权不延伸到另一台；回执中不夹带未授权操作。

## 回执格式（三段式，每次操作后）

1. **做了什么**：一句话说明操作 + 设备/工具。
2. **对象 + 数量**：设备 ID、文件/进程等对象与数量。
3. **失败原因 + ≤2 条建议**：有失败才给，最多 2 条可执行建议；无失败写「无失败」。

`exec` / `pull` 的三段式说明必须同时保留结构化回执关键字段：任务编号、固定目标、状态；命令输出/退出码/双耗时，或文件源/目标/预期与实际字节数。不得只回“完成”，也不得把 token 或设备私钥写入回执。

## 设备离线：零静默

offline（status != online、下发返回 409、心跳超时）必须明确回「离线」+ 设备 ID + 最后心跳时间，
绝不静默、不伪造结果、不说"已执行"。建议先查设备端进程/网络或用 `list` 复查。

## 设备端程序（用户问"怎么装/怎么用"时）

被控机器上运行的单文件程序，无需 Python/依赖。管理台「下载中心」按系统下载：

| 平台 | 文件 | 运行 |
|---|---|---|
| Win x86_64 | `team-workers-device-win.exe` | 双击（首次自动开配置页） |
| macOS | `team-workers-device-mac` / `team-workers-device.app` | `./team-workers-device-mac` 或双击 app |
| Linux x86_64 | `team-workers-device-linux` | `chmod +x team-workers-device-linux && ./team-workers-device-linux` |

首次配置：运行后自动开 `http://127.0.0.1:18770/`（占用顺延，只绑回环）→ 填服务端 `wss://hub.example.com/ws`（必须 `/ws` 结尾，写成 `/cn/ws` 会 404 离线）+ 设备 ID/私钥（管理台「创建设备」下发，只显示一次）+ 白名单全选 + 文件范围不限 → 「保存并连接」，显示「在线」即成功。

Win 备用：配置页报 `CSRF check failed` 时用 `windows-setup.ps1` 直接写配置绕过页面。
配置页有「开机自启动」（Win 写 HKCU Run；mac 写 LaunchAgent，后台启动不弹浏览器）和「退出程序」。
手动抑制弹浏览器：`--no-browser`（三端）或 Mac `TEAM_WORKERS_NO_BROWSER=1`。
上线后用 `list` 找 device_id 即可 exec / 文件工具。
