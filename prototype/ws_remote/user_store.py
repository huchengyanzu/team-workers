#!/usr/bin/env python3
"""多用户存储：users 表 + 设备/任务归属 + 从旧 settings 单管理员迁移。

设计：
  - 用户存 users 表（不再是 settings 里的固定 key），可增删多个用户
  - 每个用户一个独立 control_token（skill 用它）；老全局 token 归迁移出的管理员
  - devices / tasks 带 owner_id；列设备、下发任务、查任务都按 owner_id 过滤
  - role: admin | user；admin 可管理用户，普通用户只能操作自己的设备
  - 所有 SQL 参数化；口令 PBKDF2 加盐（复用 security.PasswordHasher）

迁移（幂等）：
  - 若 users 表为空但 settings 里有旧 admin_user/salt/hash，则建管理员账号，
    control_token = 传入的 legacy_token（保证现有 skill 的 REMOTE_TOKEN 不失效）；
    现有 devices/tasks 的 owner_id 全部指向该管理员。
  - 已迁移过则什么都不做，绝不覆盖现有口令/token。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from security import PasswordHasher, now_iso


@contextlib.contextmanager
def _connect(db_path: str):
    """确保连接用后关闭（长驻服务里避免连接累积）。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path)
    try:
        with c:
            yield c
    finally:
        c.close()


