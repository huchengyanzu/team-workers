#!/usr/bin/env python3
"""公网安全模块：管理员口令、图形验证码、会话、防暴破、按月分表审计日志。

零第三方依赖，纯标准库，可在 2C2G 云机直接跑。
所有 SQL 均为参数化查询；任何用户可控字符串都不参与 SQL 拼接。

对外组件：
  PasswordHasher  - PBKDF2-HMAC-SHA256 加盐口令散列 + 常量时间比对
  CaptchaStore    - 一次性 SVG 图形验证码（内存、带过期与容量上限）
  SessionStore    - 内存会话（HttpOnly / SameSite / 超时）
  LoginGuard      - 按 IP+账号与 IP 双维度失败锁定（指数退避）
  AuditLogger     - 操作日志，logs_YYYYMM 按月分表
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

PBKDF2_ROUNDS = 180_000
SESSION_TTL = 8 * 3600           # 会话绝对有效期
SESSION_IDLE = 30 * 60           # 闲置超时
CAPTCHA_TTL = 180                # 验证码 3 分钟
CAPTCHA_MAX = 1000               # 内存验证码上限（防刷爆内存）
MAX_FAILS = 5                    # 连续失败 N 次锁定
BASE_LOCK = 15 * 60              # 首次锁定时长
MAX_LOCK = 24 * 3600             # 锁定上限

_MONTH_RE = re.compile(r"^\d{6}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOCAL_TZ = timezone(timedelta(hours=8))  # 界面按 GMT+8 展示，按天筛选以此为本地时区


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m")


# --------------------------------------------------------------------------
# 口令散列
# --------------------------------------------------------------------------
class PasswordHasher:
    @staticmethod
    def make(password: str, salt: str | None = None) -> tuple[str, str]:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
        ).hex()
        return salt, digest

    @staticmethod
    def verify(password: str, salt: str, digest: str) -> bool:
        try:
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
            ).hex()
        except (ValueError, TypeError):
            return False
        # 常量时间比对，防时序侧信道
        return hmac.compare_digest(candidate, digest)


# --------------------------------------------------------------------------
# 图形验证码（SVG，无 Pillow 依赖）
# --------------------------------------------------------------------------
class CaptchaStore:
    # 去掉易混淆字符
    _ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}

    def _gc(self) -> None:
        now = time.time()
        for cid in [k for k, (_, exp) in self._items.items() if exp < now]:
            self._items.pop(cid, None)
        while len(self._items) > CAPTCHA_MAX:
            self._items.pop(next(iter(self._items)))

    def issue(self) -> tuple[str, str]:
        """返回 (captcha_id, svg)。"""
        self._gc()
        cid = secrets.token_urlsafe(18)
        text = "".join(secrets.choice(self._ALPHABET) for _ in range(5))
        self._items[cid] = (text, time.time() + CAPTCHA_TTL)
        rnd = secrets.SystemRandom()
        parts = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="46" '
            'viewBox="0 0 150 46" role="img" aria-label="验证码">',
            '<rect width="150" height="46" fill="#eef2f7"/>',
        ]
        # 干扰线
        for _ in range(4):
            x1, y1, x2, y2 = (rnd.randint(0, 150), rnd.randint(0, 46),
                              rnd.randint(0, 150), rnd.randint(0, 46))
            color = rnd.choice(["#b9c6d8", "#c8d3e2", "#a9b8cf"])
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                         f'stroke="{color}" stroke-width="1"/>')
        # 字符：随机颜色/旋转/基线偏移
        palette = ["#1f3a5f", "#22577a", "#2b3a67", "#17435e", "#3a2e5f"]
        for i, ch in enumerate(text):
            x = 14 + i * 27
            y = rnd.randint(30, 37)
            deg = rnd.randint(-16, 16)
            color = rnd.choice(palette)
            size = rnd.randint(22, 27)
            parts.append(
                f'<text x="{x}" y="{y}" font-size="{size}" font-weight="700" '
                f'font-family="ui-monospace,Menlo,Consolas,monospace" fill="{color}" '
                f'transform="rotate({deg} {x} {y})">{xml_escape(ch)}</text>'
            )
        # 噪点
        for _ in range(18):
            x, y = rnd.randint(0, 150), rnd.randint(0, 46)
            parts.append(f'<circle cx="{x}" cy="{y}" r="1" fill="#9fb0c6" opacity="0.6"/>')
        parts.append("</svg>")
        return cid, "".join(parts)

    def verify(self, cid: str, text: str) -> bool:
        item = self._items.pop(cid or "", None)  # 一次性：无论对错都消费
        if not item:
            return False
        expected, exp = item
        if exp < time.time():
            return False
        return hmac.compare_digest(expected, (text or "").strip().upper())


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
class SessionStore:
    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def _gc(self) -> None:
        now = time.time()
        for sid, s in list(self._items.items()):
            if now - s["last"] > SESSION_IDLE or now - s["created"] > SESSION_TTL:
                self._items.pop(sid, None)

    def create(self, username: str, ip: str) -> tuple[str, dict]:
        self._gc()
        sid = secrets.token_urlsafe(32)
        now = time.time()
        self._items[sid] = {
            "username": username, "ip": ip,
            "created": now, "last": now,
        }
        return sid, self._items[sid].copy()

    def get(self, sid: str) -> dict | None:
        if not sid:
            return None
        self._gc()
        s = self._items.get(sid)
        if not s:
            return None
        s["last"] = time.time()
        return s.copy()

    def destroy(self, sid: str) -> None:
        self._items.pop(sid, None)

    def destroy_by_user(self, username: str) -> int:
        """删除该用户名下全部会话，返回删除数量（内存态，进程重启即失效）。"""
        doomed = [sid for sid, s in self._items.items() if s.get("username") == username]
        for sid in doomed:
            self._items.pop(sid, None)
        return len(doomed)


class ResetCodeGuard:
    """找回密码发码限流：按 IP + 邮箱双维度，滑动窗口计数（进程内存）。"""

    def __init__(self, ip_window: int = 60, ip_max: int = 3,
                 email_window: int = 60, email_max: int = 2) -> None:
        self.ip_window = ip_window
        self.ip_max = ip_max
        self.email_window = email_window
        self.email_max = email_max
        self._ip: dict[str, list[float]] = {}
        self._email: dict[str, list[float]] = {}

    @staticmethod
    def _prune(hits: list[float], now: float, window: int) -> list[float]:
        return [t for t in hits if now - t < window]

    def check(self, ip: str, email: str) -> tuple[bool, str]:
        now = time.time()
        ip_hits = self._prune(self._ip.get(ip, []), now, self.ip_window)
        em_hits = self._prune(self._email.get(email, []), now, self.email_window)
        if len(ip_hits) >= self.ip_max:
            return False, "操作过于频繁，请稍后再试"
        if len(em_hits) >= self.email_max:
            return False, "发送太频繁，请稍后再试"
        return True, ""

    def record(self, ip: str, email: str) -> None:
        now = time.time()
        self._ip[ip] = self._prune(self._ip.get(ip, []), now, self.ip_window) + [now]
        self._email[email] = self._prune(self._email.get(email, []), now, self.email_window) + [now]


# --------------------------------------------------------------------------
# 防暴破
# --------------------------------------------------------------------------
class LoginGuard:
    """失败锁定：按 (ip, username) 与 ip 双维度，指数退避。"""

    def __init__(self) -> None:
        self._fails: dict[tuple[str, str], tuple[int, float, float]] = {}
        self._ip_fails: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _lock_seconds(fails: int) -> int:
        if fails < MAX_FAILS:
            return 0
        return min(BASE_LOCK * (2 ** (fails - MAX_FAILS)), MAX_LOCK)

    def check(self, ip: str, username: str) -> tuple[bool, str]:
        """返回 (是否允许尝试, 提示信息)。"""
        now = time.time()
        key = (ip, username)
        cnt, until, _ = self._fails.get(key, (0, 0.0, 0.0))
        if until > now:
            return False, "账号已临时锁定，请 %d 秒后重试" % int(until - now)
        icnt, iuntil = self._ip_fails.get(ip, (0, 0.0))
        if iuntil > now:
            return False, "来源 IP 已临时限制，请 %d 秒后重试" % int(iuntil - now)
        left = max(0, MAX_FAILS - cnt)
        return True, "" if left == MAX_FAILS else "还可尝试 %d 次" % left

    def record_fail(self, ip: str, username: str) -> tuple[int, int, str]:
        """返回 (账号失败次数, 剩余尝试, 提示)。"""
        now = time.time()
        key = (ip, username)
        cnt, _, _ = self._fails.get(key, (0, 0.0, 0.0))
        cnt += 1
        lock = self._lock_seconds(cnt)
        until = now + lock if lock else 0.0
        self._fails[key] = (cnt, until, now)

        icnt, _ = self._ip_fails.get(ip, (0, 0.0))
        icnt += 1
        ilock = self._lock_seconds(icnt)
        self._ip_fails[ip] = (icnt, now + ilock if ilock else 0.0)

        if lock:
            return cnt, 0, "失败次数过多，已锁定 %d 秒" % lock
        return cnt, MAX_FAILS - cnt, "账号或密码错误，还可尝试 %d 次" % (MAX_FAILS - cnt)

    def remaining_lock(self, ip: str, username: str) -> int:
        """返回当前有效锁定的剩余秒数（账号/IP 取较大者），供前端倒计时。"""
        now = time.time()
        _, until, _ = self._fails.get((ip, username), (0, 0.0, 0.0))
        _, iuntil = self._ip_fails.get(ip, (0, 0.0))
        return max(0, int(max(until, iuntil) - now))

    def record_success(self, ip: str, username: str) -> None:
        self._fails.pop((ip, username), None)
        # IP 维度失败计数保留，但给一次成功做衰减，避免误伤同 NAT 其他人
        icnt, _ = self._ip_fails.get(ip, (0, 0.0))
        if icnt > 0:
            self._ip_fails[ip] = (max(0, icnt - 2), 0.0)


# --------------------------------------------------------------------------
# 按月分表审计日志
# --------------------------------------------------------------------------
class AuditLogger:
    _TABLE_RE = re.compile(r"^logs_\d{6}$")

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_table(current_month())

    def _ensure_table(self, month: str) -> str:
        if not _MONTH_RE.fullmatch(month):
            raise ValueError("invalid month")
        table = "logs_" + month
        if not self._TABLE_RE.fullmatch(table):
            raise ValueError("invalid table")
        with sqlite3.connect(self.db_path) as c:
            # 表名来自内部受控 month（已正则校验），不是用户输入拼接
            c.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, actor TEXT NOT NULL, ip TEXT NOT NULL, "
                "action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', "
                "detail TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT 'ok')"
            )
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table}(ts)")
        return table

    def log(self, action: str, actor: str = "-", ip: str = "-",
            target: str = "", detail: str = "", result: str = "ok") -> None:
        month = current_month()
        table = self._ensure_table(month)
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                f"INSERT INTO {table}(ts,actor,ip,action,target,detail,result) "
                "VALUES(?,?,?,?,?,?,?)",
                (now_iso(), actor[:128], ip[:64], action[:64],
                 target[:256], detail[:1000], result[:32]),
            )

    @staticmethod
    def _day_bounds(day: str) -> tuple[str, str]:
        """把本地日期 YYYY-MM-DD（GMT+8）换算成 UTC 的 ts 字符串边界 [start, end)。

        ts 存的是 UTC ISO（now_iso，形如 2026-11-20T16:00:00+00:00），
        字典序即时间序，可直接用字符串比较。界面按 GMT+8 显示，
        故本地某日 00:00 对应 UTC 前一日 16:00。
        """
        local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_LOCAL_TZ)
        start = local.astimezone(timezone.utc)
        end = (local + timedelta(days=1)).astimezone(timezone.utc)
        return (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))

    def query(self, month: str = "", actor: str = "", action: str = "",
              result: str = "", page: int = 1, page_size: int = 20,
              day: str = "") -> dict:
        """查询审计日志。day=YYYY-MM-DD 时按本地自然日精确过滤（可跨 UTC 月分表）。"""
        day = (day or "").strip()
        if day and not _DAY_RE.fullmatch(day):
            raise ValueError("day must be YYYY-MM-DD")

        clauses, args = [], []
        if actor:
            clauses.append("actor LIKE ?")
            args.append("%" + actor + "%")
        if action:
            clauses.append("action = ?")
            args.append(action)
        if result:
            clauses.append("result = ?")
            args.append(result)

        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        offset = (page - 1) * page_size

        if day:
            # 按天：ts 属于 [start,end)，可能横跨两张 UTC 月表。
            start, end = self._day_bounds(day)
            # 起点所在 UTC 月 与 终点前一秒所在 UTC 月（end 为排他的次日零点）
            start_month = start[:4] + start[5:7]
            end_inclusive = (datetime.fromisoformat(end) - timedelta(seconds=1))
            end_month = end_inclusive.strftime("%Y%m")
            months = sorted({start_month, end_month})
            # 每张表都加上 ts 区间条件
            day_clause = ["ts >= ?", "ts < ?"]
            where = (" WHERE " + " AND ".join(clauses + day_clause)) if (clauses or day_clause) else ""
            day_args = args + [start, end]
            rows_all = []
            with sqlite3.connect(self.db_path) as c:
                for m in months:
                    table = self._ensure_table(m)
                    rows_all.extend(c.execute(
                        f"SELECT ts,actor,ip,action,target,detail,result FROM {table}{where} "
                        "ORDER BY id DESC", day_args,
                    ).fetchall())
            rows_all.sort(key=lambda r: r[0], reverse=True)  # ts 字典序=时间序
            total = len(rows_all)
            rows = rows_all[offset:offset + page_size]
        else:
            month = month or current_month()
            if not _MONTH_RE.fullmatch(month):
                raise ValueError("month must be YYYYMM")
            table = self._ensure_table(month)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            with sqlite3.connect(self.db_path) as c:
                total = c.execute(f"SELECT COUNT(*) FROM {table}{where}", args).fetchone()[0]
                rows = c.execute(
                    f"SELECT ts,actor,ip,action,target,detail,result FROM {table}{where} "
                    "ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*args, page_size, offset),
                ).fetchall()
        return {
            "rows": [dict(zip(("ts", "actor", "ip", "action", "target", "detail", "result"), r))
                     for r in rows],
            "total": total,
            "page": page,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    def distinct_actions(self) -> list[str]:
        """返回所有日志表中出现过的 action 去重列表（供筛选下拉用）。"""
        actions: set[str] = set()
        with sqlite3.connect(self.db_path) as c:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'logs_%'"
            ).fetchall()]
            for name in names:
                if not self._TABLE_RE.fullmatch(name):
                    continue
                for (a,) in c.execute(f"SELECT DISTINCT action FROM {name}").fetchall():
                    if a:
                        actions.add(a)
        return sorted(actions)

    def months(self) -> list[str]:
        """返回已有日志表对应的月份列表（新→旧）。"""
        with sqlite3.connect(self.db_path) as c:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'logs_%'"
            ).fetchall()]
        return sorted((n[5:] for n in names if self._TABLE_RE.fullmatch(n)), reverse=True)

    def purge_before(self, month: str) -> int:
        """删除早于指定月份的日志表（运维用，调用方需显式确认）。"""
        if not _MONTH_RE.fullmatch(month):
            raise ValueError("month must be YYYYMM")
        count = 0
        with sqlite3.connect(self.db_path) as c:
            for name in [r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'logs_%'"
            ).fetchall()]:
                if self._TABLE_RE.fullmatch(name) and name[5:] < month:
                    c.execute(f"DROP TABLE {name}")
                    count += 1
        return count
