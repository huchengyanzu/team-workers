#!/usr/bin/env python3
"""WebSocket 远程控制服务端（公网加固版原型）。

职责：
  1) WebSocket 中心：设备长链注册、心跳、结果回传、离线检测
  2) REST API：控制端列设备 / 下发任务 / 查任务结果（X-Control-Token 兼容 agent skill）
  3) Web 管理台：账号密码 + 图形验证码 + 防暴破登录；设备管理 / 用户管理 / 概览 / 操作日志
  4) SQLite：users / devices / tasks / logs_YYYYMM（按月分表）

多用户权限：
  - 用户存 users 表（admin/user 两种角色），每个用户独立 control_token
  - 设备与任务带 owner_id；所有 API 按 owner 隔离，用户只能看/控自己的设备
  - 旧单管理员（settings 表）首次启动自动迁移为 users 表的 admin，现有设备归其所有

公网安全口径：
  - 口令 PBKDF2-HMAC-SHA256 加盐，常量时间比对；验证码一次性、内存限容量
  - 登录失败按 (IP,账号) 与 IP 双维度指数退避锁定；会话 HttpOnly+SameSite+可选 Secure
  - 所有 SQL 参数化；动态表名仅来自内部正则校验后的 YYYYMM
  - 操作日志按月分表，记录登录/设备变更/指令下发/配置变更

运行：
  python3 server.py --host 0.0.0.0 --ws-port 8765 --http-port 8766 --db ./hub.db \
      --admin-user admin --admin-pass '<强口令>' --cookie-secure
  # 重置管理员口令：python3 server.py --db ./hub.db --reset-admin-pass '<新口令>'

设计边界见 docs/design/websocket-remote-skill-design.md。
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from websockets.asyncio.server import serve as ws_serve

from security import (
    AuditLogger, CaptchaStore, LoginGuard, PasswordHasher, SessionStore,
)
import user_store as US
import mailer

HEARTBEAT_TIMEOUT = 90  # 秒：超过此值无心跳视为离线
TASK_TIMEOUT = 15       # 秒：同步等待任务结果的超时
MAX_BODY = 256 * 1024   # HTTP 请求体上限（防内存耗尽）
THIS_DIR = Path(__file__).resolve().parent
UI_DIR = THIS_DIR / "ui"

HTTP_REASON = {
    200: "OK", 302: "Found", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    409: "Conflict", 429: "Too Many Requests", 500: "Internal Server Error",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return prefix + "-" + secrets.token_hex(6)


def hash_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def client_ip(headers: dict) -> str:
    # 只信直连对端；X-Forwarded-For 仅在受信任反代后由 Caddy 覆盖后才可信，
    # 原型阶段不解析该头，避免伪造绕过来源锁定。
    return headers.get("x-real-ip", "")[:64] or "-"


def parse_cookies(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


# --------------------------------------------------------------------------
# HTTP 响应
# --------------------------------------------------------------------------
@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    ctype: str = "application/json; charset=utf-8"
    headers: dict = field(default_factory=dict)


def json_resp(status: int, obj, headers: dict | None = None) -> Response:
    return Response(
        status,
        json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        "application/json; charset=utf-8",
        headers or {},
    )


def text_resp(status: int, text: str, ctype: str, headers: dict | None = None) -> Response:
    return Response(status, text.encode("utf-8"), ctype, headers or {})


def redirect(path: str, headers: dict | None = None) -> Response:
    h = {"Location": path}
    h.update(headers or {})
    return Response(302, b"", "text/plain; charset=utf-8", h)


def cookie_header(name: str, value: str, secure: bool, max_age: int | None = None) -> str:
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# 管理员账号（落 settings 表，重启后保持）
# --------------------------------------------------------------------------
def ensure_admin(db_path: str, user: str, password: str | None) -> str | None:
    """首次初始化管理员。返回生成随机口令（仅当未提供时），否则 None。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    generated = None
    with sqlite3.connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = c.execute("SELECT value FROM settings WHERE key='admin_user'").fetchone()
        if row:
            return None  # 已初始化，不覆盖主人设置的口令
        if password is None:
            password = secrets.token_urlsafe(14)
            generated = password
        salt, digest = PasswordHasher.make(password)
        c.executemany(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            [("admin_user", user), ("admin_salt", salt), ("admin_hash", digest)],
        )
    return generated


def get_admin(db_path: str) -> tuple[str, str, str]:
    with sqlite3.connect(db_path) as c:
        rows = dict(c.execute(
            "SELECT key,value FROM settings WHERE key IN ('admin_user','admin_salt','admin_hash')"
        ).fetchall())
    return rows.get("admin_user", "admin"), rows.get("admin_salt", ""), rows.get("admin_hash", "")