def init_schema(db_path: str) -> None:
    """建 users/email_codes 表，为旧表补列（幂等）。"""
    with _connect(db_path) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                control_token TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                purpose TEXT NOT NULL DEFAULT 'register',
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_email_codes_email ON email_codes(email, purpose, used);
            """
        )
        # 旧库补 email 列（唯一索引不能在含 NULL 的旧行上直接建，用普通索引去重由应用层保证）
        ucols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "email" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        # email 唯一索引：仅对非空邮箱生效，避免旧用户空串冲突
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email<>''")
        # SQLite 没有 ADD COLUMN IF NOT EXISTS：表不存在则跳过（由 Hub._init_db 建表），
        # 表存在但没有 owner_id 列才 ALTER。这样 init_schema 独立调用也不报错。
        for table in ("devices", "tasks"):
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            cols = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}
            if "owner_id" not in cols:
                c.execute("ALTER TABLE %s ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0" % table)


def migrate_legacy_admin(db_path: str, legacy_token: str) -> None:
    """把旧 settings 单管理员迁成 users 表首用户；已有用户则跳过（幂等）。"""
    init_schema(db_path)
    with _connect(db_path) as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return
        row = c.execute(
            "SELECT value FROM settings WHERE key='admin_user'"
        ).fetchone()
        if not row:
            return
        username = row[0]
        vals = dict(c.execute(
            "SELECT key,value FROM settings WHERE key IN ('admin_salt','admin_hash')"
        ).fetchall())
        salt = vals.get("admin_salt", "")
        digest = vals.get("admin_hash", "")
        if not salt or not digest:
            return
        token = legacy_token or secrets.token_urlsafe(32)
        cur = c.execute(
            "INSERT INTO users(username,salt,password_hash,role,control_token,enabled,created_at) "
            "VALUES(?,?,?,'admin',?,1,?)",
            (username[:128], salt, digest, token, now_iso()),
        )
        admin_id = cur.lastrowid
        # 现有设备/任务归到管理员（owner_id 仍为 0 的）；表可能尚未由 Hub 建立
        for table in ("devices", "tasks"):
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                c.execute("UPDATE %s SET owner_id=? WHERE owner_id=0" % table, (admin_id,))


def migrate_admin_to_email(db_path: str, new_email: str) -> bool:
    """把当前唯一的 admin 账号迁成邮箱身份（幂等）。超管唯一，按角色找而非按用户名。"""
    new_email = valid_email(new_email)
    if not new_email:
        return False
    init_schema(db_path)
    with _connect(db_path) as c:
        rows = c.execute("SELECT id,username FROM users WHERE role='admin'").fetchall()
        if not rows:
            return False
        uid, current = rows[0][0], rows[0][1]
        # 已经是目标邮箱则无需迁移
        if current.lower() == new_email:
            return False
        clash = c.execute("SELECT id FROM users WHERE (username=? OR email=?) AND id<>?",
                          (new_email, new_email, uid)).fetchone()
        if clash:
            return False
        c.execute("UPDATE users SET username=?, email=? WHERE id=?", (new_email, new_email, uid))
        try:
            c.execute("UPDATE sessions SET username=? WHERE username=?", (new_email, current))
        except sqlite3.OperationalError:
            pass
    return True


def migrate_username_to_email(db_path: str, old_username: str, new_email: str) -> bool:
    """把指定账号的 username 改成邮箱（并补 email 列）。幂等。

    返回 True 表示本次确实改了。用于把旧的用户名账号切换成邮箱身份。
    若新邮箱已被别的账号占用则拒绝迁移（返回 False），避免唯一约束冲突。
    """
    new_email = valid_email(new_email)
    if not new_email:
        return False
    init_schema(db_path)
    with _connect(db_path) as c:
        row = c.execute("SELECT id,email FROM users WHERE username=?", (old_username,)).fetchone()
        if not row:
            # 已经是新邮箱则视为已迁移
            row2 = c.execute("SELECT id FROM users WHERE username=? OR email=?",
                             (new_email, new_email)).fetchone()
            return False
        uid = row[0]
        # 目标邮箱被其它账号占用则不迁移
        clash = c.execute("SELECT id FROM users WHERE (username=? OR email=?) AND id<>?",
                          (new_email, new_email, uid)).fetchone()
        if clash:
            return False
        c.execute("UPDATE users SET username=?, email=? WHERE id=?", (new_email, new_email, uid))
        # session 表若存了旧用户名，一并更新（避免已登录会话失效）
        try:
            c.execute("UPDATE sessions SET username=? WHERE username=?", (new_email, old_username))
        except sqlite3.OperationalError:
            pass  # session 未必是 SQLite 表（内存存储），忽略
    return True


# ---------- 查询 ----------
def get_by_username(db_path: str, username: str) -> dict | None:
    with _connect(db_path) as c:
        cur = c.execute("SELECT * FROM users WHERE username=?", (username,))
        return _row_named(cur, cur.fetchone())


def get_by_id(db_path: str, uid: int) -> dict | None:
    with _connect(db_path) as c:
        cur = c.execute("SELECT * FROM users WHERE id=?", (uid,))
        return _row_named(cur, cur.fetchone())


def get_by_token(db_path: str, token: str) -> dict | None:
    if not token:
        return None
    with _connect(db_path) as c:
        cur = c.execute("SELECT * FROM users WHERE control_token=?", (token,))
        return _row_named(cur, cur.fetchone())


def get_by_email(db_path: str, email: str) -> dict | None:
    if not email:
        return None
    with _connect(db_path) as c:
        cur = c.execute("SELECT * FROM users WHERE email=?", (email,))
        return _row_named(cur, cur.fetchone())


def list_users(db_path: str) -> list[dict]:
    with _connect(db_path) as c:
        cur = c.execute("SELECT id,username,email,role,control_token,enabled,created_at FROM users ORDER BY id")
        rows = cur.fetchall()
    return [
        {"id": r[0], "username": r[1], "email": r[2] or r[1], "role": r[3],
         "control_token": r[4], "enabled": bool(r[5]), "created_at": r[6]}
        for r in rows
    ]


def _row(r) -> dict | None:
    """把 SELECT * 结果按 cursor.description 动态转 dict（加列后不会错位）。"""
    if not r:
        return None
    # r 来自 fetchone()；列顺序由查询决定，这里用调用方传入元组无法拿到 description，
    # 因此 get_by_* 会改用 _row_named(cursor, row)。保留此函数兼容 tuple(8列) 旧调用。
    keys = ("id", "username", "salt", "password_hash", "role", "control_token",
            "enabled", "created_at", "email")
    d = {k: r[i] for i, k in enumerate(keys) if i < len(r)}
    d["enabled"] = bool(d.get("enabled"))
    return d


def _row_named(cur: sqlite3.Cursor, r) -> dict | None:
    """按 cursor.description 把一行转成 dict（列增减安全）。"""
    if not r:
        return None
    d = {col[0]: r[i] for i, col in enumerate(cur.description)}
    if "enabled" in d:
        d["enabled"] = bool(d["enabled"])
    return d


# ---------- 校验 ----------
def valid_email(value: str) -> str:
    """规范化邮箱（去空白、转小写），格式不合法返回空串。"""
    v = (value or "").strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@") or len(v) > 254 or " " in v:
        return ""
    return v


def verify_login(db_path: str, email: str, password: str) -> tuple[bool, dict | None]:
    """邮箱登录（username 列存的就是邮箱，兼容旧数据按 email 列查）。"""
    email = valid_email(email)
    if not email:
        return False, None
    u = get_by_email(db_path, email) or get_by_username(db_path, email)
    if not u or not u["enabled"]:
        return False, None
    if not PasswordHasher.verify(password, u["salt"], u["password_hash"]):
        return False, None
    return True, u


def token_matches(user: dict, token: str) -> bool:
    """常量时间比对 control token（token 是秘密）。"""
    return bool(token) and hmac.compare_digest(token, user["control_token"])


# ---------- 变更 ----------
def create_user(db_path: str, email: str, password: str, role: str = "user") -> tuple[dict | None, str]:
    """邮箱即身份：username 列与 email 列都存邮箱。返回 (user, error)。"""
    email = valid_email(email)
    if not email:
        return None, "邮箱格式不正确"
    if role not in ("admin", "user"):
        return None, "角色只能是 admin 或 user"
    if get_by_email(db_path, email) or get_by_username(db_path, email):
        return None, "该邮箱已被注册"
    salt, digest = PasswordHasher.make(password)
    token = secrets.token_urlsafe(32)
    try:
        with _connect(db_path) as c:
            cur = c.execute(
                "INSERT INTO users(username,salt,password_hash,role,control_token,enabled,created_at,email) "
                "VALUES(?,?,?,?,?,1,?,?)",
                (email, salt, digest, role, token, now_iso(), email),
            )
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        return None, "该邮箱已被注册"
    return get_by_id(db_path, uid), ""


def set_password(db_path: str, uid: int, new_password: str) -> None:
    salt, digest = PasswordHasher.make(new_password)
    with _connect(db_path) as c:
        c.execute("UPDATE users SET salt=?, password_hash=? WHERE id=?", (salt, digest, uid))


def reset_token(db_path: str, uid: int) -> str:
    token = secrets.token_urlsafe(32)
    with _connect(db_path) as c:
        c.execute("UPDATE users SET control_token=? WHERE id=?", (token, uid))
    return token


def set_enabled(db_path: str, uid: int, enabled: bool) -> None:
    with _connect(db_path) as c:
        c.execute("UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, uid))


def delete_user(db_path: str, uid: int) -> tuple[bool, str]:
    """删除用户；仍有设备/任务归属时拒绝（需先转移或删除）。不能删最后一个 admin。"""
    with _connect(db_path) as c:
        u = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return False, "用户不存在"
        if u[0] == "admin":
            admins = c.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1"
            ).fetchone()[0]
            if admins <= 1:
                return False, "不能删除唯一的管理员"
        if c.execute("SELECT COUNT(*) FROM devices WHERE owner_id=?", (uid,)).fetchone()[0]:
            return False, "该用户仍有设备，请先删除或转移设备"
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    return True, ""


# ---------- 归属 ----------
def owns_device(db_path: str, uid: int, device_id: str) -> bool:
    with _connect(db_path) as c:
        r = c.execute(
            "SELECT 1 FROM devices WHERE device_id=? AND owner_id=?", (device_id, uid)
        ).fetchone()
    return bool(r)


def owns_task(db_path: str, uid: int, task_id: str) -> bool:
    with _connect(db_path) as c:
        r = c.execute(
            "SELECT 1 FROM tasks WHERE task_id=? AND owner_id=?", (task_id, uid)
        ).fetchone()
    return bool(r)


# ---------- 邮箱注册 ----------
# 验证码：6 位数字，5 分钟有效，最多尝试 5 次，一次性；只存 hash 不存明文。
CODE_TTL_SECONDS = 300
CODE_MAX_ATTEMPTS = 5
SEND_COOLDOWN_SECONDS = 60


def _code_hash(email: str, code: str) -> str:
    return hashlib.sha256(("%s|%s" % (email.lower().strip(), code)).encode("utf-8")).hexdigest()


def issue_email_code(db_path: str, email: str, purpose: str = "register") -> tuple[str | None, str]:
    """生成验证码返回明文（供发邮件），库里只存 hash。返回 (code, error)。

    限流：同一邮箱 + purpose 60 秒内有未使用且未过期的验证码则拒绝重发。
    """
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 254:
        return None, "邮箱格式不正确"
    if get_by_email(db_path, email):
        return None, "该邮箱已注册"
    now = datetime.now(timezone.utc)
    with _connect(db_path) as c:
        recent = c.execute(
            "SELECT created_at FROM email_codes WHERE email=? AND purpose=? AND used=0 "
            "ORDER BY id DESC LIMIT 1", (email, purpose)
        ).fetchone()
        if recent:
            try:
                created = datetime.fromisoformat(recent[0])
                if (now - created).total_seconds() < SEND_COOLDOWN_SECONDS:
                    left = int(SEND_COOLDOWN_SECONDS - (now - created).total_seconds())
                    return None, "发送太频繁，请 %d 秒后重试" % left
            except (ValueError, TypeError):
                pass
        code = "%06d" % secrets.randbelow(1_000_000)
        expires = (now + timedelta(seconds=CODE_TTL_SECONDS)).isoformat()
        c.execute(
            "INSERT INTO email_codes(email,code_hash,purpose,expires_at,attempts,used,created_at) "
            "VALUES(?,?,?,?,0,0,?)",
            (email, _code_hash(email, code), purpose, expires, now.isoformat()),
        )
    return code, ""


def verify_email_code(db_path: str, email: str, code: str, purpose: str = "register") -> tuple[bool, str]:
    """校验验证码（不消费；成功后由 register_with_email 标记 used）。返回 (ok, error)。"""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    now = datetime.now(timezone.utc)
    with _connect(db_path) as c:
        row = c.execute(
            "SELECT id,code_hash,expires_at,attempts,used FROM email_codes "
            "WHERE email=? AND purpose=? AND used=0 ORDER BY id DESC LIMIT 1",
            (email, purpose),
        ).fetchone()
        if not row:
            return False, "验证码不存在或已使用，请重新获取"
        cid, code_hash, expires, attempts, used = row
        try:
            if now > datetime.fromisoformat(expires):
                return False, "验证码已过期，请重新获取"
        except (ValueError, TypeError):
            return False, "验证码已失效，请重新获取"
        if attempts >= CODE_MAX_ATTEMPTS:
            return False, "尝试次数过多，请重新获取验证码"
        if not hmac.compare_digest(code_hash, _code_hash(email, code)):
            c.execute("UPDATE email_codes SET attempts=attempts+1 WHERE id=?", (cid,))
            return False, "验证码错误"
    return True, ""


def register_with_email(db_path: str, email: str, code: str,
                        password: str, role: str = "user") -> tuple[dict | None, str]:
    """邮箱即身份：校验验证码并创建用户，成功后验证码标记 used。返回 (user, error)。"""
    email = valid_email(email)
    if not email:
        return None, "邮箱格式不正确"
    if role not in ("user",):  # 自助注册只能建普通用户，不能自建 admin
        return None, "注册角色不合法"
    ok, err = verify_email_code(db_path, email, code, "register")
    if not ok:
        return None, err
    if get_by_email(db_path, email) or get_by_username(db_path, email):
        return None, "该邮箱已被注册"
    salt, digest = PasswordHasher.make(password)
    token = secrets.token_urlsafe(32)
    try:
        with _connect(db_path) as c:
            cur = c.execute(
                "INSERT INTO users(username,salt,password_hash,role,control_token,enabled,created_at,email) "
                "VALUES(?,?,?,'user',?,1,?,?)",
                (email, salt, digest, token, now_iso(), email),
            )
            uid = cur.lastrowid
            # 标记该邮箱已发的验证码全部作废，防止重放
            c.execute("UPDATE email_codes SET used=1 WHERE email=? AND purpose='register'", (email,))
    except sqlite3.IntegrityError:
        return None, "该邮箱已被注册"
    return get_by_id(db_path, uid), ""
