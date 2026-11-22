"""exec-server 原型的单测（unittest，标准库，无需 pytest）。

覆盖：
- exec：正常返回、非 0 退出码透传、超时、超长输出截断、白名单拒绝
- job 三件套：start/query/cancel、job_id 稳定、取消后可查到 cancelled、不存在的 job 返回错误
- stdio 协议：子进程发 initialize / notifications/initialized / tools/list / tools/call 全链路

测试只用 echo / sleep / false / true 这类无害命令；job 的输出文件只写系统临时目录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

PROTOTYPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROTOTYPE_DIR)

import exec_server  # noqa: E402


class ExecToolTests(unittest.TestCase):
    def setUp(self):
        self.ex = exec_server.Executor(
            allowlist={"echo", "sleep", "false", "true", "cat", "ls", "pwd"},
            max_output_bytes=1024,
            default_timeout=5.0,
        )

    def tearDown(self):
        self.ex.shutdown()

    def test_exec_echo_returns_stdout_exit_code_and_duration(self):
        result = self.ex.exec_command(["echo", "hello"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "hello\n")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsInstance(result["duration_ms"], int)
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["truncated"]["stdout"])

    def test_exec_nonzero_exit_code_passthrough(self):
        result = self.ex.exec_command(["false"])
        self.assertTrue(result["ok"])  # 命令确实执行了，不算工具错误
        self.assertEqual(result["exit_code"], 1)

    def test_exec_timeout_returns_explicit_error_not_hang(self):
        started = time.monotonic()
        result = self.ex.exec_command(["sleep", "5"], timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertIn("超时", result["error"])
        self.assertLess(elapsed, 3.0, "超时后应立即返回，不能挂死")
        self.assertIsNone(result["exit_code"])

    def test_exec_long_output_is_truncated_with_marker(self):
        result = self.ex.exec_command(["echo", "x" * 5000], timeout=5.0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"]["stdout"])
        self.assertIn("输出已截断", result["stdout"])
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 1100)

    def test_exec_rejects_command_outside_allowlist(self):
        result = self.ex.exec_command(["rm", "-rf", "/tmp/should-not-run"])
        self.assertFalse(result["ok"])
        self.assertIn("白名单", result["error"])


class JobToolTests(unittest.TestCase):
    def setUp(self):
        self.ex = exec_server.Executor(
            allowlist={"echo", "sleep", "false", "true"},
            max_output_bytes=1024,
            default_timeout=5.0,
        )

    def tearDown(self):
        self.ex.shutdown()

    def _wait_until_not_running(self, job_id, deadline=5.0):
        end = time.monotonic() + deadline
        snapshot = self.ex.query_job(job_id)
        while snapshot["status"] == "running" and time.monotonic() < end:
            time.sleep(0.05)
            snapshot = self.ex.query_job(job_id)
        return snapshot

    def test_job_id_is_stable_and_cancel_is_visible_via_query(self):
        started = self.ex.start_job(["sleep", "3"])
        self.assertTrue(started["ok"])
        job_id = started["job_id"]
        self.assertTrue(job_id.startswith("job-"))

        running = self.ex.query_job(job_id)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["job_id"], job_id)

        cancelled = self.ex.cancel_job(job_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["job_id"], job_id)

        again = self.ex.query_job(job_id)
        self.assertEqual(again["status"], "cancelled")
        self.assertEqual(again["job_id"], job_id, "job_id 必须稳定，不能每次查询变新 id")
        self.assertIn("取消", again["note"])

    def test_job_finishes_and_exposes_output(self):
        started = self.ex.start_job(["echo", "job-done"])
        job_id = started["job_id"]
        snapshot = self._wait_until_not_running(job_id)
        self.assertEqual(snapshot["status"], "finished")
        self.assertEqual(snapshot["exit_code"], 0)
        self.assertIn("job-done", snapshot["stdout"])

    def test_unknown_job_returns_explicit_error_without_exception(self):
        snapshot = self.ex.query_job("job-does-not-exist")
        self.assertFalse(snapshot["ok"])
        self.assertIn("不存在", snapshot["error"])

        cancelled = self.ex.cancel_job("job-does-not-exist")
        self.assertFalse(cancelled["ok"])
        self.assertIn("不存在", cancelled["error"])

    def test_job_timeout_is_flagged(self):
        started = self.ex.start_job(["sleep", "5"], timeout=0.2)
        job_id = started["job_id"]
        snapshot = self._wait_until_not_running(job_id)
        self.assertEqual(snapshot["status"], "timeout")
        self.assertIn("超时", snapshot["note"])


class StdioProtocolTests(unittest.TestCase):
    """真起子进程，按 MCP stdio（换行分隔 JSON-RPC）走一遍。"""

    def test_initialize_list_and_call_over_stdio(self):
        env = os.environ.copy()
        env["EXEC_ALLOWLIST"] = "echo,sleep"
        env["EXEC_DEFAULT_TIMEOUT"] = "5"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05",
                        "clientInfo": {"name": "unittest", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "exec",
                        "arguments": {"command": ["echo", "hi"], "timeout": 5}}},
        ]
        payload = "\n".join(json.dumps(m) for m in messages) + "\n"
        proc = subprocess.Popen(
            [sys.executable, os.path.join(PROTOTYPE_DIR, "exec_server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout, stderr = proc.communicate(payload, timeout=20)
        self.assertEqual(proc.returncode, 0, "server 应正常退出；stderr=%s" % stderr[-500:])

        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        by_id = {r["id"]: r for r in responses}
        self.assertEqual(set(by_id), {1, 2, 3}, "通知消息不应产生回包")

        init = by_id[1]["result"]
        self.assertIn("protocolVersion", init)
        self.assertIn("tools", init["capabilities"])

        tool_names = {t["name"] for t in by_id[2]["result"]["tools"]}
        self.assertEqual(tool_names, {"exec", "start_job", "query_job", "cancel_job"})

        call = by_id[3]["result"]
        self.assertFalse(call["isError"])
        payload_text = call["content"][0]["text"]
        payload_json = json.loads(payload_text)
        self.assertEqual(payload_json["stdout"], "hi\n")
        self.assertEqual(payload_json["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
