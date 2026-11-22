"""Windows 兼容性排查的模拟单测（Task #15）。

说明：本机是 macOS，不能在真 Windows 上跑；这里用 `windows_mode=True` 开关模拟 Windows 分支，
只验证纯逻辑（拆词、basename、编码兜底、默认白名单），不代替真机验证。
真机项见 README「Windows 上还要验什么」。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

PROTOTYPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROTOTYPE_DIR)
import exec_server  # noqa: E402


class WindowsCompatTests(unittest.TestCase):
    def test_basename_any_handles_both_separators(self):
        self.assertEqual(exec_server.basename_any(r"C:\Tools\echo.exe"), "echo.exe")
        self.assertEqual(exec_server.basename_any("C:/Tools/echo.exe"), "echo.exe")
        self.assertEqual(exec_server.basename_any("/usr/bin/echo"), "echo")
        self.assertEqual(exec_server.basename_any(r'"C:\Program Files\a.exe"'), "a.exe")
        # 反斜杠路径在 POSIX 的 os.path.basename 下会整串返回——这是原写法在跨平台日志/校验时的坑
        self.assertNotEqual(os.path.basename(r"C:\Tools\echo.exe"), "echo.exe")

    def test_split_command_windows_keeps_backslashes_and_strips_quotes(self):
        argv = exec_server.split_command(r'"C:\Program Files\echo.exe" hi', windows=True)
        self.assertEqual(argv, [r"C:\Program Files\echo.exe", "hi"])
        # POSIX 分支仍按 shlex 原样工作
        self.assertEqual(exec_server.split_command("echo hi", windows=False), ["echo", "hi"])

    def test_split_command_windows_preserves_path_without_quotes(self):
        argv = exec_server.split_command(r"C:\Tools\echo.exe one two", windows=True)
        self.assertEqual(argv, [r"C:\Tools\echo.exe", "one", "two"])

    def test_prepare_accepts_windows_path_when_basename_allowlisted(self):
        ex = exec_server.Executor(allowlist={"echo.exe"}, windows_mode=True)
        argv = ex._prepare([r"C:\Tools\echo.exe", "hi"])
        self.assertEqual(argv, [r"C:\Tools\echo.exe", "hi"])

    def test_prepare_rejects_windows_path_not_in_allowlist(self):
        ex = exec_server.Executor(allowlist={"echo.exe"}, windows_mode=True)
        with self.assertRaises(ValueError) as ctx:
            ex._prepare([r"C:\Windows\System32\rm.exe", "-rf"])
        self.assertIn("白名单", str(ctx.exception))

    def test_default_allowlist_differs_by_platform(self):
        posix = set(exec_server.default_allowlist("posix"))
        windows = set(exec_server.default_allowlist("nt"))
        self.assertIn("echo", posix)
        self.assertIn("ls", posix)
        self.assertIn("cmd", windows)
        self.assertIn("powershell", windows)
        # cmd 内建命令（echo/ls/cat）在 Windows 上不是可执行文件，不能直接 Popen
        self.assertNotIn("echo", windows)
        self.assertNotIn("ls", windows)

    def test_executor_windows_mode_uses_windows_default_allowlist(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("EXEC_ALLOWLIST", "EXEC_WINDOWS_MODE")}
        with mock.patch.dict(os.environ, env, clear=True):
            ex = exec_server.Executor(windows_mode=True)
            self.assertIn("cmd", ex.allowlist)

    def test_decode_output_prefers_utf8(self):
        text = "你好，中文输出"
        self.assertEqual(exec_server.decode_output(text.encode("utf-8")), text)

    def test_decode_output_falls_back_to_cp936_bytes(self):
        text = "系统盘剩余空间不足"
        raw = text.encode("gbk")
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")
        self.assertEqual(exec_server.decode_output(raw, fallback_encoding="gbk"), text)

    def test_decode_output_explicit_encoding_env_wins(self):
        raw = "中文".encode("gb18030")
        with mock.patch.dict(os.environ, {"EXEC_OUTPUT_ENCODING": "gb18030"}):
            self.assertEqual(exec_server.decode_output(raw), "中文")

    def test_spawn_kwargs_windows_does_not_use_start_new_session(self):
        ex = exec_server.Executor(allowlist={"cmd"}, windows_mode=True)
        kwargs = ex._spawn_kwargs()
        self.assertNotIn("start_new_session", kwargs)
        posix = exec_server.Executor(allowlist={"echo"}, windows_mode=False)
        self.assertEqual(posix._spawn_kwargs(), {"start_new_session": True})


if __name__ == "__main__":
    unittest.main()
