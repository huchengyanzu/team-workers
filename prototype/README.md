# exec-server 原型（Task #8 stdio + Task #12 HTTP/token + Task #15 Windows 兼容 + Task #19 绑定策略）

> 状态：**本机原型**。在 macOS + Python 3.13 上通过 34 条单测（stdio/工具 10 条 + HTTP/鉴权 6 条 + Windows 模拟 11 条 + 绑定策略 7 条），HTTP **默认只绑 `127.0.0.1`**，只有显式设 `EXEC_BIND_HOST` 才绑局域网且强制要求 token；**Windows 兼容只做了静态修复与本机模拟测试，没有在真 Windows 上运行过**（待验项见「Windows 上还要验什么」）；未部署到任何真机、未在 DSH 里实际登记（属「待验证」）。
> 定位：验证 #1 方案里「跨机执行代理」最关键的那部分承诺（白名单命令、超时、退出码、输出截断、任务句柄、token 鉴权）。**不是产品**；若主人最终选裸 SSH 路线，本原型弃用。

## 文件清单

| 文件 | 说明 |
|---|---|
| `prototype/exec_server.py` | 单文件服务端：四个工具 + MCP stdio（换行分隔 JSON-RPC）+ HTTP（POST + JSON-RPC）两种传输 + Bearer token 鉴权 + 平台相关分支（切词/终止/编码/换行），仅用 Python 标准库 |
| `prototype/tests/test_exec_server.py` | stdio/工具层单测（10 条）：exec 五类、job 四类、stdio 协议一条 |
| `prototype/tests/test_http_transport.py` | HTTP/鉴权单测（6 条）：401 且不执行、正确 token 三交互、通知 202、非回环地址连不上、拒绝绑 0.0.0.0 |
| `prototype/tests/test_windows_compat.py` | Windows 兼容模拟单测（11 条）：双分隔符 basename、Windows 拆词保留反斜杠、含空格路径、白名单、平台默认白名单、cp936 解码兜底、spawn 参数 |
| `prototype/tests/test_bind_policy.py` | 绑定策略单测（7 条）：默认回环且非回环不可达、0.0.0.0/:: 拒绝、main 拒绝宽监听且退出码 2 无堆栈、局域网地址无 token 拒绝启动、局域网 + token 能起且横幅/health 回显地址、main 拒绝无 token 的局域网绑定、EXEC_BIND_HOST 解析 |
| `prototype/README.md` | 本文件 |

## 四个工具

| 工具 | 一句话 |
|---|---|
| `exec` | 前台执行一条白名单命令，返回 stdout / stderr / exit_code / duration_ms；支持超时，超长输出截断并标注 |
| `start_job` | 后台起长任务，立即返回稳定 `job_id`（输出落到系统临时目录，避免管道塞死） |
| `query_job` | 按 `job_id` 查状态与输出；状态含 running / finished / failed / cancelled / timeout；不存在的 id 返回明确错误而非抛异常 |
| `cancel_job` | 取消运行中的任务：POSIX 先 SIGTERM、1.5s 不退再整组 SIGKILL；Windows 走 `taskkill /T /F` 杀进程树；取消后 `query_job` 可查到 `cancelled` |

## 在本机跑起来（stdio 模式）

```bash
# 1) 跑全部单测（当前 34 条：stdio/工具 10 + HTTP 6 + Windows 模拟 11 + 绑定策略 7；原 stdio 10 条与 HTTP 6 条仍全绿）
python3 -m unittest discover -s prototype/tests -v

# 2) 以 stdio 方式起服务端（等待 stdin 上的 JSON-RPC）
python3 prototype/exec_server.py
```

手动发一条请求看效果（复制整段到终端）：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"manual","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"exec","arguments":{"command":["echo","hi"]}}}' \
  | python3 prototype/exec_server.py
```

## HTTP 模式（跨机调用 + Bearer token 鉴权）

token 只从环境变量或文件读取，代码里没有任何默认 token；**没给 token 时进程拒绝启动**（退出码 2），不会偷偷生成。

```bash
# 1) 生成一个随机 token（示例）
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(16))')

