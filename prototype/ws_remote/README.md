# WebSocket 远程控制（已部署到云端）

服务端（Linux 云机）+ 设备端（Win/Mac/Linux）+ 控制端 CLI/skill。设备启动即上线，服务端转发指令、设备执行后回传结果。

> **已部署**：云端 `203.0.113.10`，systemd 服务 `team-workers.service`，公网入口 `https://hub.example.com/`，设备 WS `wss://hub.example.com/ws`。管理员账号「admin」（用户名支持中文）。公网端到端已验证（设备 wss 上线 + skill 下发命令回传 + 审计）。部署详情与回滚见 `../../deploy/README.md`。通用 agent skill 在 `../../skill/device-remote/`。

> 本目录是**可运行的原型**：链路与安全机制已在本机验证，但未上真机/公网长期运行。
> 设计文档：`docs/design/websocket-remote-skill-design.md`

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | 服务端：WS 中心 + REST API + Web 管理台 + 登录/防暴破 + SQLite + 按月审计 |
| `security.py` | 安全模块：PBKDF2 口令 / SVG 验证码 / 会话 / 防暴破 / 按月分表审计 |
| `device.py` | 设备端：WS 客户端 + 本地 Web 配置页 + 复用现有 4 工具执行内核 |
| `device_web.py` | 设备端本地配置服务（仅回环、免登录、防 CSRF） |
| `client.py` | 控制端 CLI（skill 脚本雏形） |
| `test_proto.py` | 协议层单测 9 条 |
| `test_security.py` | 安全单测 10 条 |
| `ui/` | 三套页面：登录页 / 管理台 / 设备配置页 + 共用主题 |

## 快速启动（本机）

```bash
cd prototype/ws_remote
python3 server.py --host 127.0.0.1 --ws-port 18765 --http-port 18766 \
  --db ./hub.db --admin-user admin --admin-pass '<强口令>' --control-token '<token>'
```

- 管理台：http://127.0.0.1:18766/login.html （账号密码 + 图形验证码）
- 控制端 API 凭证：`X-Control-Token: <token>`（给 agent skill 用，无需交互登录）
- 首次初始化若未传 `--admin-pass`，会生成随机口令并**只打印一次**；之后保持不变。
- 重置口令：`python3 server.py --db ./hub.db --reset-admin-pass '<新口令>'`

设备端：

```bash
python3 device.py --config ./device.json
# 本地配置页 http://127.0.0.1:18770/ （仅回环，免登录）
# 填服务端地址 + device_id + 私钥 → 保存即自动连接
```

也可用配置文件直接启动（无需 Web）：`device.json` 含 `server_url/device_id/key/allowlist`。

## 协议

WebSocket（设备 ↔ 服务端）：
- 设备→服务端：`auth{device_id,key,platform,info}`（首帧）、`heartbeat{info}`（20s）、`result{task_id,...}`
- 服务端→设备：`auth_ack{ok}`、`command{task_id,tool,arguments}`
- 认证失败服务端回 `auth_ack{ok:false,reason}` 并断开；设备不重试（避免刷屏）

REST API（控制端 ↔ 服务端，头 `X-Control-Token`；或浏览器会话 Cookie）：
- `GET /api/devices`　设备列表与状态
- `POST /api/admin/devices`　创建设备（返回一次性私钥）
- `POST /api/admin/devices/{id}/enabled`　禁用/启用；`DELETE /api/admin/devices/{id}` 删除
- `POST /api/admin/devices/{id}/key`　重置私钥
- `POST /api/tasks {device_id,tool,arguments}`　下发任务（15s 同步等待，超时返回 task_id）
- `GET /api/tasks/{id}`　查任务结果
- `GET /api/overview`　概览；`GET /api/logs?month=YYYYMM&actor=&action=&result=&page=`　按月审计

## 安全设计（公网前提）

