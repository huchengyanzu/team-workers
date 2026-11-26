"""device-remote CLI 的 A/B 增量单测（仅 mock，不访问网络或真实 Downloads）。"""

from __future__ import annotations

import base64
import errno
import http.client
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
REMOTE_PATH = ROOT / "skill" / "device-remote" / "scripts" / "remote.py"
SPEC = importlib.util.spec_from_file_location("device_remote_cli", REMOTE_PATH)
remote = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(remote)


HOSTNAME = "EXAMPLE-HOST"
DEVICE = {
    "device_id": "dev-000000000000",
    "display_name": "公司的电脑",
    "platform": "windows",
    "status": "online",
    "enabled": True,
    "info": {"hostname": HOSTNAME},
}


def devices_response(*devices):
    return 200, {"devices": list(devices)}


def done_response(task_id, result):
    return 200, {"task_id": task_id, "state": "done", "result": result}


def mock_http_response(payload=b"", *, status=200, read_error=None):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.read.return_value = payload
    response.read.side_effect = read_error
    return response


def make_args(argv):
    args = remote.build_parser().parse_args(argv)
    args.base = "http://hub.test"
    args.token = "test-token"
    return args


class CliCase(unittest.TestCase):
    def invoke(self, args, command=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        value = None
        command = command or getattr(remote, "cmd_" + args.cmd)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                value = command(args)
            except SystemExit as exc:
                code = int(exc.code or 0)
        return value, code, stdout.getvalue(), stderr.getvalue()

    def json_receipt(self, args, command=None):
        args.json = True
        _, code, stdout, stderr = self.invoke(args, command)
        self.assertEqual(stderr, "")
        return json.loads(stdout), code


class RequestJsonTests(unittest.TestCase):
    def test_urlerror_is_connect_error(self):
        with mock.patch.object(remote.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.request_json("http://hub.test", "GET", "/api/devices", "token")
        self.assertEqual(caught.exception.kind, "connect_error")

    def test_urlerror_wrapping_socket_timeout_is_timeout(self):
        with mock.patch.object(remote.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError(socket.timeout("slow"))):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.request_json("http://hub.test", "GET", "/api/devices", "token")
        self.assertEqual(caught.exception.kind, "timeout")

    def test_socket_timeout_is_timeout(self):
        with mock.patch.object(remote.urllib.request, "urlopen",
                               side_effect=socket.timeout("slow")):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.request_json("http://hub.test", "GET", "/api/devices", "token")
        self.assertEqual(caught.exception.kind, "timeout")

    def test_response_read_failures_are_classified_without_token_leak(self):
        secret = "secret-control-token"
        cases = [
            (http.client.IncompleteRead(b'{"token":"' + secret.encode() + b'"'),
             "connect_error"),
            (ConnectionResetError(secret), "connect_error"),
            (http.client.RemoteDisconnected(secret), "connect_error"),
            (OSError(secret), "connect_error"),
            (socket.timeout(secret), "timeout"),
            (TimeoutError(secret), "timeout"),
        ]
        for error_response in (False, True):
            for read_error, kind in cases:
                with self.subTest(error_response=error_response,
                                  exception=type(read_error).__name__, kind=kind):
                    effect = mock_http_response(read_error=read_error)
                    if error_response:
                        effect = urllib.error.HTTPError(
                            "http://hub.test/api/devices", 500, "server error", {}, None)
                        effect.read = mock.Mock(side_effect=read_error)
                    opener = (mock.patch.object(remote.urllib.request, "urlopen",
                                                side_effect=effect)
                              if error_response else
                              mock.patch.object(remote.urllib.request, "urlopen",
                                                return_value=effect))
                    with opener:
                        with self.assertRaises(remote.RemoteError) as caught:
                            remote.request_json("http://hub.test", "GET", "/api/devices",
                                                secret)
                    self.assertEqual(caught.exception.kind, kind)
                    self.assertNotIn(secret, str(caught.exception))


class ResolveDeviceTests(unittest.TestCase):
    def test_exact_id_and_unique_name_resolve_same_device(self):
        with mock.patch.object(remote, "request_json", return_value=devices_response(DEVICE)):
            by_id = remote.resolve_device("base", "token", DEVICE["device_id"])
            by_name = remote.resolve_device("base", "token", DEVICE["display_name"],
                                            expect_hostname=HOSTNAME)
        self.assertEqual(by_id["device_id"], DEVICE["device_id"])
        self.assertEqual(by_name["device_id"], DEVICE["device_id"])

    def test_id_takes_precedence_over_duplicate_display_name(self):
        other = dict(DEVICE, device_id="dev-other")
        with mock.patch.object(remote, "request_json",
                               return_value=devices_response(DEVICE, other)):
            selected = remote.resolve_device("base", "token", DEVICE["device_id"])
        self.assertEqual(selected["device_id"], DEVICE["device_id"])

    def test_ambiguous_display_name_is_rejected(self):
        other = dict(DEVICE, device_id="dev-other")
        with mock.patch.object(remote, "request_json",
                               return_value=devices_response(DEVICE, other)):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.resolve_device("base", "token", DEVICE["display_name"])
        self.assertEqual(caught.exception.kind, "rejected")

    def test_unknown_offline_disabled_and_hostname_mismatch(self):
        cases = [
            (devices_response(), "not_found"),
            (devices_response(dict(DEVICE, status="offline")), "offline"),
            (devices_response(dict(DEVICE, enabled=False)), "permission_error"),
        ]
        for response, kind in cases:
            with self.subTest(kind=kind):
                with mock.patch.object(remote, "request_json", return_value=response):
                    with self.assertRaises(remote.RemoteError) as caught:
                        remote.resolve_device("base", "token", DEVICE["device_id"])
                self.assertEqual(caught.exception.kind, kind)
        with mock.patch.object(remote, "request_json", return_value=devices_response(DEVICE)):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.resolve_device("base", "token", DEVICE["device_id"],
                                      expect_hostname="WRONG-HOST")
        self.assertEqual(caught.exception.kind, "rejected")

    def test_malformed_device_list_is_protocol_error(self):
        with mock.patch.object(remote, "request_json", return_value=(200, {"devices": {}})):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.resolve_device("base", "token", DEVICE["device_id"])
        self.assertEqual(caught.exception.kind, "protocol_error")

    def test_http_status_mapping(self):
        expected = {
            400: "rejected",
            401: "auth_error",
            403: "permission_error",
            404: "not_found",
            409: "offline",
            429: "rate_limited",
            500: "http_error",
        }
        for status, kind in expected.items():
            with self.subTest(status=status):
                self.assertEqual(remote._http_error(status, {"error": "x"}).kind, kind)


class ExecReceiptTests(CliCase):
    def test_cmd_ver_receipt_has_raw_stdout_exit_code_target_and_timings(self):
        version = "Microsoft Windows [版本 10.0.26100.2315]\r\n"
        result = {
            "ok": True,
            "stdout": version,
            "stderr": "",
            "exit_code": 0,
            "duration_ms": 31,
            "timed_out": False,
        }
        args = make_args([
            "--json", "exec", "--expect-hostname", HOSTNAME,
            DEVICE["display_name"], "--", "cmd", "/c", "ver",
        ])
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE), done_response("task-abc123", result),
        ]) as request, mock.patch.object(remote.time, "monotonic", side_effect=[10.0, 10.125]):
            receipt, code = self.json_receipt(args)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["stdout"], version)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["duration_ms"], 31)
        self.assertEqual(receipt["elapsed_ms"], 125)
        self.assertEqual(receipt["target"], {
            "device_id": DEVICE["device_id"],
            "display_name": DEVICE["display_name"],
            "hostname": HOSTNAME,
        })
        self.assertEqual(receipt["command"], ["cmd", "/c", "ver"])
        self.assertEqual(request.call_args_list[1].kwargs["body"]["device_id"],
                         DEVICE["device_id"])

    def test_green_command_human_output_has_no_confirmation(self):
        result = {"ok": True, "stdout": "version\n", "stderr": "",
                  "exit_code": 0, "duration_ms": 2, "timed_out": False}
        args = make_args(["exec", DEVICE["device_id"], "--", "cmd", "/c", "ver"])
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE), done_response("task-green", result),
        ]), mock.patch("builtins.input") as prompt:
            _, code, stdout, stderr = self.invoke(args)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("version\n", stdout)
        self.assertIn("退出码：0", stdout)
        self.assertIn(HOSTNAME, stdout)
        prompt.assert_not_called()

    def test_nonzero_exit_is_command_failed_and_nonzero_process(self):
        result = {"ok": True, "stdout": "bad\n", "stderr": "",
                  "exit_code": 7, "duration_ms": 4, "timed_out": False}
        args = make_args(["--json", "exec", DEVICE["device_id"], "--", "false"])
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE), done_response("task-fail", result),
        ]):
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "command_failed")
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(code, 1)

    def test_result_ok_false_is_command_failed(self):
        args = make_args(["--json", "exec", DEVICE["device_id"], "--", "blocked"])
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE),
            done_response("task-blocked", {"ok": False, "error": "策略拒绝"}),
        ]):
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "command_failed")
        self.assertIn("策略拒绝", receipt["error"])
        self.assertEqual(code, 1)

    def test_state_timeout_and_execution_timeout_are_not_command_failed(self):
        args = make_args(["--json", "exec", DEVICE["device_id"], "--", "sleep", "30"])
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE),
            (200, {"task_id": "task-long", "state": "timeout"}),
        ]):
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "timeout")
        self.assertEqual(receipt["task_id"], "task-long")
        self.assertIn("task-long", receipt["error"])
        self.assertEqual(code, 1)

        result = {"ok": False, "stdout": "", "stderr": "", "exit_code": None,
                  "duration_ms": 10001, "timed_out": True, "error": "命令执行超时"}
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE), done_response("task-exec-timeout", result),
        ]):
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "timeout")
        self.assertEqual(receipt["duration_ms"], 10001)
        self.assertEqual(code, 1)

    def test_missing_or_illegal_exec_fields_are_protocol_errors(self):
        valid = {"ok": True, "stdout": "ok\n", "stderr": "",
                 "exit_code": 0, "duration_ms": 1, "timed_out": False}
        cases = []
        for field in ("stdout", "exit_code", "duration_ms"):
            item = dict(valid)
            del item[field]
            cases.append(item)
        cases.extend([
            dict(valid, stdout=1),
            dict(valid, exit_code=True),
            dict(valid, duration_ms=False),
            dict(valid, duration_ms=-1),
            dict(valid, ok=1),
        ])
        args = make_args(["--json", "exec", DEVICE["device_id"], "--", "echo", "ok"])
        for result in cases:
            with self.subTest(result=result):
                with mock.patch.object(remote, "request_json", side_effect=[
                    devices_response(DEVICE), done_response("task-protocol", result),
                ]):
                    receipt, code = self.json_receipt(args)
                self.assertEqual(receipt["status"], "protocol_error")
                self.assertEqual(code, 1)

    def test_http_and_transport_failures_keep_distinct_statuses(self):
        expected = {401: "auth_error", 403: "permission_error", 404: "not_found",
                    409: "offline", 429: "rate_limited", 500: "http_error"}
        args = make_args(["--json", "exec", DEVICE["device_id"], "--", "cmd", "/c", "ver"])
        for status, kind in expected.items():
            with self.subTest(status=status):
                with mock.patch.object(remote, "request_json", side_effect=[
                    devices_response(DEVICE), (status, {"error": "mock"}),
                ]):
                    receipt, code = self.json_receipt(args)
                self.assertEqual(receipt["status"], kind)
                self.assertIsNone(receipt["task_id"])
                self.assertEqual(code, 1)
        for kind in ("connect_error", "timeout"):
            with self.subTest(kind=kind):
                with mock.patch.object(remote, "request_json",
                                       side_effect=remote.RemoteError("mock", kind=kind)):
                    receipt, code = self.json_receipt(args)
                self.assertEqual(receipt["status"], kind)
                self.assertIsNone(receipt["task_id"])
                self.assertEqual(code, 1)

    def test_json_exec_incomplete_read_emits_connect_error_receipt(self):
        args = make_args(["--json", "exec", DEVICE["device_id"], "--", "echo", "ok"])
        devices = mock_http_response(json.dumps({"devices": [DEVICE]}).encode("utf-8"))
        interrupted = mock_http_response(
            read_error=http.client.IncompleteRead(b'{"task_id":"task-partial"'))
        with mock.patch.object(remote.urllib.request, "urlopen",
                               side_effect=[devices, interrupted]):
            _, code, stdout, stderr = self.invoke(args)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertNotIn("Traceback", stdout)
        self.assertNotIn(args.token, stdout)
        receipt = json.loads(stdout)
        self.assertEqual(receipt["status"], "connect_error")
        self.assertIsNone(receipt["task_id"])
        self.assertEqual(receipt["target"]["device_id"], DEVICE["device_id"])
        self.assertIn("读取服务端响应失败", receipt["error"])

    def test_expect_hostname_after_device_name_is_honored(self):
        result = {"ok": True, "stdout": "ok\n", "stderr": "",
                  "exit_code": 0, "duration_ms": 1, "timed_out": False}
        args = make_args(["--json", "exec", DEVICE["device_id"],
                          "--expect-hostname", HOSTNAME, "--", "cmd", "/c", "ver"])
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE), done_response("task-option", result),
        ]):
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["command"], ["cmd", "/c", "ver"])
        self.assertEqual(code, 0)

    def test_hostname_mismatch_and_missing_command_do_not_post(self):
        args = make_args(["--json", "exec", "--expect-hostname", "WRONG",
                          DEVICE["device_id"], "--", "cmd", "/c", "ver"])
        with mock.patch.object(remote, "request_json",
                               return_value=devices_response(DEVICE)) as request:
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(code, 1)

        args = make_args(["--json", "exec", DEVICE["device_id"]])
        with mock.patch.object(remote, "request_json") as request:
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "rejected")
        request.assert_not_called()
        self.assertEqual(code, 1)