# 2) 起 HTTP 模式；只绑 127.0.0.1，端口可用 EXEC_HTTP_PORT 改（缺省 8100）
EXEC_TOKEN="$TOKEN" EXEC_HTTP_PORT=8100 python3 prototype/exec_server.py --http

# 3) 带 token 调 initialize / tools/list / tools/call
curl -sS http://127.0.0.1:8100/mcp \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# 4) 不带 token 或 token 错误 → 401，且命令不会被执行
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"exec","arguments":{"command":["echo","hi"]}}}'
```

也可以把 token 放进文件（建议 `chmod 600`）：`EXEC_TOKEN_FILE=/path/to/token.txt`。

行为约定：

- **默认只绑 `127.0.0.1`**；只有显式设置 `EXEC_BIND_HOST`（或调用 `make_http_server(host=...)`）才允许绑具体局域网地址。`0.0.0.0`/`::` 这类宽监听**永远拒绝**，没有开关（单测覆盖：默认时从本机非回环地址连必失败）。
- **绑局域网地址必须带 token**：没配 token 直接拒绝启动（退出码 2），并打印具体地址与原因；启动横幅会打印「本次监听在局域网地址 X，任何能访问该地址的人在没有 token 时会被拒（HTTP 401）」。
- **装完自检**：`GET /health` 不需要 token，返回当前 `bind_host` / `bind_port` / `loopback_only` / `auth_required`，一眼核对是否绑错地址。
- 无 token / token 错误：先返回 401，**在 dispatch 之前就拒绝**，任何命令都不会执行（单测用 `touch` 建文件做副作用探针验证）。
- 通知类消息（无 `id`）返回 `202` 空响应体；其余请求返回 `200` + JSON-RPC 响应；JSON 解析失败 `400`。
- 基础 Streamable HTTP 风格：单次 POST 请求/响应，未实现 `Mcp-Session-Id`、SSE 流与完整会话协商（见「待实现」）。

自检示例（不需要 token）：

```bash
curl -sS http://127.0.0.1:8100/health
# {"ok": true, "bind_host": "127.0.0.1", "bind_port": 8100, "loopback_only": true, "auth_required": true}
```

配置项（环境变量）：

- `EXEC_TRANSPORT` / `--http`：选 HTTP 模式（缺省 stdio）。
- `EXEC_BIND_HOST`：HTTP 绑定地址，缺省 `127.0.0.1`；**只有显式设置才绑局域网**（如 `192.168.1.10`）；`0.0.0.0`/`::` 拒绝。
- `EXEC_HTTP_PORT`：HTTP 端口，缺省 8100。
- `EXEC_TOKEN` / `EXEC_TOKEN_FILE`：HTTP 模式必填其一（token 文件读取后去空白）；绑非回环地址时也走同一份 token 校验。
- `EXEC_ALLOWLIST`：逗号分隔的命令白名单，缺省按平台（POSIX：echo,sleep,ls,pwd,whoami,cat,false,true；Windows：cmd,powershell,whoami,ipconfig,tasklist,where）。
- `EXEC_MAX_OUTPUT_BYTES`：stdout/stderr 各自截断阈值，缺省 8192。
- `EXEC_DEFAULT_TIMEOUT`：`exec` 缺省超时秒数，缺省 10。
- `EXEC_WINDOWS_MODE`：强制 Windows 分支（`1`/`true`），仅用于测试/排障。
- `EXEC_OUTPUT_ENCODING` / `EXEC_OUTPUT_FALLBACK_ENCODING`：覆盖子进程输出解码（Windows 中文系统用）。

## 怎么接进 DSH

stdio 形态的 MCP server，登记为 DSH 的 MCP server（子进程方式）示意配置：

```json
{
  "mcpServers": {
    "exec-server": {
      "command": "python3",
      "args": ["/绝对路径/prototype/exec_server.py"],
      "env": { "EXEC_ALLOWLIST": "echo,sleep,ls,cat", "EXEC_DEFAULT_TIMEOUT": "10" }
    }
  }
}
```

HTTP 形态要登记成「远程 MCP server」，需要 DSH 侧支持 remote/streamable-http 的 MCP 配置；本卡**没有**在 DSH 里实际登记调通，属「待验证」项（stdio 与 HTTP 协议本身已被单测覆盖；DSH 能否接 stdio 由 Task #11 另行验证）。

注意：字段名与放置位置以 DSH 当前版本的 MCP 配置口径为准，不要照抄猜测。

## Windows 兼容（静态修复已做，真 Windows 未验）

背景：这台 server 最终要装在两台 Windows 上，但 #8/#12 的代码按 macOS/Linux 写。以下改动只做了代码层修复，**没有在真 Windows 上运行过**。

| 原写法 | 为什么 Windows 上会坏 | 现在怎么改 |
|---|---|---|
| `shlex.split(command)`（默认 posix=True） | 把反斜杠当转义符：`"C:\Program Files\a.exe"` 会被吃成 `C:Program Filesa.exe` | `split_command(command, windows=True)` 走 `shlex(posix=False)` 并剥掉引号；`windows` 可显式传值供测试模拟 |
| `os.path.basename(argv[0])` | POSIX 版不认反斜杠，`C:\Tools\echo.exe` 整串返回，白名单永远不匹配 | `basename_any()` 同时按 `/` 与 `\` 切 |
| `os.killpg(os.getpgid(pid), SIGKILL)` | Windows 没有 `os.killpg`/`os.getpgid`，一调用就 `AttributeError` | `_force_kill_tree()`：Windows 走 `taskkill /PID x /T /F`，失败退回 `proc.kill()`；POSIX 保持整组 SIGKILL |
| `Popen(start_new_session=True)` | Windows 上无意义（会被忽略），拿不到可控进程组 | `_spawn_kwargs()`：POSIX 用 `start_new_session=True`；Windows 用 `CREATE_NEW_PROCESS_GROUP`（`getattr` 保护） |
| 默认白名单 `echo,sleep,ls,pwd,cat,...` | `echo/ls/cat` 是 cmd 内建，不是可执行文件，直接 `Popen(["echo"])` 会 `FileNotFoundError` | `default_allowlist()` 按平台给：Windows 用 `cmd, powershell, whoami, ipconfig, tasklist, where` |
| 输出一律 `decode("utf-8", "replace")` | Windows 中文系统 cmd 输出 cp936，UTF-8 强解成乱码、`replace` 吃掉中文 | `decode_output()`：`EXEC_OUTPUT_ENCODING` 显式指定 > UTF-8 严格 > 系统首选编码（cp936）兜底 |
| stdio 用默认编码/换行写 | Windows 控制台可能是 cp936，`\n` 还会被文本模式翻成 `\r\n` | `configure_stdio_utf8()` 固定 UTF-8 + LF（`StringIO` 上静默跳过） |
| `os.path.join(tempfile.gettempdir(), ...)` | 这条本身跨平台可用（Windows 得 `%TEMP%`），但路径含反斜杠时上面的 basename/拆词会连带出错 | 保留 `os.path.join`，配 `basename_any`；job 输出仍在系统临时目录 |

开关与注释（都写进代码了）：

- `Executor(windows_mode=True)` 或环境变量 `EXEC_WINDOWS_MODE=1` 可强制走 Windows 分支；不传时按 `os.name` 判断。
- `EXEC_OUTPUT_ENCODING` / `EXEC_OUTPUT_FALLBACK_ENCODING` 可覆盖编码推断。
- 凡是「只能真机确认」的点，代码注释都写了原因与替代方案，没有假装已经正确。

### Windows 上还要验什么（必须在真机执行，未验证）

| # | 要验什么 | 验证命令（Windows PowerShell/cmd） | 预期结果 |
|---|---|---|---|
| W1 | 默认白名单能执行 | `python -c "import sys;sys.path.insert(0,'prototype');import exec_server;print(exec_server.Executor(windows_mode=True).exec_command(['cmd','/c','echo','hi']))"` | `ok=True`、stdout 含 `hi`、`exit_code=0` |
| W2 | 中文输出不乱码 | 同上，命令换 `['cmd','/c','chcp 936 >nul & echo 你好']` | stdout 为可读中文「你好」，不是 `��` |
| W3 | 含空格路径拆词 | `python -c "import sys;sys.path.insert(0,'prototype');import exec_server;print(exec_server.split_command('\"C:\\Program Files\\a b.exe\" x', windows=True))"` | 两个 token，反斜杠与空格都保留 |
| W4 | 超时能终止整棵进程树 | `start_job(['ping','-n','60','127.0.0.1'])` 后立刻 `cancel_job`；再 `tasklist | findstr /i ping` | 状态 `cancelled`；`ping.exe` 无残留 |
| W5 | 长任务输出落临时目录可读 | `echo %TEMP%` 与 `query_job` 返回的 stdout 对照 | stdout 与写入内容一致；目录在 `%TEMP%` 下 |
| W6 | stdio 逐行协议与换行 | 用 PowerShell 管道发两行 JSON-RPC（initialize + tools/list） | 每行一个 JSON 回包，无乱码、无额外空行 |
| W7 | 绑定地址与曝光面 | 默认：`Get-NetTCPConnection -LocalPort 8100 -State Listen` 应为 `127.0.0.1`；显式 `EXEC_BIND_HOST=192.168.1.10` 时应为该地址，且 `/health` 的 `bind_host` 一致；再从非 Mac 设备 `curl http://192.168.1.10:8100/mcp` | 默认只听回环；绑局域网时仅 Mac IP 能连（防火墙规则生效），无 token 一律 401 |
| W8 | cancel 响应速度 | 起 `ping -n 60 127.0.0.1` 后 cancel，计时 | 2 秒内返回、状态 `cancelled` |
| W9 | Python 版本 | `python --version` | ≥ 3.10（标准库 API 需要，3.13 已在本机验证） |
| W10 | 防火墙影响 | 首次监听后看 Windows 防火墙提示；`netsh advfirewall show currentprofile` | 只绑回环时不应弹公网放行；若后续绑局域网，需显式放行规则 |

