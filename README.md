# team-workers · 远程设备控制平台

通过 WebSocket 长连接，让 AI Agent 在受控设备上执行命令、读写文件、管理设备。
包含服务端、管理台、三端设备程序、Agent Skill 与部署脚本。

## 目录结构

| 目录 | 内容 |
|---|---|
| `prototype/ws_remote/` | 服务端（Hub + HTTP API + Web 管理台）与设备端源码 |
| `skill/device-remote/` | Agent 技能：CLI 脚本 `remote.py`（13 个子命令）+ SKILL.md / README.md |
| `deploy/` | 服务端部署说明与 systemd 示例 |
| `packaging/` | 打包脚本与构建配置（发行二进制不入库，走 Releases） |

## 快速开始

### 1. 启动服务端
见 `deploy/README.md`。服务端监听 HTTPS，管理台在 `/admin.html`。

### 2. 配置 Agent Skill
```bash
cp skill/device-remote/scripts/config.local.json.example skill/device-remote/scripts/config.local.json
# 编辑 server / token
python3 skill/device-remote/scripts/remote.py list
```
详细说明见 `skill/device-remote/SKILL.md`。

### 3. 设备端
在管理台「下载中心」按系统下载设备程序，填入服务端地址（`wss://<域名>/ws`）、设备 ID 与私钥即可上线。

## 安全模型

- **控制 token**：每个用户独立，只能控制自己名下设备，跨用户返回 404。
- **设备私钥**：仅创建/重置时显示一次，不落盘、不入日志；与控制 token 是两种不同凭证。
- **命令分级**：绿（只读）直接执行；黄（写入/覆盖）先告知；红（删除/系统目录/不可逆）必须二次确认。
- **离线零静默**：设备离线一律明确回报，不伪造结果。

## 配置与凭证

所有 token、密码、邮箱授权码均通过 `.env` 或 `config.local.json` 提供，**不入库**。
模板见 `.env.example`。部署脚本中涉及的密码必须通过环境变量 `NEW_ADMIN_PASSWORD` 传入。

## 许可

待定（开源前请补充 LICENSE）。
