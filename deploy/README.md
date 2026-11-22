# team-workers 部署包

服务端已部署到云端 `203.0.113.10`，systemd 服务 `nexus-remote.service`，公网入口 `https://hub.example.com/remote/`。

## 部署结果（2026-11-20 实测）

| 项 | 值 |
|---|---|
| 公网入口 | `https://hub.example.com/remote/`（管理台/API） |
| 设备 WS | `wss://hub.example.com/remote/ws` |
| 云端目录 | `/opt/nexus-remote`（独立，不动任何现有业务） |
| 内部端口 | HTTP `127.0.0.1:18766`、WS `127.0.0.1:18765`（仅回环，不对外） |
| systemd | `nexus-remote.service`，active + enabled，开机自启 |
| 数据库 | `/opt/nexus-remote/data/hub.db`（含 logs_YYYYMM 按月表） |
| 凭证 | `/opt/nexus-remote/.env`（0700，管理员「admin」口令 + 控制 token，不在仓库明文保存）。用户名支持中文（已修 `compare_digest` 仅 ASCII 的限制）。 |
| Caddy | `/opt/mycli-edge/Caddyfile` 内新增 `Nexus Remote` 段（备份 `.bak.*`），reload 不中断现有业务 |
| 资源占用 | 内存 ~18MB（2C2G 云机无压力） |

## 文件

| 文件 | 用途 |
|---|---|
| `cloud_stage1.sh` | 阶段1：部署服务端本体（独立目录 + venv + systemd + 本机回环自检） |
| `cloud_stage2_caddy.sh` | 阶段2：Caddy 加 `/remote` 反代（备份 + adapt 校验 + 失败自动回滚 + 公网自检） |
| `cloud_verify.sh` / `cloud_fix_and_verify.sh` / `cloud_cleanup_verify.sh` | 云端端到端验证与清理 |
| `nexus-remote.service` | systemd unit |
| `caddy-snippet.txt` | Caddy 反代段（供人工核对） |
| `rollback.sh` | 回滚脚本（停服务 + 恢复 Caddyfile + reload） |

## 部署流程（通过 windows-dev 的 ssh_run.py / ssh_put.py）

```bash
# 本地打包（含全部 py + ui + exec_server.py 执行内核 + .env）
tar -czf /tmp/nexus-remote.tar.gz -C /tmp nexus-pkg

# 传到 windows-dev → 云端
scp /tmp/nexus-remote.tar.gz user@192.168.1.10:C:\\Users\\user\\nexus-remote.tar.gz
# windows-dev 上：python ssh_put.py <本地> /tmp/nexus-remote.tar.gz

# 云端执行（ssh_run.py 上传脚本后执行）
# 1) cloud_stage1.sh  2) cloud_stage2_caddy.sh  3) cloud_verify.sh
```

注意：设备端 `device.py` 依赖 `prototype/exec_server.py`（执行内核），**打包时必须包含**，否则云端设备端起不来（本次踩过此坑，已在 packaging 修正）。

## 回滚

在云端执行 `deploy/rollback.sh`：停止并禁用服务、用最近备份恢复 Caddyfile、reload。只移除新增内容，不影响现有 frp/OpenIM/班宠星球业务。

## 安全

- 服务端仅监听回环，公网入口由 Caddy 8443 反代；TLS 由现成 `hub.example.com` 证书提供，`--cookie-secure` 已开。
- 控制 token 供 skill/CLI 用，管理员口令供 Web 登录用，二者分离，均只在 `.env`（0700）。
- 本机（Mac）出口对 8443 的直连被网络环境 reset，端到端验证在云端容器内完成（wss 长链 + skill 闭环全过）；这不影响目标用户经正常网络访问。