> 这份表是**待验清单**，不是验证结果；在真 Windows 跑完并记录输出前，不要说「已兼容 Windows」。

## 装到 Windows 上时怎么配（未在真 Windows 执行，属待验证）

目标场景：Windows 开发机（WLAN `192.168.1.10`）跑 HTTP 模式，Mac（`192.168.1.11`）上的 DSH 远程连它。以下命令按 PowerShell 写，**全部没有在真 Windows 上跑过**，只是待执行步骤。

```powershell
# 1) 生成 token（示例；也可用密码管理器生成后从文件读）
$TOKEN = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
$env:EXEC_TOKEN = $TOKEN

# 2) 显式绑局域网地址（不设 EXEC_BIND_HOST 时仍然是 127.0.0.1）
$env:EXEC_BIND_HOST = "192.168.1.10"
$env:EXEC_HTTP_PORT = "8100"
python prototype\exec_server.py --http
# 预期横幅：http://192.168.1.10:8100/mcp + 「本次监听在局域网地址 192.168.1.10，任何能访问该地址的人在没有 token 时会被拒（HTTP 401）」

# 3) 本机自检（不要求 token）
Invoke-RestMethod http://192.168.1.10:8100/health
# 预期：bind_host=192.168.1.10、bind_port=8100、loopback_only=False、auth_required=True

# 4) 防火墙：只放行 Mac 的 IP（Private 配置文件；默认入站拒绝，其余地址不额外放行即被拒）
New-NetFirewallRule -DisplayName "exec-server 8100 (Mac only)" -Direction Inbound -Protocol TCP `
  -LocalPort 8100 -RemoteAddress 192.168.1.11 -Action Allow -Profile Private