class PullTests(CliCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.downloads = self.home / "Downloads"
        self.env_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.data = b"demo bytes\x00\xff\n"
        self.windows_source = r"C:\Users\user\Desktop\demo.txt"

    def args(self, *, json_mode=True, name=None, overwrite=False, path=None):
        argv = ["pull", "--expect-hostname", HOSTNAME, DEVICE["display_name"],
                path or self.windows_source]
        if name is not None:
            argv.extend(["--name", name])
        if overwrite:
            argv.append("--overwrite")
        if json_mode:
            argv.insert(0, "--json")
        return make_args(argv)

    def preflight(self, *, entries=None, ok=True, error=None, task_id="task-preflight"):
        result = {"ok": ok, "path": r"C:\Users\user\Desktop", "entries": entries}
        if error is not None:
            result["error"] = error
        return done_response(task_id, result)

    def read_result(self, *, data=None, size=None, path=None, content=None):
        data = self.data if data is None else data
        return {
            "ok": True,
            "path": path or self.windows_source,
            "size": len(data) if size is None else size,
            "encoding": "base64",
            "content": (base64.b64encode(data).decode("ascii")
                        if content is None else content),
        }

    def responses(self, preflight=None, read=None):
        entry = {"name": "demo.txt", "type": "file", "size": len(self.data)}
        return [
            devices_response(DEVICE),
            preflight or self.preflight(entries=[entry]),
            done_response("task-transfer", read or self.read_result()),
        ]

    def assert_no_parts(self):
        if self.downloads.exists():
            self.assertEqual(list(self.downloads.glob("*.part")), [])

    def test_windows_pull_default_name_and_byte_identical_receipt(self):
        args = self.args()
        with mock.patch.object(remote, "request_json", side_effect=self.responses()) as request:
            receipt, code = self.json_receipt(args)
        destination = self.downloads / "demo_task-transfer.txt"
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["task_id"], "task-transfer")
        self.assertEqual(receipt["preflight_task_id"], "task-preflight")
        self.assertEqual(receipt["source_path"], self.windows_source)
        self.assertEqual(receipt["destination_path"], str(destination.resolve()))
        self.assertEqual(receipt["expected_bytes"], len(self.data))
        self.assertEqual(receipt["actual_bytes"], len(self.data))
        self.assertEqual(destination.read_bytes(), self.data)
        self.assertEqual(request.call_args_list[1].kwargs["body"]["arguments"]["path"],
                         "C:/Users/user/Desktop")
        self.assertEqual(request.call_args_list[2].kwargs["body"]["arguments"]["path"],
                         self.windows_source)
        self.assert_no_parts()

    def test_posix_path_and_custom_name(self):
        entry = {"name": "demo.txt", "type": "file", "size": len(self.data)}
        source = "/Users/demo/Desktop/demo.txt"
        responses = [
            devices_response(DEVICE),
            done_response("task-pre", {"ok": True, "path": "/Users/demo/Desktop",
                                       "entries": [entry]}),
            done_response("task-copy", self.read_result(path=source)),
        ]
        args = self.args(name="renamed.bin", path=source)
        with mock.patch.object(remote, "request_json", side_effect=responses):
            receipt, code = self.json_receipt(args)
        self.assertEqual(code, 0)
        self.assertEqual((self.downloads / "renamed.bin").read_bytes(), self.data)
        self.assertEqual(receipt["source_path"], source)

    def test_remote_path_split_supports_both_styles(self):
        cases = {
            r"C:\Users\user\Desktop\demo.txt": ("C:/Users/user/Desktop", "demo.txt"),
            r"C:\demo.txt": ("C:/", "demo.txt"),
            "C:demo.txt": ("C:.", "demo.txt"),
            "/tmp/demo.txt": ("/tmp", "demo.txt"),
            "/demo.txt": ("/", "demo.txt"),
            "demo.txt": (".", "demo.txt"),
            r"\\server\share\demo.txt": ("//server/share", "demo.txt"),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(remote._split_remote_path(path), expected)

    def test_invalid_names_are_rejected_before_network(self):
        for name in ("", ".", "..", "../x", "a/b", r"a\b", "   "):
            with self.subTest(name=name):
                args = self.args(name=name)
                with mock.patch.object(remote, "request_json") as request:
                    receipt, code = self.json_receipt(args)
                self.assertEqual(receipt["status"], "rejected")
                self.assertEqual(code, 1)
                request.assert_not_called()
                self.assertFalse(self.downloads.exists())

    def test_preflight_missing_wrong_type_and_illegal_size_do_not_write(self):
        cases = [
            ([], "source_not_found"),
            ([{"name": "demo.txt", "type": "dir", "size": 1}], "source_not_found"),
            ([{"name": "demo.txt", "type": "file", "size": -1}], "protocol_error"),
            ([{"name": "demo.txt", "type": "file", "size": True}], "protocol_error"),
        ]
        for entries, status in cases:
            with self.subTest(entries=entries):
                with mock.patch.object(remote, "request_json", side_effect=self.responses(
                        preflight=self.preflight(entries=entries))):
                    receipt, code = self.json_receipt(self.args())
                self.assertEqual(receipt["status"], status)
                self.assertEqual(receipt["preflight_task_id"], "task-preflight")
                self.assertIsNone(receipt["task_id"])
                self.assertEqual(code, 1)
                self.assertFalse(self.downloads.exists())

    def test_strict_base64_size_and_source_change_failures_do_not_write(self):
        entry = {"name": "demo.txt", "type": "file", "size": len(self.data)}
        cases = [
            (self.read_result(content="%%%"), "protocol_error"),
            (self.read_result(size=len(self.data) + 1), "protocol_error"),
            (self.read_result(data=self.data + b"x"), "source_changed"),
            (self.read_result(path=r"C:\Users\user\Desktop\other.txt"), "source_changed"),
        ]
        for read, status in cases:
            with self.subTest(status=status, read=read):
                with mock.patch.object(remote, "request_json", side_effect=self.responses(
                        preflight=self.preflight(entries=[entry]), read=read)):
                    receipt, code = self.json_receipt(self.args())
                self.assertEqual(receipt["status"], status)
                self.assertEqual(code, 1)
                self.assertFalse(self.downloads.exists())

    def test_existing_file_noninteractive_conflict_preserves_original(self):
        self.downloads.mkdir()
        destination = self.downloads / "demo_task-transfer.txt"
        destination.write_bytes(b"original")
        args = self.args()
        with mock.patch.object(remote, "request_json", side_effect=self.responses()), \
                mock.patch.object(remote.sys.stdin, "isatty", return_value=False), \
                mock.patch("builtins.input") as prompt:
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "conflict")
        self.assertIn("--overwrite", receipt["error"])
        self.assertEqual(destination.read_bytes(), b"original")
        prompt.assert_not_called()
        self.assertEqual(code, 1)
        self.assert_no_parts()

    def test_overwrite_replaces_existing_file(self):
        self.downloads.mkdir()
        destination = self.downloads / "demo_task-transfer.txt"
        destination.write_bytes(b"original")
        with mock.patch.object(remote, "request_json", side_effect=self.responses()):
            receipt, code = self.json_receipt(self.args(overwrite=True))
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(destination.read_bytes(), self.data)
        self.assertEqual(code, 0)
        self.assert_no_parts()

    def test_interactive_only_exact_confirm_overwrites(self):
        self.downloads.mkdir()
        destination = self.downloads / "demo_task-transfer.txt"
        destination.write_bytes(b"original")
        args = self.args(json_mode=False)
        with mock.patch.object(remote, "request_json", side_effect=self.responses()), \
                mock.patch.object(remote.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="确认"):
            _, code, stdout, stderr = self.invoke(args)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("源：", stdout)
        self.assertIn("目标：", stdout)
        self.assertIn("大小：", stdout)
        self.assertEqual(destination.read_bytes(), self.data)

    def test_interactive_cancel_or_other_input_writes_nothing(self):
        self.downloads.mkdir()
        destination = self.downloads / "demo_task-transfer.txt"
        destination.write_bytes(b"original")
        for answer in ("取消", "yes", " 确认", ""):
            with self.subTest(answer=answer):
                destination.write_bytes(b"original")
                args = self.args(json_mode=False)
                with mock.patch.object(remote, "request_json", side_effect=self.responses()), \
                        mock.patch.object(remote.sys.stdin, "isatty", return_value=True), \
                        mock.patch("builtins.input", return_value=answer):
                    _, code, stdout, stderr = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "")
                self.assertIn("状态：cancel", stdout)
                self.assertEqual(destination.read_bytes(), b"original")
                self.assert_no_parts()

    def test_final_symlink_is_rejected_even_with_overwrite(self):
        self.downloads.mkdir()
        outside = self.home / "outside.txt"
        outside.write_bytes(b"outside")
        destination = self.downloads / "demo_task-transfer.txt"
        destination.symlink_to(outside)
        with mock.patch.object(remote, "request_json", side_effect=self.responses()):
            receipt, code = self.json_receipt(self.args(overwrite=True))
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertTrue(destination.is_symlink())
        self.assertEqual(code, 1)
        self.assert_no_parts()

    def test_hardlink_unsupported_falls_back_to_exclusive_create(self):
        with mock.patch.object(remote, "request_json", side_effect=self.responses()), \
                mock.patch.object(remote.os, "link",
                                  side_effect=OSError(errno.ENOTSUP, "unsupported")):
            receipt, code = self.json_receipt(self.args())
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual((self.downloads / "demo_task-transfer.txt").read_bytes(), self.data)
        self.assertEqual(code, 0)
        self.assert_no_parts()

    def test_publish_race_conflict_never_overwrites_and_cleans_temp(self):
        destination = self.downloads / "demo_task-transfer.txt"

        def race(source, target):
            Path(target).write_bytes(b"racer")
            raise FileExistsError(errno.EEXIST, "exists", str(target))

        self.downloads.mkdir()
        with mock.patch.object(remote, "request_json", side_effect=self.responses()), \
                mock.patch.object(remote.os, "link", side_effect=race):
            receipt, code = self.json_receipt(self.args())
        self.assertEqual(receipt["status"], "conflict")
        self.assertEqual(destination.read_bytes(), b"racer")
        self.assertEqual(code, 1)
        self.assert_no_parts()

    def test_fsync_failure_cleans_temp_and_leaves_no_destination(self):
        with mock.patch.object(remote, "request_json", side_effect=self.responses()), \
                mock.patch.object(remote.os, "fsync", side_effect=OSError("disk full")):
            receipt, code = self.json_receipt(self.args())
        self.assertEqual(receipt["status"], "io_error")
        self.assertEqual(code, 1)
        self.assertFalse((self.downloads / "demo_task-transfer.txt").exists())
        self.assert_no_parts()

    def test_preflight_and_read_timeout_have_correct_task_ids(self):
        args = self.args()
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE),
            (200, {"task_id": "task-pre-timeout", "state": "timeout"}),
        ]) as request:
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "timeout")
        self.assertIsNone(receipt["task_id"])
        self.assertEqual(receipt["preflight_task_id"], "task-pre-timeout")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(code, 1)

        entry = {"name": "demo.txt", "type": "file", "size": len(self.data)}
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE), self.preflight(entries=[entry]),
            (200, {"task_id": "task-read-timeout", "state": "timeout"}),
        ]):
            receipt, code = self.json_receipt(args)
        self.assertEqual(receipt["status"], "timeout")
        self.assertEqual(receipt["task_id"], "task-read-timeout")
        self.assertEqual(receipt["preflight_task_id"], "task-preflight")
        self.assertEqual(code, 1)
        self.assertFalse(self.downloads.exists())

    def test_pull_http_auth_error_is_not_source_not_found(self):
        with mock.patch.object(remote, "request_json", side_effect=[
            devices_response(DEVICE), (401, {"error": "invalid token"}),
        ]):
            receipt, code = self.json_receipt(self.args())
        self.assertEqual(receipt["status"], "auth_error")
        self.assertIsNone(receipt["task_id"])
        self.assertEqual(code, 1)
        self.assertFalse(self.downloads.exists())


if __name__ == "__main__":
    unittest.main()
