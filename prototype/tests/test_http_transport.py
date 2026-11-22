"""exec-server HTTP 传输 + Bearer token 鉴权单测（Task #12）。

覆盖：
- 无 token / token 错误 → 401，且命令没有被执行（用 touch 建文件做副作用探针）
- 正确 token → initialize / tools/list / tools/call 三个交互跑通
- 通知类消息 → 202 无响应体
- 关键安全断言：服务端只绑 127.0.0.1 时，用本机非回环网卡地址去连必须连不上
- make_http_server：0.0.0.0 / :: 等宽监听地址一律无条件拒绝；非回环地址必须带 token 才允许
  （Task #19 后的新策略；带 token 的局域网绑定三道闸由 test_bind_policy.py 覆盖）

测试只写系统临时目录，不安装任何包、不访问外部网络（非回环探测只连本机自己的地址）。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid

PROTOTYPE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROTOTYPE_DIR)
import exec_server  # noqa: E402


def _local_non_loopback_ip() -> str:
    """取本机一个非回环 IPv4 地址（UDP connect 不会真的发包）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1，不可路由，仅用于让内核选本地出口地址
        return sock.getsockname()[0]
    finally:
        sock.close()


class HttpTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 测试里把 HTTP 访问日志静音，避免刷屏
        exec_server._HttpHandler.log_message = lambda *args, **kwargs: None
        cls.executor = exec_server.Executor(
            allowlist={"echo", "sleep", "touch"},
            max_output_bytes=1024,
            default_timeout=5.0,
        )
        cls.mcp = exec_server.McpServer(cls.executor)
        cls.token = "test-" + uuid.uuid4().hex
        cls.httpd = exec_server.make_http_server(cls.mcp, cls.token, 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmpdir = tempfile.mkdtemp(prefix="exec-server-http-test-")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.executor.shutdown()

    def _post(self, payload, token=None, path="/mcp", timeout=5):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _touch_payload(self, marker_path: str) -> dict:
        return {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "exec", "arguments": {"command": ["touch", marker_path]}},
        }

    def test_missing_token_returns_401_and_does_not_execute(self):
        marker = os.path.join(self.tmpdir, "no-token-%s" % uuid.uuid4().hex)
        status, body = self._post(self._touch_payload(marker))
        self.assertEqual(status, 401)
        self.assertIn("unauthorized", json.loads(body)["error"])
        self.assertFalse(os.path.exists(marker), "401 时不得执行任何命令（touch 不应创建文件）")

    def test_wrong_token_returns_401_and_does_not_execute(self):
        marker = os.path.join(self.tmpdir, "wrong-token-%s" % uuid.uuid4().hex)
        status, body = self._post(self._touch_payload(marker), token="wrong-" + uuid.uuid4().hex)
        self.assertEqual(status, 401)
        self.assertIn("unauthorized", json.loads(body)["error"])
        self.assertFalse(os.path.exists(marker), "token 错误时不得执行任何命令")

    def test_valid_token_initialize_list_and_call(self):
        status, body = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "clientInfo": {"name": "unittest", "version": "0"}},
        }, token=self.token)
        self.assertEqual(status, 200)
        init = json.loads(body)["result"]
        self.assertIn("protocolVersion", init)
        self.assertIn("tools", init["capabilities"])

        status, body = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token=self.token)
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in json.loads(body)["result"]["tools"]}
        self.assertEqual(names, {"exec", "start_job", "query_job", "cancel_job"})

        status, body = self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "exec", "arguments": {"command": ["echo", "hi"]}},
        }, token=self.token)
        self.assertEqual(status, 200)
        call = json.loads(body)["result"]
        self.assertFalse(call["isError"])
        payload = json.loads(call["content"][0]["text"])
        self.assertEqual(payload["stdout"], "hi\n")
        self.assertEqual(payload["exit_code"], 0)

    def test_notification_returns_202_without_body(self):
        status, body = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, token=self.token)
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")

    def test_loopback_only_non_loopback_address_must_refuse(self):
        local_ip = _local_non_loopback_ip()
        self.assertFalse(local_ip.startswith("127."), "拿到的应是本机非回环地址，实际是 %s" % local_ip)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            with self.assertRaises(OSError, msg="绑定 127.0.0.1 后，非回环地址 %s 不应能连上" % local_ip):
                sock.connect((local_ip, self.port))

    def test_make_http_server_refuses_wildcard_bind(self):
        with self.assertRaises(ValueError):
            exec_server.make_http_server(self.mcp, self.token, 0, host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
