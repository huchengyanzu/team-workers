# team-workers 部署

服务端是纯 Python 标准库 + `websockets` 的常驻进程，推荐部署在 Linux 主机上，
前面用 Caddy / Nginx 终止 TLS 并反代到本机回环端口。仓库提供 systemd 示例
`deploy/team-workers.service`，其余步骤与具体云厂商无关。

## 1. 准备

```bash
# 目录可自定义
sudo mkdir -p /opt/team-workers
sudo cp -a ./. /opt/team-workers/
cd /opt/team-workers

python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 2. 配置环境变量

```bash
# /opt/team-workers/.env（权限 600，勿提交）
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=<强口令，至少 12 位，含大小写/数字/符号>
CONTROL_TOKEN=<随机串，可用 python3 -c "import secrets;print(secrets.token_urlsafe(32))" 生成>
RESEND_API_KEY=<可选，邮箱验证码用>
TEAM_WORKERS_EMAIL_FROM=team-workers <no-reply@notify.example.com>
TEAM_WORKERS_EMAIL_REPLY_TO=support@example.com
```

服务只监听回环：

- HTTP API / 管理台：`127.0.0.1:18766`
- WebSocket 设备长链：`127.0.0.1:18765`

## 3. 启动与自检

```bash
set -a; source .env; set +a
venv/bin/python server.py --db ./data/hub.db \
  --admin-email "$ADMIN_EMAIL" --admin-pass "$ADMIN_PASSWORD" \
  --control-token "$CONTROL_TOKEN" --cookie-secure

# 自检
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18766/login.html
```

长期运行建议交给 systemd：

```bash
sudo cp deploy/team-workers.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now team-workers.service
```

## 4. 反向代理与 TLS

把管理台与 API 反代到 `127.0.0.1:18766`，把设备 WebSocket 反代到
`127.0.0.1:18765`，并在代理层启用 TLS。设备端填写的地址必须以 `/ws` 结尾
（例如 `wss://<你的域名>/ws`）。

## 5. 回滚

- 停止并禁用服务：`sudo systemctl disable --now team-workers.service`
- 恢复反向代理配置并 reload
- 数据在 `/opt/team-workers/data/hub.db`，如需回滚版本，直接替换代码目录后重启

## 安全要点

- 服务端只绑定回环，公网入口统一走反代。
- `ADMIN_PASSWORD` 与控制 token 分离，均只放在 `.env`（600），不入库。
- 首次登录后建议在管理台立即修改管理员口令；怀疑泄露时轮换控制 token。
- 设备私钥只在创建/重置时显示一次，不落盘、不入日志。