| 机制 | 实现 |
|---|---|
| 登录 | 账号 + 密码 + 一次性 SVG 图形验证码；口令 PBKDF2-HMAC-SHA256 加盐、常量时间比对 |
| 防暴破 | 按 (IP,账号) 与 IP 双维度失败计数，5 次起指数退避锁定（15min→30min…上限 24h），验证码先行防绕过 |
| 会话 | `HttpOnly` + `SameSite=Strict`，8h 绝对 / 30min 闲置超时；公网 HTTPS 反代后加 `--cookie-secure` 开启 `Secure` |
| SQL 注入 | 全部参数化查询；动态表名只来自内部正则校验后的 `YYYYMM`；注入串仅作普通字符串参数 |
| XSS | 页面用 `textContent` 写入，不 `innerHTML` 拼接不可信数据；响应带 `X-Content-Type-Options: nosniff` |
| 设备配置页 | 仅绑 `127.0.0.1`，公网不可达；POST 需 `X-Local-Config: 1` + Origin 校验防 CSRF；私钥只回显掩码 |
| 审计 | `logs_YYYYMM` 按月分表，记录登录/设备变更/指令下发，可按月/操作人/动作/结果筛选，结果值 `ok` 前端显示为成功 |

**部署注意**：公网必须走 HTTPS 反代（Caddy 已有域名可复用），服务端只监听内网端口，反代层传 `X-Real-IP` 供来源锁定；不要把 WS/HTTP 裸端口直接映射到公网。TLS 在反代终止，服务端与设备端之间建议也用 `wss://`。

## 验证结果（本机，2026-11-20）

| # | 场景 | 结果 |
|---|---|---|
| 1 | 服务端 HTTP API / 创建设备 / 一次性私钥 | ✅ |
| 2 | 两台设备启动即上线，Web 列表可见平台/info | ✅ |
| 3 | `echo hello` 下发→执行→回传 `stdout="hello\n"`,`exit_code=0` | ✅ |
| 4 | `pwd` 跨设备执行回传工作目录 | ✅ |
| 5 | 非白名单命令（`rm`）被拒，返回明确错误文本，服务端不崩 | ✅ |
| 6 | 不存在的设备 → HTTP 409「设备离线」明确回执 | ✅ |
| 7 | 错误控制 token → 401 拒绝 | ✅ |
| 8 | 停止设备 → 状态转 offline；重启 → 自动重连上线 | ✅ |
| 9 | 错误私钥 → auth 失败拒绝连接，不干扰在线设备 | ✅ |
| 10 | 未登录访问管理 API → 401；根路径返回登录页 | ✅ |
| 11 | 验证码一次性消费，重放被拒 | ✅ |
| 12 | 正确登录拿会话 Cookie，会话可访问设备管理 | ✅ |
| 13 | 连续 5 次错误密码 → 锁定 900 秒（指数退避），第 6 次拒绝 | ✅ |
| 14 | IP 维度连带锁定（换用户名也被限） | ✅ |
| 15 | SQL 注入串 `' OR 1=1 --` 无效（参数化 + 锁定） | ✅ |
| 16 | 审计日志落 `logs_202611` 按月表，success 筛选映射正确 | ✅ |
| 17 | 设备端 Web 配置页：无 CSRF 头 POST → 403；写配置后自动上线 | ✅ |
| 18 | 私钥回显掩码，不通过 GET 下发完整值 | ✅ |

单测：`python3 -m unittest test_proto` → 9 条全绿；`python3 -m unittest test_security` → 10 条全绿。

## 边界

- 明文链路：本机原型用 `ws://` + `http://`，上云由 Caddy 加 `wss`/`https`。TLS 在反代终止，服务端视角可见明文（B1 中心 hub 的已知权衡）。
- 控制端通用 agent skill 尚未写（本目录 `client.py` 是雏形）。
- 文件工具、红级二次确认、任务持久化、断线期间指令排队均未接（按设计列二期）。
- 未上真 Windows、未上云部署；W1–W10 真机验收待做。
- `client.py` 的 `exec` 若命令参数以 `-` 开头，argparse 会误判为选项；用 `--` 分隔或不带 `-` 的命令。
- 端口 18765/18766/18770 是本机测试端口；上云按设计用 Caddy 反代 + 独立端口（避开云端已占用的 22/80/3306/8443）。
