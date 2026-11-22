"""并发与频率限制单测（Task #23）。

覆盖：到顶拒绝且结构正确、到顶后恢复、默认配置下单条命令不受影响、
start_job 与 exec 共享预算且不排队、限流窗口与恢复、HTTP 429、stdio 层不做频率限制。
测试只用 echo / sleep 这类无害命令，只写系统临时目录。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

PROTOTYPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROTOTYPE_DIR)
import exec_server  # noqa: E402


def _wait_active(executor, target: int, timeout: float = 3.0) -> bool:
    """等并发占用数到达 target（exec 是同步阻塞的，用线程起第一条时靠轮询确认已进入执行）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if executor.concurrency_status()["active"] >= target:
            return True
        time.sleep(0.01)
    return False


class ConcurrencyLimitTests(unittest.TestCase):
    def test_exec_rejected_at_cap_with_structured_error_then_recovers(self):
        ex = exec_server.Executor(allowlist={"echo", "sleep"}, max_concurrency=1)
        first = threading.Thread(target=ex.exec_command, args=("sleep 0.6",))
        first.start()
        self.assertTrue(_wait_active(ex, 1), "第一条命令应已进入执行")

        started = time.monotonic()
        result = ex.exec_command("echo hi")
        elapsed = time.monotonic() - started

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "concurrency_limit")
        self.assertIn("并发上限", result["error"])
        self.assertIn("不排队", result["error"])
        self.assertEqual(result["limit"], 1)
        self.assertEqual(result["active"], 1)
        self.assertIn("timed_out", result)  # 与既有 exec 错误的字段形状保持一致
        self.assertLess(elapsed, 0.5, "拒绝必须立即返回，不能排队挂死")

        first.join(timeout=3)
        self.assertFalse(first.is_alive())
        recovered = ex.exec_command("echo ok")
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["stdout"].strip(), "ok")
        self.assertNotIn("error_code", recovered)

    def test_default_single_command_unaffected(self):
        ex = exec_server.Executor(allowlist={"echo"})  # 未配 EXEC_MAX_CONCURRENCY，用默认值
        self.assertEqual(ex.max_concurrency, exec_server.DEFAULT_MAX_CONCURRENCY)
        result = ex.exec_command("echo hello")
        self.assertTrue(result["ok"], result)
        self.assertNotIn("error_code", result)
        self.assertEqual(ex.concurrency_status()["active"], 0)

    def test_start_job_shares_cap_rejects_without_queue_then_recovers(self):
        ex = exec_server.Executor(allowlist={"echo", "sleep"}, max_concurrency=1)
        first = ex.start_job("sleep 0.5")
        self.assertTrue(first["ok"], first)

        rejected_exec = ex.exec_command("echo x")
        self.assertFalse(rejected_exec["ok"])
        self.assertEqual(rejected_exec["error_code"], "concurrency_limit")

        rejected_job = ex.start_job("echo y")
        self.assertFalse(rejected_job["ok"])
        self.assertEqual(rejected_job["error_code"], "concurrency_limit")
        self.assertIn("start_job 被拒绝", rejected_job["error"])
        self.assertNotIn("job_id", rejected_job)  # 拒绝不产生半成品任务

        # 任务进程退出后（下一次申请槽位时自动回收）恢复
        time.sleep(0.7)
        self.assertTrue(ex.start_job("echo z")["ok"])
        self.assertTrue(ex.exec_command("echo w")["ok"])

    def test_max_concurrency_env_config_and_clamping(self):
        with mock.patch.dict(os.environ, {"EXEC_MAX_CONCURRENCY": "2"}, clear=False):
            self.assertEqual(exec_server.Executor().max_concurrency, 2)
        with mock.patch.dict(os.environ, {"EXEC_MAX_CONCURRENCY": "0"}, clear=False):
            self.assertEqual(exec_server.Executor().max_concurrency, 1)  # 最小 1，不允许关成 0
        with mock.patch.dict(os.environ, {"EXEC_MAX_CONCURRENCY": "abc"}, clear=False):
            self.assertEqual(exec_server.Executor().max_concurrency,
                             exec_server.DEFAULT_MAX_CONCURRENCY)  # 非法值退回默认


