#!/usr/bin/env python3
"""协议层单元测试：不启动真实服务端，直接测消息处理与数据结构。

用途：快速回归（无需开端口/起进程）。端到端联调见 README 的 10 项手工验证与
docs/design/websocket-remote-skill-design.md §8 验收清单。

运行：python3 -m unittest test_proto -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))
sys.path.insert(0, str(THIS.parent))

from server import Hub  # noqa: E402
from exec_server import Executor, McpServer, normalize_allowlist  # noqa: E402


class TestAllowlistNormalization(unittest.TestCase):
    """回归：Windows 设备端残留 POSIX 白名单导致 exec 全废（家用机 2026-11-21 实测）。"""

    def test_posix_unchanged(self):
        out = normalize_allowlist(["echo", "ls", "cat"], windows_mode=False)
        self.assertEqual(out, {"echo", "ls", "cat"})

    def test_windows_drops_posix_only_and_keeps_valid(self):
        # 家用机的实际配置：POSIX 全选 + 两个真 exe，归一化后只留 whoami/hostname + 兜底 cmd
        cfg = ["echo", "pwd", "ls", "whoami", "cat", "sleep", "hostname", "date", "uptime", "uname"]
        out = normalize_allowlist(cfg, windows_mode=True)
        self.assertIn("whoami", out)
        self.assertIn("hostname", out)
        self.assertIn("cmd", out)  # 兜底保证有 shell，可写代码/调试
        for posix in ("echo", "pwd", "ls", "cat", "sleep", "date", "uptime", "uname"):
            self.assertNotIn(posix, out)

    def test_windows_all_posix_falls_back_and_adds_shell(self):
        out = normalize_allowlist(["echo", "ls", "cat"], windows_mode=True)
        self.assertIn("cmd", out)
        self.assertGreaterEqual(len(out), 2)  # 回退到 Windows 默认集

    def test_windows_keeps_custom_dev_commands(self):
        out = normalize_allowlist(["python", "git", "MYTOOL", "ls"], windows_mode=True)
        self.assertIn("python", out)
        self.assertIn("git", out)
        self.assertIn("mytool", out)  # 大小写归一便于比对
        self.assertIn("cmd", out)
        self.assertNotIn("ls", out)

    def test_windows_existing_shell_kept(self):
        out = normalize_allowlist(["powershell", "whoami"], windows_mode=True)
        self.assertIn("powershell", out)
        self.assertIn("whoami", out)
        # 已有 powershell 这条真 shell，不必再强塞 cmd
        self.assertTrue({"cmd", "powershell"} & out)

    def test_empty_uses_platform_default(self):
        self.assertEqual(normalize_allowlist([], windows_mode=False),
                         {"echo", "sleep", "ls", "pwd", "whoami", "cat", "false", "true"})
        self.assertIn("cmd", normalize_allowlist([], windows_mode=True))

    def test_executor_windows_mode_applies_normalization(self):
        ex = Executor(allowlist=["echo", "ls", "whoami"], windows_mode=True)
        self.assertIn("whoami", ex.allowlist)
        self.assertIn("cmd", ex.allowlist)
        self.assertNotIn("echo", ex.allowlist)


class TestHub(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.hub = Hub(self.db, "tok")

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def test_create_device_returns_one_time_key_and_hash_only(self):
        device_id, key = self.hub.create_device("Mac", "mac", 1)
        self.assertTrue(device_id.startswith("dev-"))
        self.assertEqual(len(key), 43)
        row = self.hub._q("SELECT key_hash FROM devices WHERE device_id=?", (device_id,))[0][0]
        self.assertNotEqual(row, key)
        self.assertEqual(len(row), 64)  # sha256 hex

    def test_auth_rejects_wrong_key(self):
        device_id, key = self.hub.create_device("Mac", "mac", 1)
        self.assertIsNone(self.hub.device_by_key(device_id, "wrong"))
        self.assertIsNotNone(self.hub.device_by_key(device_id, key))

    def test_disabled_device_cannot_auth(self):
        device_id, key = self.hub.create_device("Mac", "mac", 1)
        self.hub.set_device_enabled(device_id, False, 1)
        self.assertIsNone(self.hub.device_by_key(device_id, key))

    def test_reset_key_invalidates_old(self):
        device_id, key = self.hub.create_device("Mac", "mac", 1)
        new_key = self.hub.reset_device_key(device_id, 1)
        self.assertIsNone(self.hub.device_by_key(device_id, key))
        self.assertIsNotNone(self.hub.device_by_key(device_id, new_key))

    def test_task_lifecycle(self):
        device_id, _ = self.hub.create_device("Mac", "mac", 1)
        task_id = self.hub.create_task(device_id, "exec", {"command": ["echo", "hi"]}, 1)
        self.assertEqual(self.hub.get_task(task_id)["state"], "queued")
        self.hub.set_task_state(task_id, "done", result={"ok": True, "stdout": "hi\n"})
        task = self.hub.get_task(task_id)
        self.assertEqual(task["state"], "done")
        self.assertEqual(task["result"]["stdout"], "hi\n")

    def test_device_list_offline_by_default(self):
        self.hub.create_device("Mac", "mac", 1)
        devices = self.hub.list_devices(1)
        self.assertEqual(devices[0]["status"], "offline")


class TestDeviceDispatch(unittest.TestCase):
    """验证设备端复用 exec_server 的 dispatch 能正确执行工具。"""

    def setUp(self):
        self.server = McpServer(Executor(allowlist=["echo", "pwd"]))

    def _call(self, name, arguments):
        return self.server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })

    def _text(self, resp):
        return json.loads(resp["result"]["content"][0]["text"])

    def test_exec_allowed(self):
        r = self._text(self._call("exec", {"command": ["echo", "hi"]}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["stdout"].strip(), "hi")
        self.assertEqual(r["exit_code"], 0)

    def test_exec_rejected_not_in_allowlist(self):
        r = self._text(self._call("exec", {"command": ["rm", "/tmp/x"]}))
        self.assertFalse(r["ok"])
        self.assertIn("白名单", r["error"])

    def test_tools_list(self):
        resp = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["exec", "start_job", "query_job", "cancel_job"])


if __name__ == "__main__":
    unittest.main()
