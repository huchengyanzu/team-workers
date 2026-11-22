#!/usr/bin/env python3
"""安全模块单元测试：口令散列、验证码一次性、会话超时、防暴破、按月分表与 SQL 注入。

运行： python3 -m unittest test_security -v
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from security import (
    AuditLogger, CaptchaStore, LoginGuard, PasswordHasher, SessionStore,
)


class TestPassword(unittest.TestCase):
    def test_verify_and_reject(self):
        salt, digest = PasswordHasher.make("StrongPass!2026")
        self.assertTrue(PasswordHasher.verify("StrongPass!2026", salt, digest))
        self.assertFalse(PasswordHasher.verify("wrong", salt, digest))
        self.assertNotEqual(salt, PasswordHasher.make("StrongPass!2026")[0])

    def test_bad_salt_no_crash(self):
        self.assertFalse(PasswordHasher.verify("x", "not-hex", "abc"))


class TestCaptcha(unittest.TestCase):
    def test_one_time(self):
        c = CaptchaStore()
        cid, svg = c.issue()
        text = "".join(
            __import__("re").findall(r"<text[^>]*>([A-Z0-9])</text>", svg)
        )
        self.assertEqual(len(text), 5)
        self.assertIn("<svg", svg)
        self.assertTrue(c.verify(cid, text))
        self.assertFalse(c.verify(cid, text))  # 一次性：重放无效

    def test_wrong_text(self):
        c = CaptchaStore()
        cid, _ = c.issue()
        self.assertFalse(c.verify(cid, "ZZZZZ"))


class TestSession(unittest.TestCase):
    def test_create_get_destroy(self):
        s = SessionStore()
        sid, _ = s.create("admin", "1.2.3.4")
        self.assertEqual(s.get(sid)["username"], "admin")
        s.destroy(sid)
        self.assertIsNone(s.get(sid))


class TestLoginGuard(unittest.TestCase):
    def test_exponential_lock(self):
        g = LoginGuard()
        for i in range(5):
            ok, _ = g.check("1.2.3.4", "admin")
            self.assertTrue(ok, "第 %d 次应允许" % (i + 1))
            cnt, left, msg = g.record_fail("1.2.3.4", "admin")
            self.assertEqual(left, 4 - i)
        self.assertIn("锁定", msg)  # 第 5 次失败触发锁定
        ok, _ = g.check("1.2.3.4", "admin")
        self.assertFalse(ok)  # 第 6 次起被拒
        self.assertGreaterEqual(g.remaining_lock("1.2.3.4", "admin"), 800)
        g.record_success("1.2.3.4", "admin")
        ok, _ = g.check("1.2.3.4", "admin")
        self.assertTrue(ok)  # 成功后解锁

    def test_ip_dimension(self):
        g = LoginGuard()
        for _ in range(5):
            g.record_fail("9.9.9.9", "user%d" % _)
        ok, _ = g.check("9.9.9.9", "someone_else")
        self.assertFalse(ok)  # IP 维度连带限制


class TestAuditLogger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "audit.db")
        self.log = AuditLogger(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_monthly_table_and_query(self):
        self.log.log("login_success", actor="admin", ip="1.2.3.4")
        self.log.log("login_fail", actor="admin", ip="1.2.3.4", result="fail")
        names = [r[0] for r in sqlite3.connect(self.db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        self.assertIn("logs_202611", names)  # 按当前月份分表
        rows = self.log.query(month="202611")
        self.assertEqual(rows["total"], 2)
        self.assertIn("login_success", [r["action"] for r in rows["rows"]])

    def test_sql_injection_safe(self):
        # 注入串只作为普通字符串参数，不破坏查询也不影响其他表
        attack = "x'; DROP TABLE devices; --"
        self.log.log("login_fail", actor=attack, ip=attack, result="fail")
        rows = self.log.query(month="202611", actor=attack)
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["rows"][0]["actor"], attack)
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS devices(id TEXT)")
        names = [r[0] for r in sqlite3.connect(self.db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        self.assertIn("devices", names)  # 表未被删除

    def test_invalid_month_rejected(self):
        with self.assertRaises(ValueError):
            self.log.query(month="2026-11; DROP TABLE x")
        with self.assertRaises(ValueError):
            self.log.purge_before("bad")


if __name__ == "__main__":
    unittest.main(verbosity=2)