# 5) 从 Mac 侧验证（token 用环境变量传，不写进任何文件）
curl -sS http://192.168.1.10:8100/health
curl -sS http://192.168.1.10:8100/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

回滚（同样未在真机执行）：

```powershell
Remove-NetFirewallRule -DisplayName "exec-server 8100 (Mac only)"
Stop-Process -Name python -ErrorAction SilentlyContinue   # 用 Ctrl+C 停服务亦可
Remove-Item Env:EXEC_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:EXEC_BIND_HOST -ErrorAction SilentlyContinue
```

核对防火墙是否只对 Mac 放行：`Get-NetFirewallRule -DisplayName "exec-server 8100 (Mac only)" | Get-NetFirewallPortFilter` 与 `Get-NetFirewallRule ... | Get-NetFirewallAddressFilter`，RemoteAddress 应只有 `192.168.1.11`。若同网段其他设备也能连上，先停服务再查规则与 profile（可能规则不在 Private 配置或存在更宽的旧规则）。

> 上述步骤、命令与预期结果均属**待验证**；在真 Windows 跑通并留下输出前，不写成「已部署/已生效」。

## 设计取舍（原型级）

- **白名单按 argv[0] 的 basename 校验**，不做 shell 展开：字符串命令用 `shlex.split` 拆分后直接 `Popen`，不经过 `shell=True`，天然避开管道/分号注入。代价是 `echo hi > f` 这类 shell 语法不支持（要重定向请显式用数组 + 换工具）。
- **进程组与终止**：POSIX 用 `start_new_session=True` + 整组 `killpg`；Windows 用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F` 杀进程树（详见「Windows 兼容」）。
- **输出截断**：stdout/stderr 各按 UTF-8 字节截断，追加 `...[输出已截断：超过 N 字节阈值]`，同时返回 `truncated` 布尔标记；截断阈值内可能出现半个多字节字符，用 `errors="ignore"` 丢弃。
- **job 输出落临时文件**：`start_job` 把 stdout/stderr 直接重定向到 `$TMPDIR/exec-server-prototype-jobs/<job_id>/`，避免父进程不读管道导致任务卡死；查询时再读文件并同样截断。
- **job 元数据只在内存**：进程重启后 job 记录丢失（见「待实现」）。
- **stdio 消息**：一行一条 JSON-RPC；通知（无 `id`）不回包；未知方法返回 `-32601`，JSON 解析失败返回 `-32700` 且不退出。
- **鉴权**：`hmac.compare_digest` 常量时间比较 Bearer token；鉴权检查发生在读取请求体/解析/执行之前；401 时读掉请求体保持连接干净但不解析、不执行。
- **HttpTransportServer**：`ThreadingHTTPServer` + `daemon_threads`，便于测试里起停；`make_http_server` 强制「默认回环、显式局域网必须带 token、宽监听拒绝」的策略，并把实际绑定地址写进 `/health` 与启动横幅。

## 未做项（待实现）

以下均为「还没做」，不是已有能力：

1. **Streamable HTTP 完整规范**：未实现会话协商（`Mcp-Session-Id`）、SSE 流式响应与连接恢复；当前是「一次 POST 一次 JSON 响应」的简化版。
2. **限流 / 并发上限**：HTTP 端没有速率限制、连接数上限与请求体以外的资源配额。
3. **鉴权增强**：只有单 token Bearer；没有 token 轮换、多 token、OAuth 2.1、审计日志。
4. **Windows 兼容**：已做静态修复（拆词/终止/编码/换行/白名单，见「Windows 兼容」一节），并有 11 条本机模拟测试；但**未在真 Windows 运行**，W1–W10 待验。
5. **任务持久化**：job 元数据在内存，服务重启后 `query_job` 查不到旧任务。
6. 审计日志（谁在何时调了什么、结果码）与白名单热更新。
7. 危险命令的二次确认/红级拦截（当前只做白名单，没有分级确认）。
8. 权限降级（当前以启动进程的用户身份执行，未做独立低权限账户）。
9. HTTPS/TLS：HTTP 模式当前是明文；绑局域网时仅靠 token + 防火墙收敛风险，跨网/跨机仍必须走 Tailscale 或 TLS 反代（见 #1 §2.3/§9）。

## 安全边界（本原型遵守）

- stdio 模式不监听端口；HTTP 模式默认只绑 `127.0.0.1`，显式绑局域网时必须带 token，且 `0.0.0.0`/`::` 宽监听以退出码 2 拒绝启动（一行原因，不甩堆栈）。
- HTTP 模式下无 token/错误 token 一律 401，且不执行任何命令。
- 不访问 `~/.ssh`；不读写用户文档目录（job 输出只写系统临时目录）。
- 只执行白名单里的命令，白名单外的直接拒绝并返回明确错误。
- 不安装任何依赖、不修改系统配置与现有脚本；不部署到任何真机。