class RateLimiterUnitTests(unittest.TestCase):
    def test_window_rejects_then_recovers_and_other_clients_unaffected(self):
        now = [0.0]
        limiter = exec_server.RateLimiter(max_requests=3, window_seconds=10,
                                          clock=lambda: now[0])
        for _ in range(3):
            allowed, retry = limiter.check("192.168.1.10")
            self.assertTrue(allowed)
            self.assertEqual(retry, 0.0)
        allowed, retry = limiter.check("192.168.1.10")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        self.assertLessEqual(retry, 10000)

        # 其他客户端不受影响（按客户端隔离）
        self.assertTrue(limiter.check("192.168.1.11")[0])
        # 窗口滑出后恢复
        now[0] += 10.5
        self.assertTrue(limiter.check("192.168.1.10")[0])

    def test_zero_max_requests_disables_rate_limit(self):
        limiter = exec_server.RateLimiter(max_requests=0, window_seconds=10,
                                          clock=lambda: 0.0)
        self.assertFalse(limiter.enabled)
        for _ in range(50):
            self.assertTrue(limiter.check("1.2.3.4")[0])


class HttpRateLimitTests(unittest.TestCase):
    def test_http_rate_limit_429_then_recovery_and_health_not_counted(self):
        env = {"EXEC_RATE_MAX": "2", "EXEC_RATE_WINDOW": "0.5"}
        with mock.patch.dict(os.environ, env, clear=False):
            executor = exec_server.Executor(allowlist={"echo"})
            server = exec_server.make_http_server(
                exec_server.McpServer(executor), token="unit-test-token", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.bind_port
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
            headers = {"Content-Type": "application/json",
                       "Authorization": "Bearer unit-test-token"}

            for _ in range(2):  # 窗口内前 2 次放行
                with urllib.request.urlopen(urllib.request.Request(
                        base + "/mcp", data=body, headers=headers), timeout=5) as resp:
                    self.assertEqual(resp.status, 200)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(urllib.request.Request(
                    base + "/mcp", data=body, headers=headers), timeout=5)
            err = ctx.exception
            self.assertEqual(err.code, 429)
            self.assertIsNotNone(err.headers.get("Retry-After"))
            payload = json.loads(err.read().decode("utf-8"))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error_code"], "rate_limit")
            self.assertIn("请求频率超限", payload["error"])
            self.assertGreater(payload["retry_after_ms"], 0)

            # GET /health 不计数、且能看到限流生效配置
            with urllib.request.urlopen(base + "/health", timeout=5) as resp:
                health = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(health["rate_limit"]["max"], 2)
            self.assertTrue(health["rate_limit"]["enabled"])
            self.assertEqual(health["concurrency"]["limit"],
                             exec_server.DEFAULT_MAX_CONCURRENCY)

            # 窗口滑出后恢复
            time.sleep(0.6)
            with urllib.request.urlopen(urllib.request.Request(
                    base + "/mcp", data=body, headers=headers), timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_stdio_dispatch_layer_has_no_rate_limit(self):
        # stdio 是单客户端本机场景：McpServer.dispatch（stdio 走的路径）不做频率限制，
        # 但并发上限仍然生效（由 ConcurrencyLimitTests 覆盖）。
        executor = exec_server.Executor(allowlist={"echo"})
        server = exec_server.McpServer(executor)
        for i in range(10):
            response = server.dispatch({
                "jsonrpc": "2.0", "id": i, "method": "tools/call",
                "params": {"name": "exec", "arguments": {"command": "echo tick"}},
            })
            self.assertIn("result", response)
            text = response["result"]["content"][0]["text"]
            self.assertTrue(json.loads(text)["ok"], text)


if __name__ == "__main__":
    unittest.main()
