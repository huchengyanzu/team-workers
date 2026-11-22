#!/usr/bin/env python3
"""多用户与归属隔离单测：users 表、迁移、token 隔离、越权拒绝、SQL 注入。

运行： python3 -m unittest test_users -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import user_store as US
from security import PasswordHasher, now_iso


class TestUserStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "hub.db")
        US.init_schema(self.db)
        # 模拟 Hub 建立的设备/任务表（含 owner_id）
        with sqlite3.connect(self.db) as c:
            c.executescript(
                "CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY,display_name TEXT,"
                "platform TEXT,key_hash TEXT,status TEXT,info_json TEXT,last_seen TEXT,"
                "enabled INTEGER,created_at TEXT,owner_id INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,device_id TEXT,tool TEXT,"
                "args_json TEXT,state TEXT,result_json TEXT,error TEXT,created_at TEXT,"
                "finished_at TEXT,owner_id INTEGER NOT NULL DEFAULT 0);"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def _mkdev(self, device_id, owner_id):
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO devices(device_id,display_name,platform,key_hash,status,"
                "info_json,last_seen,enabled,created_at,owner_id) "
                "VALUES(?,?,'mac','h','offline','{}',?,1,?,?)",
                (device_id, device_id, now_iso(), now_iso(), owner_id),
            )

    def _mktask(self, task_id, owner_id):
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO tasks(task_id,device_id,tool,args_json,state,created_at,owner_id) "
                "VALUES(?,?,?,'{}','done',?,?)",
                (task_id, "dev-x", "exec", now_iso(), owner_id),
            )

    def test_create_and_login(self):
        u, err = US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026", "user")
        self.assertEqual(err, "", err)
        self.assertTrue(u["control_token"])
        ok, found = US.verify_login(self.db, "alice@x.com", "Str0ng!Pass2026")
        self.assertTrue(ok)
        self.assertEqual(found["id"], u["id"])
        self.assertFalse(US.verify_login(self.db, "alice@x.com", "wrong")[0])
        self.assertFalse(US.verify_login(self.db, "bob@x.com", "Str0ng!Pass2026")[0])

    def test_duplicate_email_rejected(self):
        US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026")
        u, err = US.create_user(self.db, "alice@x.com", "Another!Pass2026")
        self.assertIsNone(u)
        self.assertIn("已被注册", err)

    def test_token_isolation(self):
        a, _ = US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026")
        b, _ = US.create_user(self.db, "bob@x.com", "Str0ng!Pass2026")
        self.assertNotEqual(a["control_token"], b["control_token"])
        self.assertEqual(US.get_by_token(self.db, a["control_token"])["id"], a["id"])
        self.assertEqual(US.get_by_token(self.db, b["control_token"])["id"], b["id"])
        self.assertIsNone(US.get_by_token(self.db, "not-a-token"))
        self.assertTrue(US.token_matches(a, a["control_token"]))
        self.assertFalse(US.token_matches(a, b["control_token"]))

    def test_owns_device_and_task(self):
        a, _ = US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026")
        b, _ = US.create_user(self.db, "bob@x.com", "Str0ng!Pass2026")
        self._mkdev("dev-a", a["id"])
        self._mktask("task-a", a["id"])
        self.assertTrue(US.owns_device(self.db, a["id"], "dev-a"))
        self.assertFalse(US.owns_device(self.db, b["id"], "dev-a"))
        self.assertTrue(US.owns_task(self.db, a["id"], "task-a"))
        self.assertFalse(US.owns_task(self.db, b["id"], "task-a"))

    def test_disabled_user_cannot_login_and_token(self):
        u, _ = US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026")
        US.set_enabled(self.db, u["id"], False)
        self.assertFalse(US.verify_login(self.db, "alice@x.com", "Str0ng!Pass2026")[0])
        self.assertFalse(US.get_by_token(self.db, u["control_token"])["enabled"])

    def test_reset_token_changes_token(self):
        u, _ = US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026")
        old = u["control_token"]
        new = US.reset_token(self.db, u["id"])
        self.assertNotEqual(old, new)
        self.assertIsNone(US.get_by_token(self.db, old))
        self.assertEqual(US.get_by_token(self.db, new)["id"], u["id"])

    def test_delete_user_with_device_rejected(self):
        u, _ = US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026")
        self._mkdev("dev-a", u["id"])
        ok, err = US.delete_user(self.db, u["id"])
        self.assertFalse(ok)
        self.assertIn("设备", err)

    def test_delete_last_admin_rejected(self):
        a, _ = US.create_user(self.db, "admin1@x.com", "Str0ng!Pass2026", "admin")
        ok, err = US.delete_user(self.db, a["id"])
        self.assertFalse(ok)
        self.assertIn("管理员", err)

    def test_sql_injection_safe(self):
        attack = "x'; DROP TABLE users; --"
        # 注入串不是合法邮箱，被拒绝；且无论接受与否，表结构都不被破坏（参数化查询）
        u, err = US.create_user(self.db, attack, "Str0ng!Pass2026")
        self.assertIsNone(u)
        with sqlite3.connect(self.db) as c:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("users", names)  # 表未被删

    def test_list_users_masks_nothing_in_store(self):
        """存储层返回完整 token 供内部使用；掩码由 API 层负责。"""
        US.create_user(self.db, "alice@x.com", "Str0ng!Pass2026")
        rows = US.list_users(self.db)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["control_token"])


class TestEmailRegistration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "hub.db")
        US.init_schema(self.db)
        with sqlite3.connect(self.db) as c:
            c.executescript(
                "CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY,display_name TEXT,"
                "platform TEXT,key_hash TEXT,status TEXT,info_json TEXT,last_seen TEXT,"
                "enabled INTEGER,created_at TEXT,owner_id INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,device_id TEXT,tool TEXT,"
                "args_json TEXT,state TEXT,result_json TEXT,error TEXT,created_at TEXT,"
                "finished_at TEXT,owner_id INTEGER NOT NULL DEFAULT 0);"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_issue_and_verify_code(self):
        code, err = US.issue_email_code(self.db, "a@x.com")
        self.assertEqual(err, "")
        self.assertEqual(len(code), 6)
        # 库里只存 hash，不存明文
        with sqlite3.connect(self.db) as c:
            stored = c.execute("SELECT code_hash FROM email_codes").fetchone()[0]
        self.assertNotEqual(stored, code)
        ok, e = US.verify_email_code(self.db, "a@x.com", code)
        self.assertTrue(ok, e)

    def test_wrong_code_rejected_and_attempt_limited(self):
        code, _ = US.issue_email_code(self.db, "a@x.com")
        bad = "000000" if code != "000000" else "999999"
        for _ in range(US.CODE_MAX_ATTEMPTS):
            ok, _ = US.verify_email_code(self.db, "a@x.com", bad)
            self.assertFalse(ok)
        ok, e = US.verify_email_code(self.db, "a@x.com", code)
        self.assertFalse(ok)
        self.assertIn("次数", e)

    def test_send_cooldown(self):
        US.issue_email_code(self.db, "a@x.com")
        code2, err = US.issue_email_code(self.db, "a@x.com")
        self.assertIsNone(code2)
        self.assertIn("频繁", err)

    def test_register_success_and_token(self):
        code, _ = US.issue_email_code(self.db, "bob@x.com")
        u, err = US.register_with_email(self.db, "bob@x.com", code, "Str0ng!Pass2026")
        self.assertEqual(err, "")
        self.assertIsNotNone(u)
        self.assertEqual(u["email"], "bob@x.com")
        self.assertEqual(u["role"], "user")  # 自助注册不能建 admin
        self.assertTrue(u["control_token"])
        # 新 token 可查到用户
        self.assertEqual(US.get_by_token(self.db, u["control_token"])["id"], u["id"])

    def test_code_replay_blocked(self):
        code, _ = US.issue_email_code(self.db, "c@x.com")
        US.register_with_email(self.db, "c@x.com", code, "Str0ng!Pass2026")
        u2, err = US.register_with_email(self.db, "c@x.com", code, "Str0ng!Pass2026")
        self.assertIsNone(u2)
        self.assertIn("已使用", err)

    def test_duplicate_email_rejected(self):
        code, _ = US.issue_email_code(self.db, "d@x.com")
        US.register_with_email(self.db, "d@x.com", code, "Str0ng!Pass2026")
        _, err = US.issue_email_code(self.db, "d@x.com")
        self.assertIn("已注册", err)
        # 不同邮箱可以独立注册（邮箱即账号，无独立用户名字段）
        code2, _ = US.issue_email_code(self.db, "e@x.com")
        u, err = US.register_with_email(self.db, "e@x.com", code2, "Str0ng!Pass2026")
        self.assertIsNotNone(u, err)
        self.assertEqual(u["email"], "e@x.com")

    def test_bad_email_rejected(self):
        code, err = US.issue_email_code(self.db, "not-an-email")
        self.assertIsNone(code)
        self.assertIn("格式", err)


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "hub.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy(self, user="legacy@example.com", password="Legacy!Pass2026"):
        salt, digest = PasswordHasher.make(password)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                          [("admin_user", user), ("admin_salt", salt), ("admin_hash", digest)])
            # 模拟旧设备/任务（owner_id 默认 0）
            c.execute("CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY,display_name TEXT,"
                      "platform TEXT,key_hash TEXT,status TEXT,info_json TEXT,last_seen TEXT,"
                      "enabled INTEGER,created_at TEXT)")
            c.execute("INSERT INTO devices VALUES('dev-old','旧设备','mac','h','offline','{}',?,1,?)",
                      (now_iso(), now_iso()))
            c.execute("CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,device_id TEXT,tool TEXT,"
                      "args_json TEXT,state TEXT,result_json TEXT,error TEXT,created_at TEXT,finished_at TEXT)")
        return password

    def test_migrate_admin_and_devices(self):
        pwd = self._legacy()
        token = "legacy-control-token"
        US.migrate_legacy_admin(self.db, token)
        u = US.get_by_username(self.db, "legacy@example.com")
        self.assertIsNotNone(u)
        self.assertEqual(u["role"], "admin")
        self.assertEqual(u["control_token"], token)
        self.assertTrue(US.verify_login(self.db, "legacy@example.com", pwd)[0])
        # 旧设备归到管理员
        with sqlite3.connect(self.db) as c:
            owner = c.execute("SELECT owner_id FROM devices WHERE device_id='dev-old'").fetchone()[0]
        self.assertEqual(owner, u["id"])
        self.assertTrue(US.owns_device(self.db, u["id"], "dev-old"))

    def test_migration_idempotent(self):
        self._legacy()
        US.migrate_legacy_admin(self.db, "token-one")
        u1 = US.get_by_username(self.db, "legacy@example.com")
        # 第二次迁移不应覆盖 token/口令
        US.migrate_legacy_admin(self.db, "token-two")
        u2 = US.get_by_username(self.db, "legacy@example.com")
        self.assertEqual(u1["control_token"], u2["control_token"])
        self.assertEqual(len(US.list_users(self.db)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
