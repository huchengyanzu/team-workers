"""绑定策略单测（Task #19）：默认回环、显式局域网 + 强制 token、宽监听拒绝。"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from unittest import mock

PROTOTYPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROTOTYPE_DIR)
import exec_server  # noqa: E402


def _local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1，仅让内核选出口地址
        return sock.getsockname()[0]
    finally:
        sock.close()


def _env_without_bind(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("EXEC_BIND_HOST", "EXEC_TOKEN", "EXEC_TOKEN_FILE", "EXEC_TRANSPORT")}
    if extra:
        env.update(extra)
    return env


class BindPolicyTests(unittest.TestCase):
    def test_default_bind_is_loopback_and_non_loopback_refused(self):
        with mock.patch.dict(os.environ, _env_without_bind(), clear=True):
            ex = exec_server.Executor(allowlist={"echo"})
            srv = exec_server.make_http_server(exec_server.McpServer(ex), "tok", 0)
            self.assertEqual(srv.bind_host, "127.0.0.1")
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            try:
                ip = _local_ip()
                self.assertFalse(ip.startswith("127."))
                with self.assertRaises(OSError):
                    socket.create_connection((ip, srv.bind_port), timeout=2)
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)
                ex.shutdown()

    def test_wildcard_bind_is_still_refused(self):
        ex = exec_server.Executor(allowlist={"echo"})
        mcp = exec_server.McpServer(ex)
        try:
            for host in ("0.0.0.0", "::"):
                with self.assertRaises(ValueError) as ctx:
                    exec_server.make_http_server(mcp, "tok", 0, host=host)
                self.assertIn("宽监听", str(ctx.exception))
        finally:
            ex.shutdown()

    def test_lan_bind_without_token_is_refused(self):
        ex = exec_server.Executor(allowlist={"echo"})
        mcp = exec_server.McpServer(ex)
        try:
            with self.assertRaises(ValueError) as ctx:
                exec_server.make_http_server(mcp, "", 0, host=_local_ip())
            self.assertIn("token", str(ctx.exception))
        finally:
            ex.shutdown()

    def test_lan_bind_with_token_banner_and_health(self):
        ip = _local_ip()
        ex = exec_server.Executor(allowlist={"echo"})
        srv = exec_server.make_http_server(exec_server.McpServer(ex), "secret-token", 0, host=ip)
        self.assertEqual(srv.bind_host, ip)

        banner = exec_server.binding_banner(srv.bind_host, srv.bind_port)
        self.assertIn(ip, banner)
        self.assertIn("token", banner)
        self.assertIn("/health", banner)

        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen("http://%s:%d/health" % (ip, srv.bind_port), timeout=3) as resp:
                info = json.loads(resp.read())
            self.assertTrue(info["ok"])
            self.assertEqual(info["bind_host"], ip)
            self.assertFalse(info["loopback_only"])
            self.assertTrue(info["auth_required"])

            request = urllib.request.Request(
                "http://%s:%d/mcp" % (ip, srv.bind_port),
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)
            ex.shutdown()

    def test_main_refuses_lan_bind_without_token(self):
        ip = _local_ip()
        with mock.patch.dict(os.environ, _env_without_bind({"EXEC_BIND_HOST": ip}), clear=True):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = exec_server.main(["--http"])
        self.assertEqual(rc, 2)
        text = stderr.getvalue()
        self.assertIn(ip, text)
        self.assertIn("token", text)

    def test_main_refuses_wildcard_bind_with_clean_exit(self):
        with mock.patch.dict(os.environ, _env_without_bind(
                {"EXEC_BIND_HOST": "0.0.0.0", "EXEC_TOKEN": "tok"}), clear=True):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = exec_server.main(["--http"])
        self.assertEqual(rc, 2)
        text = stderr.getvalue()
        self.assertIn("宽监听", text)
        self.assertNotIn("Traceback", text)

    def test_resolve_bind_host(self):
        with mock.patch.dict(os.environ, _env_without_bind(), clear=True):
            self.assertEqual(exec_server.resolve_bind_host(), "127.0.0.1")
        with mock.patch.dict(os.environ, _env_without_bind({"EXEC_BIND_HOST": "192.168.1.10"}), clear=True):
            self.assertEqual(exec_server.resolve_bind_host(), "192.168.1.10")
            self.assertEqual(exec_server.resolve_bind_host("10.0.0.5"), "10.0.0.5")


if __name__ == "__main__":
    unittest.main()