def check_password_strength(password: str) -> str:
    """返回空串表示合格，否则返回中文错误说明。12 位起，大小写/数字/特殊符号齐全。"""
    if len(password) < 12:
        return "密码至少 12 位"
    checks = (
        (any(c.islower() for c in password), "小写字母"),
        (any(c.isupper() for c in password), "大写字母"),
        (any(c.isdigit() for c in password), "数字"),
        (any(not c.isalnum() for c in password), "特殊符号"),
    )
    missing = [name for ok, name in checks if not ok]
    if missing:
        return "密码需包含：" + "、".join(missing)
    return ""


def reset_admin_password(db_path: str, new_password: str) -> None:
    salt, digest = PasswordHasher.make(new_password)
    with sqlite3.connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("UPDATE settings SET value=? WHERE key='admin_salt'", (salt,))
        c.execute("UPDATE settings SET value=? WHERE key='admin_hash'", (digest,))


# --------------------------------------------------------------------------
# 设备连接
# --------------------------------------------------------------------------
@dataclass
class DeviceConn:
    device_id: str
    ws: object
    platform: str = ""
    info: dict | None = None
    last_seen: float = 0.0


class Hub:
    def __init__(self, db_path: str, control_token: str):
        self.db_path = db_path
        self.control_token = control_token
        self.conns: dict[str, DeviceConn] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.audit = AuditLogger(db_path)
        self._init_db()

    # ---------- 存储 ----------
    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # 确保 users 表与 devices/tasks.owner_id 列存在（多用户模型）
        US.init_schema(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    key_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'offline',
                    info_json TEXT NOT NULL DEFAULT '{}',
                    last_seen TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    owner_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    owner_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
                """
            )

    def _q(self, sql: str, args=()):
        with sqlite3.connect(self.db_path) as c:
            return c.execute(sql, args).fetchall()

    def _x(self, sql: str, args=()):
        with sqlite3.connect(self.db_path) as c:
            c.execute(sql, args)

    # ---------- 设备（按 owner 隔离） ----------
    def create_device(self, display_name: str, platform: str, owner_id: int) -> tuple[str, str]:
        device_id = new_id("dev")
        key = secrets.token_urlsafe(32)
        self._x(
            "INSERT INTO devices(device_id,display_name,platform,key_hash,status,last_seen,created_at,owner_id) "
            "VALUES(?,?,?,?,'offline',?,?,?)",
            (device_id, display_name[:128], platform[:64], hash_key(key),
             now_iso(), now_iso(), owner_id),
        )
        return device_id, key

    def delete_device(self, device_id: str, owner_id: int) -> bool:
        rows = self._q("SELECT 1 FROM devices WHERE device_id=? AND owner_id=?", (device_id, owner_id))
        if not rows:
            return False
        self._x("DELETE FROM devices WHERE device_id=? AND owner_id=?", (device_id, owner_id))
        self._x("DELETE FROM tasks WHERE device_id=? AND owner_id=?", (device_id, owner_id))
        if device_id in self.conns:
            asyncio.create_task(self.conns[device_id].ws.close(code=4003, reason="deleted"))
        return True

    def device_by_key(self, device_id: str, key: str) -> dict | None:
        rows = self._q(
            "SELECT device_id,display_name,platform,enabled FROM devices WHERE device_id=? AND key_hash=?",
            (device_id, hash_key(key)),
        )
        if not rows or not rows[0][3]:
            return None
        return {"device_id": rows[0][0], "display_name": rows[0][1], "platform": rows[0][2]}

    def list_devices(self, owner_id: int) -> list[dict]:
        rows = self._q(
            "SELECT device_id,display_name,platform,status,info_json,last_seen,enabled "
            "FROM devices WHERE owner_id=? ORDER BY created_at", (owner_id,)
        )
        out = []
        for r in rows:
            device_id, name, platform, _status, info_json, last_seen, enabled = r
            conn = self.conns.get(device_id)
            live = bool(conn) and (time.time() - conn.last_seen <= HEARTBEAT_TIMEOUT)
            out.append({
                "device_id": device_id,
                "display_name": name,
                "platform": platform,
                "status": "online" if live and enabled else "offline",
                "info": json.loads(info_json or "{}"),
                "last_seen": last_seen,
                "enabled": bool(enabled),
            })
        return out

    def overview(self, owner_id: int) -> dict:
        devices = self.list_devices(owner_id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tasks = self._q(
            "SELECT COUNT(*) FROM tasks WHERE owner_id=? AND created_at LIKE ?",
            (owner_id, today + "%"),
        )[0][0]
        return {
            "devices_total": len(devices),
            "devices_online": sum(1 for d in devices if d["status"] == "online"),
            "tasks_today": tasks,
            "last_logs": self.audit.query(page_size=8)["rows"],
        }

    def update_device_state(self, device_id: str, status: str, info: dict | None) -> None:
        info_json = json.dumps(info or {}, ensure_ascii=False)
        self._x(
            "UPDATE devices SET status=?, info_json=?, last_seen=? WHERE device_id=?",
            (status, info_json, now_iso(), device_id),
        )

    def set_device_enabled(self, device_id: str, enabled: bool, owner_id: int) -> bool:
        rows = self._q("SELECT 1 FROM devices WHERE device_id=? AND owner_id=?", (device_id, owner_id))
        if not rows:
            return False
        self._x("UPDATE devices SET enabled=? WHERE device_id=? AND owner_id=?",
                (1 if enabled else 0, device_id, owner_id))
        if not enabled and device_id in self.conns:
            asyncio.create_task(self.conns[device_id].ws.close(code=4001, reason="disabled"))
        return True

    def reset_device_key(self, device_id: str, owner_id: int) -> str | None:
        if not self._q("SELECT 1 FROM devices WHERE device_id=? AND owner_id=?", (device_id, owner_id)):
            return None
        key = secrets.token_urlsafe(32)
        self._x("UPDATE devices SET key_hash=? WHERE device_id=? AND owner_id=?",
                (hash_key(key), device_id, owner_id))
        if device_id in self.conns:
            asyncio.create_task(self.conns[device_id].ws.close(code=4002, reason="key reset"))
        return key

    def rename_device(self, device_id: str, new_name: str, owner_id: int) -> tuple[bool, str]:
        rows = self._q(
            "SELECT display_name FROM devices WHERE device_id=? AND owner_id=?",
            (device_id, owner_id),
        )
        if not rows:
            return False, ""
        old_name = rows[0][0]
        with sqlite3.connect(self.db_path) as c:
            cur = c.execute(
                "UPDATE devices SET display_name=? WHERE device_id=? AND owner_id=?",
                (new_name, device_id, owner_id),
            )
            if cur.rowcount != 1:
                return False, ""
        return True, old_name

    # ---------- 任务 ----------
    def create_task(self, device_id: str, tool: str, arguments: dict, owner_id: int) -> str:
        task_id = new_id("task")
        self._x(
            "INSERT INTO tasks(task_id,device_id,tool,args_json,state,created_at,owner_id) "
            "VALUES(?,?,?,?,'queued',?,?)",
            (task_id, device_id, tool, json.dumps(arguments, ensure_ascii=False), now_iso(), owner_id),
        )
        return task_id

    def set_task_state(self, task_id: str, state: str, result: dict | None = None, error: str = "") -> None:
        result_json = json.dumps(result, ensure_ascii=False) if result is not None else ""
        self._x(
            "UPDATE tasks SET state=?, result_json=?, error=?, finished_at=? WHERE task_id=?",
            (state, result_json, error, now_iso(), task_id),
        )

    def get_task(self, task_id: str) -> dict | None:
        rows = self._q(
            "SELECT task_id,device_id,tool,state,result_json,error FROM tasks WHERE task_id=?", (task_id,)
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "task_id": r[0], "device_id": r[1], "tool": r[2], "state": r[3],
            "result": json.loads(r[4]) if r[4] else None, "error": r[5],
        }

    # ---------- WebSocket 设备通道 ----------
    async def device_handler(self, ws) -> None:
        device_id = None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            auth = json.loads(raw)
            if auth.get("type") != "auth":
                await ws.send(json.dumps({"type": "auth_ack", "ok": False, "reason": "first frame must be auth"}))
                return
            device = self.device_by_key(auth.get("device_id", ""), auth.get("key", ""))
            if not device:
                await ws.send(json.dumps({"type": "auth_ack", "ok": False, "reason": "invalid device or key"}))
                return
            device_id = device["device_id"]
            platform = auth.get("platform", device["platform"] or "")
            info = auth.get("info") or {}
            self.conns[device_id] = DeviceConn(
                device_id=device_id, ws=ws, platform=platform,
                info=info, last_seen=time.time(),
            )
            self.update_device_state(device_id, "online", info)
            self.audit.log("device_online", actor=device_id, ip="-", target=device_id,
                           detail="platform=%s" % platform)
            await ws.send(json.dumps({"type": "auth_ack", "ok": True, "device_id": device_id}))

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "heartbeat":
                    conn = self.conns.get(device_id)
                    if conn:
                        conn.info = msg.get("info") or conn.info
                        conn.last_seen = time.time()
                    self.update_device_state(device_id, "online", msg.get("info") or {})
                elif msg.get("type") == "result":
                    task_id = msg.get("task_id")
                    if not task_id:
                        continue
                    result = {k: v for k, v in msg.items() if k not in ("type", "task_id")}
                    ok = result.get("ok", True)
                    state = "done" if ok else "failed"
                    self.set_task_state(task_id, state, result=result)
                    fut = self.pending.get(task_id)
                    if fut and not fut.done():
                        fut.set_result(result)
        except Exception:
            pass
        finally:
            if device_id and device_id in self.conns:
                del self.conns[device_id]
                self.update_device_state(device_id, "offline", None)
                self.audit.log("device_offline", actor=device_id, ip="-", target=device_id)

    # ---------- 指令下发 ----------
    async def dispatch_task(self, device_id: str, tool: str, arguments: dict,
                            owner_id: int) -> tuple[int, dict]:
        # 越权保护：设备不归调用者 → 当作不存在（不泄露设备是否属于他人）
        if not US.owns_device(self.db_path, owner_id, device_id):
            return 404, {"error": "设备不存在：%s" % device_id}
        conn = self.conns.get(device_id)
        if not conn or time.time() - conn.last_seen > HEARTBEAT_TIMEOUT:
            return 409, {"error": "设备离线：%s" % device_id}
        task_id = self.create_task(device_id, tool, arguments, owner_id)
        fut = asyncio.get_running_loop().create_future()
        self.pending[task_id] = fut
        await conn.ws.send(json.dumps({
            "type": "command", "task_id": task_id, "tool": tool, "arguments": arguments,
        }, ensure_ascii=False))
        self.set_task_state(task_id, "dispatched")
        try:
            result = await asyncio.wait_for(fut, timeout=TASK_TIMEOUT)
            return 200, {"task_id": task_id, "state": "done", "result": result}
        except asyncio.TimeoutError:
            self.set_task_state(task_id, "timeout")
            return 200, {"task_id": task_id, "state": "timeout",
                         "hint": "用 GET /api/tasks/%s 查询结果（长任务）" % task_id}
        finally:
            self.pending.pop(task_id, None)


# --------------------------------------------------------------------------
# HTTP API（含登录）
# --------------------------------------------------------------------------
class HttpApi:
    TOOLS = ("exec", "start_job", "query_job", "cancel_job",
             "file_read", "file_write", "file_list")

    def __init__(self, hub: Hub, db_path: str, control_token: str,
                 secure_cookie: bool = False):
        self.hub = hub
        self.db_path = db_path
        self.control_token = control_token
        self.secure_cookie = secure_cookie
        self.captcha = CaptchaStore()
        self.sessions = SessionStore()
        self.guard = LoginGuard()

    # ---------- 鉴权 ----------
    def _token_user(self, headers: dict) -> dict | None:
        """控制端 token → 用户。每个用户一个独立 token，天然隔离。"""
        u = US.get_by_token(self.db_path, headers.get("x-control-token", ""))
        return u if (u and u["enabled"]) else None

    def _session_user(self, cookies: dict) -> dict | None:
        s = self.sessions.get(cookies.get("session", ""))
        if not s:
            return None
        u = US.get_by_username(self.db_path, s["username"])
        return u if (u and u["enabled"]) else None

    @staticmethod
    def _actor(user: dict) -> str:
        return user["username"]

    def _require_admin(self, user: dict) -> bool:
        return user["role"] == "admin"

    def _static(self, name: str) -> Response:
        allowed = {"login.html", "admin.html", "theme.css"}
        if name not in allowed:
            return json_resp(404, {"error": "not found"})
        path = UI_DIR / name
        if not path.is_file():
            return json_resp(500, {"error": "ui asset missing: " + name})
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else "text/css; charset=utf-8"
        return Response(200, path.read_bytes(), ctype,
                        {"Cache-Control": "no-store"})

    # 分发包下载：白名单固定文件名，防目录穿越；二进制用 application/octet-stream
    _DOWNLOADS = {
        "nexus-device-win.exe", "nexus-device-mac", "nexus-device-linux",
        "Nexus-Remote-Device.app.zip", "device-remote.zip", "windows-setup.ps1",
    }

    def _download(self, name: str) -> Response:
        # 只取最后一段文件名，且必须在白名单内（杜绝 ../ 穿越）
        name = Path(name).name
        if name not in self._DOWNLOADS:
            return json_resp(404, {"error": "not found"})
        path = UI_DIR / "downloads" / name
        if not path.is_file():
            return json_resp(404, {"error": "package not found"})
        if name.endswith(".ps1"):
            ctype = "text/plain; charset=utf-8"
        elif name.endswith(".zip"):
            ctype = "application/zip"
        else:
            ctype = "application/octet-stream"
        size = path.stat().st_size
        return Response(200, path.read_bytes(), ctype, {
            "Content-Disposition": 'attachment; filename="%s"' % name,
            "Content-Length": str(size),
            "Cache-Control": "public, max-age=300",
        })

    # ---------- 请求入口 ----------
    async def handle(self, reader, writer) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), timeout=10)).decode("utf-8", "replace").strip()
            if not line:
                return
            parts = line.split()
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]
            headers: dict[str, str] = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                if b":" in h:
                    k, v = h.decode("utf-8", "replace").split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0)
            if length < 0 or length > MAX_BODY:
                resp = json_resp(413, {"error": "body too large"})
            else:
                body_bytes = await reader.readexactly(length) if length else b""
                try:
                    body = json.loads(body_bytes) if body_bytes else {}
                except json.JSONDecodeError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                u = urlparse(target)
                resp = await self.route(method, u.path, parse_qs(u.query), body,
                                        headers, parse_cookies(headers.get("cookie", "")))
            self._write(writer, resp)
        except Exception as exc:
            self._write(writer, json_resp(500, {"error": str(exc)}))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _write(self, writer, resp: Response) -> None:
        reason = HTTP_REASON.get(resp.status, "OK")
        head = (
            f"HTTP/1.1 {resp.status} {reason}\r\n"
            f"Content-Type: {resp.ctype}\r\n"
            f"Content-Length: {len(resp.body)}\r\n"
            "Connection: close\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\n"
        )
        for k, v in resp.headers.items():
            head += f"{k}: {v}\r\n"
        writer.write(head.encode("utf-8") + b"\r\n" + resp.body)

    # ---------- 路由 ----------
    async def route(self, method: str, path: str, query: dict, body: dict,
                    headers: dict, cookies: dict) -> Response:
        ip = client_ip(headers)

        # ===== 公开资源（免登录）=====
        if method == "GET" and path in ("/", "/login.html", "/theme.css"):
            return self._static("login.html" if path == "/" else path.lstrip("/"))
        if method in ("GET", "HEAD") and path.startswith("/downloads/"):
            return self._download(path[len("/downloads/"):])
        if method == "GET" and path == "/api/captcha.svg":
            cid, svg = self.captcha.issue()
            return text_resp(200, svg, "image/svg+xml; charset=utf-8", {
                "Cache-Control": "no-store",
                "Set-Cookie": cookie_header("captcha_id", cid, self.secure_cookie, 180),
            })
        if method == "POST" and path == "/api/login":
            return await self._login(body, ip, cookies)

        # ===== 邮箱自助注册（免登录）=====
        if method == "POST" and path == "/api/register/send-code":
            email = str(body.get("email", "")).strip().lower()
            code, err = US.issue_email_code(self.db_path, email, "register")
            if err:
                # 不泄露邮箱是否已注册等细节差异最小化；此处错误文案本身不含敏感信息
                self.hub.audit.log("register_code_fail", actor=email or "-", ip=ip,
                                   detail=err, result="fail")
                return json_resp(400, {"ok": False, "error": err})
            if not mailer.smtp_configured():
                self.hub.audit.log("register_mail_unconfigured", actor=email or "-",
                                   ip=ip, result="fail")
                return json_resp(503, {"ok": False,
                                       "error": "服务端邮件未配置，请联系管理员开通注册"})
            sent = await asyncio.get_running_loop().run_in_executor(
                None, mailer.send_email, email,
                "team-workers 注册验证码",
                "您好，\n\n您正在注册 team-workers，验证码为：%s\n\n"
                "验证码 5 分钟内有效，请尽快填写。若非本人操作请忽略此邮件。" % code,
            )
            if not sent:
                self.hub.audit.log("register_mail_fail", actor=email, ip=ip, result="fail")
                return json_resp(502, {"ok": False, "error": "验证码邮件发送失败，请稍后重试"})
            self.hub.audit.log("register_code_sent", actor=email, ip=ip, result="ok")
            return json_resp(200, {"ok": True, "message": "验证码已发送到邮箱"})

        if method == "POST" and path == "/api/register/verify":
            email = str(body.get("email", "")).strip().lower()
            code = str(body.get("code", "")).strip()
            password = str(body.get("password", ""))
            strength = check_password_strength(password)
            if strength:
                return json_resp(400, {"ok": False, "error": strength})
            user, err = US.register_with_email(
                self.db_path, email, code, password, role="user"
            )
            if err:
                self.hub.audit.log("register_fail", actor=email or "-",
                                   ip=ip, detail=err, result="fail")
                return json_resp(400, {"ok": False, "error": err})
            self.hub.audit.log("register_ok", actor=email, ip=ip, result="ok")
            # token 仅此一次返回，用户需保存；不写入任何日志
            return json_resp(200, {
                "ok": True, "email": email,
                "control_token": user["control_token"],
                "message": "注册成功，请保存你的控制 token（仅显示一次）",
            })

        # ===== 需鉴权 =====
        session = self.sessions.get(cookies.get("session", ""))
        user = self._session_user(cookies) or self._token_user(headers)
        if not user:
            if path.endswith(".html"):
                return redirect("/login.html")
            return json_resp(401, {"error": "未登录或凭证无效"})
        actor = self._actor(user)
        src_ip = session["ip"] if session else ip
        owner_id = user["id"]

        if method == "POST" and path == "/api/logout" and session:
            self.sessions.destroy(cookies.get("session", ""))
            self.hub.audit.log("logout", actor=actor, ip=src_ip)
            return json_resp(200, {"ok": True}, {
                "Set-Cookie": cookie_header("session", "", self.secure_cookie, 0),
            })
        if method == "POST" and path == "/api/change-password":
            # 改密只允许会话登录（token 调用方不能改口令）
            if not session:
                return json_resp(403, {"ok": False, "error": "请用管理台登录后修改密码"})
            old_pwd = str(body.get("old_password", ""))
            new_pwd = str(body.get("new_password", ""))
            if not PasswordHasher.verify(old_pwd, user["salt"], user["password_hash"]):
                self.hub.audit.log("password_change", actor=actor, ip=src_ip,
                                   detail="旧密码错误", result="fail")
                return json_resp(401, {"ok": False, "error": "当前密码不正确"})
            strength = check_password_strength(new_pwd)
            if strength:
                return json_resp(400, {"ok": False, "error": strength})
            US.set_password(self.db_path, owner_id, new_pwd)
            # 改密后销毁当前会话，强制用新密码重新登录
            self.sessions.destroy(cookies.get("session", ""))
            self.hub.audit.log("password_change", actor=actor, ip=src_ip, result="ok")
            return json_resp(200, {"ok": True, "message": "密码已修改，请用新密码重新登录"}, {
                "Set-Cookie": cookie_header("session", "", self.secure_cookie, 0),
            })
        if method == "GET" and path == "/api/session":
            return json_resp(200, {"username": actor, "role": user["role"],
                                   "via": "session" if session else "token"})
        if method == "GET" and path == "/admin.html":
            return self._static("admin.html")

        if method == "GET" and path == "/api/overview":
            return json_resp(200, self.hub.overview(owner_id))
        if method == "GET" and path == "/api/devices":
            return json_resp(200, {"devices": self.hub.list_devices(owner_id)})
        if method == "POST" and path == "/api/admin/devices":
            name = str(body.get("display_name", "未命名"))[:128]
            plat = str(body.get("platform", ""))[:64]
            device_id, key = self.hub.create_device(name, plat, owner_id)
            self.hub.audit.log("device_create", actor=actor, ip=src_ip, target=device_id,
                               detail="name=%s platform=%s" % (name, plat))
            return json_resp(200, {"device_id": device_id, "key": key})
        if path.startswith("/api/admin/devices/") and method == "POST":
            seg = path.split("/")
            if len(seg) < 5:
                return json_resp(404, {"error": "not found"})
            device_id, op = seg[4], seg[-1]
            if op == "enabled":
                enabled = bool(body.get("enabled"))
                ok = self.hub.set_device_enabled(device_id, enabled, owner_id)
                if ok:
                    self.hub.audit.log("device_enable" if enabled else "device_disable",
                                       actor=actor, ip=src_ip, target=device_id)
                return (json_resp(200, {"ok": True}) if ok
                        else json_resp(404, {"error": "device not found"}))
            if op == "key":
                key = self.hub.reset_device_key(device_id, owner_id)
                self.hub.audit.log("device_key_reset", actor=actor, ip=src_ip, target=device_id,
                                   result="ok" if key else "fail")
                return (json_resp(200, {"key": key}) if key
                        else json_resp(404, {"error": "device not found"}))
            if op == "rename":
                name = str(body.get("display_name", "")).strip()
                if (not name or len(name) > 128
                        or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in name)):
                    return json_resp(400, {"error": "显示名不能为空或过长"})
                ok, old_name = self.hub.rename_device(device_id, name, owner_id)
                self.hub.audit.log("device_rename", actor=actor, ip=src_ip, target=device_id,
                                   detail=("from=%s to=%s" % (old_name, name))[:1000],
                                   result="ok" if ok else "fail")
                return (json_resp(200, {"ok": True, "display_name": name}) if ok
                        else json_resp(404, {"error": "device not found"}))
        if path.startswith("/api/admin/devices/") and method == "DELETE":
            device_id = path.split("/")[4]
            ok = self.hub.delete_device(device_id, owner_id)
            self.hub.audit.log("device_delete", actor=actor, ip=src_ip, target=device_id,
                               result="ok" if ok else "fail")
            return (json_resp(200, {"ok": True}) if ok
                    else json_resp(404, {"error": "device not found"}))
        if method == "POST" and path == "/api/tasks":
            device_id = str(body.get("device_id", ""))[:128]
            tool = str(body.get("tool", ""))
            arguments = body.get("arguments") or {}
            if tool not in self.TOOLS or not isinstance(arguments, dict):
                self.hub.audit.log("task_rejected", actor=actor, ip=src_ip,
                                   target=device_id, detail="tool=%s" % tool, result="reject")
                return json_resp(400, {"error": "tool not in whitelist"})
            status, result = await self.hub.dispatch_task(device_id, tool, arguments, owner_id)
            self.hub.audit.log("task_dispatch", actor=actor, ip=src_ip, target=device_id,
                               detail="tool=%s" % tool,
                               result="ok" if status == 200 else ("offline" if status == 409 else "fail"))
            return json_resp(status, result)
        if method == "GET" and path.startswith("/api/tasks/"):
            task_id = quote(path.split("/")[3], safe="")
            if not US.owns_task(self.db_path, owner_id, task_id):
                return json_resp(404, {"error": "task not found"})
            task = self.hub.get_task(task_id)
            return (json_resp(200, task) if task else json_resp(404, {"error": "task not found"}))
        # ===== 用户管理（仅 admin） =====
        is_user_api = (path == "/api/admin/users" or path.startswith("/api/admin/users/"))
        if is_user_api and not self._require_admin(user):
            self.hub.audit.log("user_denied", actor=actor, ip=src_ip, target=path, result="fail")
            return json_resp(403, {"error": "仅管理员可管理用户"})
        if method == "GET" and path == "/api/admin/users":
            rows = US.list_users(self.db_path)
            for r in rows:
                t = r["control_token"]
                r["control_token"] = t[:6] + "…" + t[-4:] if len(t) > 12 else "***"
            return json_resp(200, {"users": rows})
        if method == "GET" and path.startswith("/api/admin/users/") and path.endswith("/token"):
            # 列表只返回打码 token；按需单独读取完整 token，避免一次性暴露全部凭证。
            seg = path.split("/")
            try:
                uid = int(seg[4])
            except (ValueError, IndexError):
                return json_resp(400, {"error": "invalid user id"})
            target_user = US.get_by_id(self.db_path, uid)
            if not target_user:
                return json_resp(404, {"error": "user not found"})
            self.hub.audit.log("user_token_view", actor=actor, ip=src_ip,
                               target=target_user["username"])
            return json_resp(200, {"ok": True, "email": target_user["username"],
                                   "control_token": target_user["control_token"]})
        if method == "POST" and path == "/api/admin/users":
            # 邮箱即账号（兼容旧字段 username）
            email = str(body.get("email", body.get("username", ""))).strip().lower()
            password = str(body.get("password", ""))
            # 超管唯一：任何新建用户都只能是普通用户，忽略请求里的 role 防提权。
            role = "user"
            strength = check_password_strength(password)
            if strength:
                return json_resp(400, {"ok": False, "error": strength})
            new_user, err = US.create_user(self.db_path, email, password, role)
            if err:
                return json_resp(400, {"ok": False, "error": err})
            self.hub.audit.log("user_create", actor=actor, ip=src_ip,
                               target=email, detail="role=%s" % role)
            return json_resp(200, {"ok": True, "user": {
                "id": new_user["id"], "email": email,
                "role": new_user["role"], "control_token": new_user["control_token"],
            }, "hint": "control_token 仅显示这一次，请交给该用户保存"})
        if path.startswith("/api/admin/users/") and method == "POST":
            seg = path.split("/")
            if len(seg) < 5:
                return json_resp(404, {"error": "not found"})
            try:
                uid = int(seg[4])
            except ValueError:
                return json_resp(400, {"error": "invalid user id"})
            op = seg[-1]
            target_user = US.get_by_id(self.db_path, uid)
            if not target_user:
                return json_resp(404, {"error": "user not found"})
            if op == "password":
                new_pwd = str(body.get("new_password", ""))
                strength = check_password_strength(new_pwd)
                if strength:
                    return json_resp(400, {"ok": False, "error": strength})
                US.set_password(self.db_path, uid, new_pwd)
                self.hub.audit.log("user_password_set", actor=actor, ip=src_ip,
                                   target=target_user["username"])
                return json_resp(200, {"ok": True})
            if op == "token":
                token = US.reset_token(self.db_path, uid)
                self.hub.audit.log("user_token_reset", actor=actor, ip=src_ip,
                                   target=target_user["username"])
                return json_resp(200, {"ok": True, "control_token": token,
                                       "hint": "仅显示这一次"})
            if op == "enabled":
                enabled = bool(body.get("enabled"))
                # 禁止禁用唯一的超管（否则系统无人可管理）
                if not enabled and target_user["role"] == "admin":
                    admins = [u for u in US.list_users(self.db_path)
                              if u["role"] == "admin" and u["enabled"]]
                    if len(admins) <= 1:
                        return json_resp(400, {"ok": False,
                                               "error": "不能禁用唯一的管理员账号"})
                US.set_enabled(self.db_path, uid, enabled)
                self.hub.audit.log("user_enable" if enabled else "user_disable",
                                   actor=actor, ip=src_ip, target=target_user["username"])
                return json_resp(200, {"ok": True})
        if path.startswith("/api/admin/users/") and method == "DELETE":
            try:
                uid = int(path.split("/")[4])
            except (ValueError, IndexError):
                return json_resp(400, {"error": "invalid user id"})
            ok, err = US.delete_user(self.db_path, uid)
            self.hub.audit.log("user_delete", actor=actor, ip=src_ip,
                               target=str(uid), result="ok" if ok else "fail", detail=err)
            return (json_resp(200, {"ok": True}) if ok
                    else json_resp(400, {"ok": False, "error": err}))

        if method == "GET" and path == "/api/logs":
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            result_filter = query.get("result", [""])[0]
            # 页面下拉传 success/fail；审计库存 ok/fail/blocked/reject/offline
            if result_filter == "success":
                result_filter = "ok"
            try:
                rows = self.hub.audit.query(
                    month=query.get("month", [""])[0],
                    day=query.get("day", [""])[0],
                    actor=query.get("actor", [""])[0],
                    action=query.get("action", [""])[0],
                    result=result_filter,
                    page=page,
                )
            except ValueError as exc:
                return json_resp(400, {"error": str(exc)})
            return json_resp(200, rows)
        if method == "GET" and path == "/api/log-actions":
            # 动作下拉候选：汇总所有日志表中出现过的 action（供前端 datalist 用）
            actions = self.hub.audit.distinct_actions()
            return json_resp(200, {"actions": actions})
        return json_resp(404, {"error": "not found"})

    async def _login(self, body: dict, ip: str, cookies: dict) -> Response:
        # 邮箱即账号（兼容旧字段 username 作为回退）
        email = str(body.get("email", body.get("username", "")))[:254].strip().lower()
        password = str(body.get("password", ""))
        captcha_text = str(body.get("captcha", ""))
        # 验证码先行：一次性消费，防止绕过锁定无限试口令
        if not self.captcha.verify(cookies.get("captcha_id", ""), captcha_text):
            return json_resp(400, {"ok": False, "error": "验证码错误或已过期"})
        allowed, hint = self.guard.check(ip, email)
        if not allowed:
            self.hub.audit.log("login_blocked", actor=email, ip=ip, detail=hint, result="blocked")
            return json_resp(429, {
                "ok": False, "error": hint, "locked": True,
                "locked_until": self.guard.remaining_lock(ip, email),  # 剩余秒数，前端倒计时
            })
        ok, user = US.verify_login(self.db_path, email, password)
        if not ok:
            _cnt, left, msg = self.guard.record_fail(ip, email)
            self.hub.audit.log("login_fail", actor=email, ip=ip, detail=msg, result="fail")
            return json_resp(401, {
                "ok": False, "error": msg, "attempts_left": left,
                "locked_until": self.guard.remaining_lock(ip, email) or None,
            })
        self.guard.record_success(ip, email)
        sid, _ = self.sessions.create(user["username"], ip)
        self.hub.audit.log("login_success", actor=email, ip=ip)
        return json_resp(200, {"ok": True, "email": email, "username": user["username"],
                               "role": user["role"]}, {
            "Set-Cookie": cookie_header("session", sid, self.secure_cookie),
        })


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
async def serve(args) -> None:
    # 旧 settings 单管理员 → users 表（幂等）；首次生成随机口令只打印一次
    generated = ensure_admin(args.db, args.admin_user, args.admin_pass)
    if generated:
        print("ADMIN_USER=" + args.admin_user, flush=True)
        print("ADMIN_PASSWORD=" + generated + "  (仅此一次，请保存)", flush=True)
    US.init_schema(args.db)
    US.migrate_legacy_admin(args.db, args.control_token)
    # 邮箱即账号：把唯一的超管账号迁成指定邮箱（幂等，按角色找，不依赖旧用户名）
    admin_email = getattr(args, "admin_email", "") or ""
    if admin_email:
        migrated = US.migrate_admin_to_email(args.db, admin_email)
        if migrated:
            print("ADMIN_USER_MIGRATED -> " + admin_email, flush=True)
    admins = US.list_users(args.db)
    print("USERS=%d (admins: %s)" % (
        len(admins), ",".join(u["username"] for u in admins if u["role"] == "admin")), flush=True)

    hub = Hub(args.db, args.control_token)
    api = HttpApi(hub, args.db, args.control_token, secure_cookie=args.cookie_secure)

    async with ws_serve(hub.device_handler, args.host, args.ws_port):
        http_server = await asyncio.start_server(api.handle, args.host, args.http_port)
        scheme = "wss" if args.cookie_secure else "ws"
        print("WS   %s://%s:%d" % (scheme, args.host, args.ws_port), flush=True)
        print("WEB  http://%s:%d/login.html" % (args.host, args.http_port), flush=True)
        print("CONTROL_TOKEN=" + args.control_token, flush=True)
        async with http_server:
            await asyncio.Future()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ws-port", type=int, default=8765)
    ap.add_argument("--http-port", type=int, default=8766)
    ap.add_argument("--db", default="./hub.db")
    ap.add_argument("--admin-user", default="admin", help="旧管理员用户名（迁移用，默认 admin）")
    ap.add_argument("--admin-email", default=os.environ.get("ADMIN_EMAIL", ""),
                    help="超管登录邮箱；启动时把 --admin-user 账号迁成此邮箱（幂等）")
    ap.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASSWORD"))
    ap.add_argument("--control-token", default=os.environ.get("CONTROL_TOKEN") or secrets.token_urlsafe(24))
    ap.add_argument("--cookie-secure", action="store_true", help="HTTPS 反代后开启 Secure Cookie")
    ap.add_argument("--reset-admin-pass", metavar="NEWPASS", help="重置管理员口令后退出")
    args = ap.parse_args()

    if args.reset_admin_pass:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        # 同步更新 users 表里的管理员口令（多用户模型），并兼容旧 settings
        init_ok = False
        US.init_schema(args.db)
        with sqlite3.connect(args.db) as c:
            rows = c.execute("SELECT id FROM users WHERE role='admin'").fetchall()
        for (uid,) in rows:
            US.set_password(args.db, uid, args.reset_admin_pass)
            init_ok = True
        if not init_ok:
            reset_admin_password(args.db, args.reset_admin_pass)
        print("admin password reset OK (users=%d)" % len(rows), flush=True)
        return
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
