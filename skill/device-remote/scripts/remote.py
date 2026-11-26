#!/usr/bin/env python3
"""远程设备控制 —— 控制端命令行工具（纯 Python 标准库）。

通过远程控制服务端（hub）的 REST API 操作受控设备，仅用 urllib 发请求，
不依赖第三方库，不直连设备、不读取任何私钥文件。

鉴权：所有请求头带 X-Control-Token。配置优先级（高→低）：
  --token / --base 参数 > 环境变量 REMOTE_TOKEN / REMOTE_SERVER >
  脚本同目录 config.local.json > 默认值。
config.local.json 形如 {"server":"https://hub.example.com","token":"<token>"}，
用于 exec 子进程不共享环境变量的场景（如 myclaw），配置一次后免参调用；该文件勿提交版本库。
服务端地址由 --base / REMOTE_SERVER / config.local.json 提供，文档示例为 https://hub.example.com；
请求路径 = base + /api/...。HTTPS 依赖 Python/系统 OpenSSL：Windows 上
若出现 ConnectionResetError [WinError 64/10054] / ssl.SSLError，优先换 Python 3.13+
(OpenSSL 3.5.x) 或 Anaconda Python；不要用 OpenSSL 3.0.x (libssl-3.dll)。

用法示例：
    python3 remote.py list
    python3 remote.py exec --expect-hostname HOST dev-xxxx -- cmd /c ver
    python3 remote.py --json exec dev-xxxx -- echo hi
    python3 remote.py pull dev-xxxx 'C:\\Users\\demo\\Desktop\\demo.txt'
    python3 remote.py pull dev-xxxx /tmp/demo.txt --name demo.txt --overwrite
    python3 remote.py task task-xxxxxxxx
    python3 remote.py create "客厅 Mac mini" macos
    python3 remote.py --json list
    python3 remote.py --base http://127.0.0.1:8766 --token <t> list
"""
from __future__ import annotations

import argparse
import base64
import binascii
import errno
import http.client
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 服务端根地址没有内置默认值：由 --base / REMOTE_SERVER / config.local.json 提供。
DEFAULT_BASE = ""
# 服务端同步等待任务结果的超时为 15s，客户端 HTTP 超时放宽到 30s，
# 以便能收到服务端返回的 {state: "timeout"} 响应而不是自己先超时。
HTTP_TIMEOUT = 30

# 工具白名单与服务端 HttpApi.TOOLS 保持一致。
TOOLS = ("exec", "start_job", "query_job", "cancel_job",
         "file_read", "file_write", "file_list")


class RemoteError(Exception):
    """面向用户的可读错误；kind 用于统一回执中的失败分类。"""

    def __init__(self, message: str, kind: str = "protocol_error") -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# 低层 HTTP
# ---------------------------------------------------------------------------
def build_url(base: str, path: str) -> str:
    """拼接 base 与 /api/... 路径，去掉 base 末尾多余的斜杠。"""
    return base.rstrip("/") + path


def request_json(
    base: str,
    method: str,
    path: str,
    token: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = HTTP_TIMEOUT,
) -> Tuple[int, Any]:
    """发一个 JSON 请求，返回 (HTTP 状态码, 解析后的 JSON 对象)。

    401/409/404 等 HTTP 错误不抛异常，而是把状态码与响应体一起返回，
    交由上层 handle_http_error 统一给出人类可读提示。
    """
    url = build_url(base, path)
    headers: Dict[str, str] = {
        "X-Control-Token": token,
        "Accept": "application/json",
    }
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        # HTTP 层的 4xx/5xx 仍带响应体，读出来交给上层显示。
        try:
            raw = exc.read().decode("utf-8", "replace")
        except (socket.timeout, TimeoutError):
            raise RemoteError("请求超时：读取服务端错误响应超时", kind="timeout")
        except (http.client.IncompleteRead, ConnectionResetError, OSError):
            raise RemoteError("网络错误：读取服务端错误响应失败", kind="connect_error")
        status = int(exc.code)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise RemoteError("请求超时：服务端无响应或网络过慢", kind="timeout")
        raise RemoteError("网络错误：无法连接服务端（%s）" % exc.reason,
                          kind="connect_error")
    except (socket.timeout, TimeoutError):
        raise RemoteError("请求超时：服务端无响应或网络过慢", kind="timeout")
    except (http.client.IncompleteRead, ConnectionResetError, OSError):
        raise RemoteError("网络错误：读取服务端响应失败", kind="connect_error")
    try:
        obj: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        obj = {"raw": raw}
    return status, obj


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------
def dump_json(obj: Any) -> None:
    """--json 模式：打印原始 JSON（保留中文，缩进可读）。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def fail(msg: str) -> None:
    """向 stderr 打印错误并以非零码退出。"""
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def handle_http_error(status: int, obj: Any) -> None:
    """把常见 HTTP 状态码翻译成明确提示；不会返回（直接退出）。"""
    err = ""
    if isinstance(obj, dict):
        err = str(obj.get("error", ""))
    suffix = ("：" + err) if err else ""
    if status == 401:
        fail("控制 token 错误或无效（HTTP 401）：请检查 REMOTE_TOKEN 环境变量或 --token 参数。")
    if status == 403:
        fail("无权限（HTTP 403）" + suffix)
    if status == 404:
        fail("资源不存在（HTTP 404）" + suffix)
    if status == 409:
        # 设备离线在服务端以 409 返回，必须明确回「离线」，不允许静默。
        fail("离线：设备当前离线（HTTP 409）" + suffix)
    if status == 400:
        fail("请求被拒绝（HTTP 400）" + suffix + "（tool 须在白名单 %s 内）" % ",".join(TOOLS))
    if status == 429:
        fail("请求过于频繁（HTTP 429）" + suffix)
    fail("服务端错误（HTTP %d）%s" % (status, suffix))


def ok_or_error(status: int, obj: Any) -> Any:
    """若状态码非 2xx 则退出；否则返回 obj。"""
    if status < 200 or status >= 300:
        handle_http_error(status, obj)
    return obj


# ---------------------------------------------------------------------------
# 设备解析与统一回执（exec / pull 共用）
# ---------------------------------------------------------------------------
def _error_text(obj: Any) -> str:
    """从服务端响应中提取可读错误，不返回空字符串。"""
    if isinstance(obj, dict):
        value = obj.get("error")
        if value not in (None, ""):
            return str(value)
    return ""


def _http_error(status: int, obj: Any) -> RemoteError:
    """把 HTTP 失败映射为回执 status，同时保留服务端错误原因。"""
    detail = _error_text(obj)
    suffix = ("：" + detail) if detail else ""
    kinds = {
        400: "rejected",
        401: "auth_error",
        403: "permission_error",
        404: "not_found",
        409: "offline",
        429: "rate_limited",
    }
    kind = kinds.get(status, "http_error")
    messages = {
        400: "请求被拒绝（HTTP 400）",
        401: "控制 token 错误或无效（HTTP 401）",
        403: "无权限（HTTP 403）",
        404: "资源不存在（HTTP 404）",
        409: "离线：设备当前离线（HTTP 409）",
        429: "请求过于频繁（HTTP 429）",
    }
    message = messages.get(status, "服务端错误（HTTP %d）" % status)
    return RemoteError(message + suffix, kind=kind)


def _device_target(device: Dict[str, Any]) -> Dict[str, Any]:
    """生成回执中的固定目标标识，不携带 token 或设备私钥。"""
    info = device.get("info")
    hostname = info.get("hostname") if isinstance(info, dict) else None
    return {
        "device_id": device.get("device_id"),
        "display_name": device.get("display_name"),
        "hostname": hostname if isinstance(hostname, str) else None,
    }


def _initial_target(selector: str) -> Dict[str, Any]:
    """目标尚未解析时的回执占位；未知字段统一为 null。"""
    return {"device_id": None, "display_name": None, "hostname": None}


def resolve_device(
    base: str,
    token: str,
    selector: str,
    expect_hostname: Optional[str] = None,
) -> Dict[str, Any]:
    """按精确 device_id 或唯一 display_name 解析在线且启用的设备。

    设备 ID 优先于显示名；显示名匹配到多台设备时拒绝下发。
    expect_hostname 非空时必须与 info.hostname 精确一致。
    """
    if not isinstance(selector, str) or not selector:
        raise RemoteError("设备 ID 或显示名不能为空", kind="rejected")
    status, obj = request_json(base, "GET", "/api/devices", token)
    if status < 200 or status >= 300:
        raise _http_error(status, obj)
    if not isinstance(obj, dict) or not isinstance(obj.get("devices"), list):
        raise RemoteError("设备列表响应格式错误：缺少 devices 数组", kind="protocol_error")

    devices: List[Dict[str, Any]] = []
    for item in obj["devices"]:
        if not isinstance(item, dict):
            raise RemoteError("设备列表响应格式错误：设备项不是对象", kind="protocol_error")
        device_id = item.get("device_id")
        display_name = item.get("display_name")
        if not isinstance(device_id, str) or not device_id or not isinstance(display_name, str):
            raise RemoteError("设备列表响应格式错误：device_id/display_name 非法",
                              kind="protocol_error")
        devices.append(item)

    by_id = [device for device in devices if device["device_id"] == selector]
    if len(by_id) == 1:
        selected = by_id[0]
    elif len(by_id) > 1:
        raise RemoteError("设备 ID 不唯一：%s" % selector, kind="protocol_error")
    else:
        by_name = [device for device in devices if device["display_name"] == selector]
        if not by_name:
            raise RemoteError("设备不存在：%s" % selector, kind="not_found")
        if len(by_name) > 1:
            raise RemoteError("显示名匹配到多台设备，请改用 device_id：%s" % selector,
                              kind="rejected")
        selected = by_name[0]

    if selected.get("status") != "online":
        raise RemoteError("离线：设备 %s 当前离线" % selected["device_id"], kind="offline")
    if selected.get("enabled") is not True:
        raise RemoteError("设备 %s 已被禁用，未下发任务" % selected["device_id"],
                          kind="permission_error")
    info = selected.get("info")
    if info is not None and not isinstance(info, dict):
        raise RemoteError("设备信息响应格式错误：info 不是对象", kind="protocol_error")
    hostname = info.get("hostname") if isinstance(info, dict) else None
    if hostname is not None and not isinstance(hostname, str):
        raise RemoteError("设备信息响应格式错误：hostname 不是字符串", kind="protocol_error")
    if expect_hostname is not None and hostname != expect_hostname:
        raise RemoteError(
            "目标主机名不匹配：期望 %s，实际 %s；未下发任务"
            % (expect_hostname, hostname if hostname is not None else "未知"),
            kind="rejected",
        )
    return selected


def _task_id(obj: Any, *, required: bool = True) -> Optional[str]:
    """校验任务编号；缺失且非必需时返回 None，绝不用空字符串冒充编号。"""
    value = obj.get("task_id") if isinstance(obj, dict) else None
    if value is None and not required:
        return None
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(ch in value for ch in ("/", "\\", "\x00"))):
        raise RemoteError("任务响应缺少合法 task_id", kind="protocol_error")
    return value


def _task_result(
    obj: Any,
    receipt: Dict[str, Any],
    id_field: str,
    phase: str,
) -> Dict[str, Any]:
    """要求同步任务返回 state=done，并把 timeout 与协议错误分开。"""
    if not isinstance(obj, dict):
        raise RemoteError("%s响应不是 JSON 对象" % phase, kind="protocol_error")
    state = obj.get("state")
    task_id = receipt.get(id_field)
    if state == "timeout":
        raise RemoteError(
            "%s超时；task_id=%s，可用 task 子命令查询" % (phase, task_id),
            kind="timeout",
        )
    if state != "done":
        detail = _error_text(obj)
        raise RemoteError(
            "%s响应状态异常：%s%s" % (phase, state, ("：" + detail) if detail else ""),
            kind="protocol_error",
        )
    result = obj.get("result")
    if not isinstance(result, dict):
        raise RemoteError("%s响应缺少合法 result 对象" % phase, kind="protocol_error")
    return result


def _elapsed_ms(started: float) -> int:
    """用 monotonic 计算从客户端发起到完整回执的毫秒数。"""
    return max(0, int((time.monotonic() - started) * 1000))


def _print_receipt(receipt: Dict[str, Any]) -> None:
    """打印统一人类可读回执；stdout 原文不做裁剪或重写。"""
    target = receipt.get("target") or {}
    print("task_id：%s" % (receipt.get("task_id") or "无（尚未取得）"))
    if receipt.get("preflight_task_id") is not None:
        print("preflight_task_id：%s" % receipt["preflight_task_id"])
    print("目标：%s / %s / hostname=%s" % (
        target.get("device_id") or "未解析",
        target.get("display_name") or "未命名",
        target.get("hostname") or "未知",
    ))
    print("状态：%s" % receipt.get("status"))
    if "command" in receipt:
        print("命令：%s" % json.dumps(receipt.get("command"), ensure_ascii=False))
    if "source_path" in receipt:
        print("源路径：%s" % (receipt.get("source_path") or "未确定"))
        print("目标路径：%s" % (receipt.get("destination_path") or "未确定"))
        print("字节数：预期 %s / 实际 %s" % (
            receipt.get("expected_bytes") if receipt.get("expected_bytes") is not None else "未知",
            receipt.get("actual_bytes") if receipt.get("actual_bytes") is not None else "未知",
        ))
        print("端到端耗时：%s ms" % receipt.get("elapsed_ms"))
    else:
        stdout = receipt.get("stdout")
        stderr = receipt.get("stderr")
        if stdout is not None:
            print("--- stdout ---")
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr is not None:
            print("--- stderr ---")
            print(stderr, end="" if stderr.endswith("\n") else "\n")
        print("退出码：%s" % (
            receipt.get("exit_code") if receipt.get("exit_code") is not None else "无（未获得）"))
        print("耗时：设备 %s ms / 端到端 %s ms" % (
            receipt.get("duration_ms") if receipt.get("duration_ms") is not None else "未知",
            receipt.get("elapsed_ms"),
        ))
        if receipt.get("status") == "timeout" and receipt.get("task_id"):
            print("可用 task 子命令查询：python3 %s task %s" % (
                sys.argv[0], receipt["task_id"]))
    if receipt.get("error"):
        print("错误：%s" % receipt["error"])


def _emit_receipt(receipt: Dict[str, Any], json_mode: bool) -> Dict[str, Any]:
    """按统一模式输出回执；任何非 ok 状态都以非零码结束。"""
    if json_mode:
        dump_json(receipt)
    else:
        _print_receipt(receipt)
    if receipt.get("status") != "ok":
        raise SystemExit(1)
    return receipt


def _apply_exec_result(receipt: Dict[str, Any], result: Any) -> None:
    """校验 exec 结果字段并填充回执，区分命令失败、超时与协议错误。"""
    if not isinstance(result, dict):
        raise RemoteError("exec 结果不是对象", kind="protocol_error")
    ok = result.get("ok")
    if not isinstance(ok, bool):
        raise RemoteError("exec 结果缺少布尔 ok 字段", kind="protocol_error")

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise RemoteError("exec 结果 stdout/stderr 必须是字符串", kind="protocol_error")
    if ok and "stdout" not in result:
        raise RemoteError("exec 结果缺少 stdout", kind="protocol_error")

    exit_code = result.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise RemoteError("exec 结果 exit_code 必须是整数", kind="protocol_error")
    if ok and ("exit_code" not in result or exit_code is None):
        raise RemoteError("exec 结果缺少整数 exit_code", kind="protocol_error")

    duration_ms = result.get("duration_ms")
    if duration_ms is not None and (
        not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0
    ):
        raise RemoteError("exec 结果 duration_ms 必须是非负整数", kind="protocol_error")
    if ok and duration_ms is None:
        raise RemoteError("exec 结果缺少 duration_ms", kind="protocol_error")

    timed_out = result.get("timed_out", False)
    if not isinstance(timed_out, bool):
        raise RemoteError("exec 结果 timed_out 必须是布尔值", kind="protocol_error")
    if timed_out and duration_ms is None:
        raise RemoteError("exec 超时结果缺少 duration_ms", kind="protocol_error")

    receipt["stdout"] = stdout
    receipt["stderr"] = stderr
    receipt["exit_code"] = exit_code
    receipt["duration_ms"] = duration_ms
    error = result.get("error")
    if error not in (None, ""):
        receipt["error"] = str(error)
    if timed_out:
        receipt["status"] = "timeout"
        if not receipt.get("error"):
            receipt["error"] = "命令执行超时"
    elif not ok:
        receipt["status"] = "command_failed"
        if not receipt.get("error"):
            receipt["error"] = "设备报告命令执行失败"
    elif exit_code != 0:
        receipt["status"] = "command_failed"
    else:
        receipt["status"] = "ok"


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> None:
    """GET /api/devices：列出设备 ID / 名称 / 平台 / 状态 / 最后心跳。"""
    status, obj = request_json(args.base, "GET", "/api/devices", args.token)
    ok_or_error(status, obj)
    if args.json:
        dump_json(obj)
        return
    devices = obj.get("devices", []) if isinstance(obj, dict) else []
    if not devices:
        print("（无设备）")
        return
    print(f"{'ID':<18} {'名称':<20} {'平台':<10} {'状态':<12} {'最后心跳'}")
    for d in devices:
        did = str(d.get("device_id", ""))
        name = str(d.get("display_name", ""))
        plat = str(d.get("platform", ""))
        state = str(d.get("status", "offline"))
        # 离线必须明确写「离线」，不静默、不模糊。
        state_cn = "在线" if state == "online" else "离线"
        seen = str(d.get("last_seen", ""))
        enabled = "启用" if d.get("enabled") else "禁用"
        print(f"{did:<18} {name:<20} {plat:<10} {state_cn}({state})/{enabled:<6} {seen}")


def print_result(result: Any) -> None:
    """打印 exec / 任务结果：优先 stdout / stderr / 退出码，否则打印原始 JSON。"""
    if not isinstance(result, dict):
        print("结果：" + str(result))
        return
    stdout = result.get("stdout")
    stderr = result.get("stderr")
    exit_code = result.get("exit_code", "未知")
    if stdout is not None or exit_code != "未知":
        if stdout is not None:
            text = str(stdout)
            print("--- stdout ---")
            print(text, end="" if text.endswith("\n") else "\n")
        if stderr:
            print("--- stderr ---")
            print(str(stderr), end="" if str(stderr).endswith("\n") else "\n")
        print("退出码：%s" % exit_code)
        truncated = result.get("truncated")
        if truncated is True or (isinstance(truncated, dict) and any(truncated.values())):
            print("提示：输出已被截断 %s" % truncated)
    else:
        # 非 exec 工具（start_job/query_job/cancel_job）结果结构不同，原样打印。
        dump_json(result)
    if result.get("ok") is False and result.get("error"):
        print("错误：%s" % result["error"])


def cmd_exec(args: argparse.Namespace) -> Dict[str, Any]:
    """解析目标并下发 exec，返回带机器、输出、退出码和双耗时的统一回执。"""
    started = time.monotonic()
    command: List[str] = list(args.command or [])
    # 兼容不同 Python 版本：若分隔符 "--" 被保留，去掉它。
    if command and command[0] == "--":
        command = command[1:]
    option_error = None
    # argparse.REMAINDER 会吞掉设备名之后的选项；兼容常见的
    # `exec dev-xxxx --expect-hostname HOST -- cmd ...` 写法。
    if (command and command[0] == "--expect-hostname"
            and getattr(args, "expect_hostname", None) is None):
        if len(command) < 2:
            option_error = "--expect-hostname 缺少主机名"
            command = []
        else:
            args.expect_hostname = command[1]
            command = command[2:]
            if command and command[0] == "--":
                command = command[1:]
    receipt: Dict[str, Any] = {
        "task_id": None,
        "target": _initial_target(args.device_id),
        "command": command,
        "status": "protocol_error",
        "stdout": None,
        "stderr": None,
        "exit_code": None,
        "duration_ms": None,
        "elapsed_ms": 0,
        "error": None,
    }
    try:
        if option_error:
            raise RemoteError(option_error, kind="rejected")
        if not command:
            raise RemoteError(
                "缺少要执行的命令：用法 remote.py exec [--expect-hostname HOST] "
                "<device_id_or_name> -- <命令> [参数...]",
                kind="rejected",
            )
        device = resolve_device(args.base, args.token, args.device_id,
                                getattr(args, "expect_hostname", None))
        receipt["target"] = _device_target(device)
        body = {
            "device_id": device["device_id"],
            "tool": "exec",
            "arguments": {"command": command},
        }
        status, obj = request_json(args.base, "POST", "/api/tasks", args.token, body=body)
        receipt["task_id"] = _task_id(obj, required=False)
        if status < 200 or status >= 300:
            raise _http_error(status, obj)
        receipt["task_id"] = _task_id(obj)
        result = _task_result(obj, receipt, "task_id", "exec")
        _apply_exec_result(receipt, result)
    except RemoteError as exc:
        receipt["status"] = exc.kind
        receipt["error"] = str(exc)
    receipt["elapsed_ms"] = _elapsed_ms(started)
    return _emit_receipt(receipt, bool(args.json))


def cmd_task(args: argparse.Namespace) -> None:
    """GET /api/tasks/{id}：查询长任务的状态与结果。"""
    tid = urllib.parse.quote(args.task_id, safe="")
    status, obj = request_json(args.base, "GET", "/api/tasks/" + tid, args.token)
    ok_or_error(status, obj)
    if args.json:
        dump_json(obj)
        return
    state = str(obj.get("state", ""))
    print("task_id：%s  设备：%s  工具：%s  状态：%s" % (
        obj.get("task_id", ""), obj.get("device_id", ""), obj.get("tool", ""), state))
    if obj.get("result") is not None:
        print_result(obj["result"])
    if obj.get("error"):
        print("错误：%s" % obj["error"])
    if state in ("queued", "dispatched"):
        print("任务尚未完成，请稍后重试本命令。")


def cmd_create(args: argparse.Namespace) -> None:
    """POST /api/admin/devices：创建设备，返回一次性私钥。"""
    body = {"display_name": args.display_name, "platform": args.platform}
    status, obj = request_json(args.base, "POST", "/api/admin/devices", args.token, body=body)
    ok_or_error(status, obj)
    if args.json:
        dump_json(obj)
        return
    print("设备已创建。")
    print("device_id：%s" % obj.get("device_id", ""))
    # 私钥是一次性的：只在这里显示，不落盘、不写日志、无法再次取回。
    print("一次性私钥：%s" % obj.get("key", ""))
    print("注意：此私钥只显示这一次，请立即保存到设备端配置；丢失请用 reset-key 重置。")


def cmd_enable(args: argparse.Namespace, enabled: bool) -> None:
    """POST /api/admin/devices/{id}/enabled：启用/禁用设备。"""
    path = "/api/admin/devices/%s/enabled" % urllib.parse.quote(args.device_id, safe="")
    status, obj = request_json(args.base, "POST", path, args.token, body={"enabled": enabled})
    ok_or_error(status, obj)
    if args.json:
        dump_json(obj)
        return
    print("设备 %s 已%s。" % (args.device_id, "启用" if enabled else "禁用（长连接会被断开）"))


def cmd_reset_key(args: argparse.Namespace) -> None:
    """POST /api/admin/devices/{id}/key：重置设备私钥，返回一次性新私钥。"""
    path = "/api/admin/devices/%s/key" % urllib.parse.quote(args.device_id, safe="")
    status, obj = request_json(args.base, "POST", path, args.token)
    ok_or_error(status, obj)
    if args.json:
        dump_json(obj)
        return
    print("设备 %s 的私钥已重置。" % args.device_id)
    # 新私钥同样只显示一次；旧私钥会立即失效。
    print("一次性新私钥：%s" % obj.get("key", ""))
    print("注意：此私钥只显示这一次，请立即更新设备端配置；旧私钥已失效。")


def cmd_delete(args: argparse.Namespace) -> None:
    """DELETE /api/admin/devices/{id}：删除设备（需 --yes 确认，不可逆）。"""
    if not args.yes:
        fail("删除设备不可逆，必须显式加 --yes 确认：remote.py delete <device_id> --yes")
    path = "/api/admin/devices/%s" % urllib.parse.quote(args.device_id, safe="")
    status, obj = request_json(args.base, "DELETE", path, args.token)
    ok_or_error(status, obj)
    if args.json:
        dump_json(obj)
        return
    print("设备 %s 已删除。" % args.device_id)


def cmd_file(args: argparse.Namespace) -> None:
    """文件工具：file-read / file-write / file-list，经 /api/tasks 下发到设备端。

    读取返回 base64，写入需 base64 内容；--overwrite 显式确认才允许覆盖。
    """
    if args.file_op == "file_read":
        arguments: Dict[str, Any] = {"path": args.path}
    elif args.file_op == "file_write":
        try:
            with open(args.local_file, "rb") as fh:
                content_b64 = __import__("base64").b64encode(fh.read()).decode("ascii")
        except OSError as exc:
            fail("读取本地文件失败：%s" % exc)
        arguments = {"path": args.path, "content_b64": content_b64,
                     "overwrite": bool(args.overwrite)}
    else:  # file_list
        arguments = {"path": args.path}
    body = {"device_id": args.device_id, "tool": args.file_op, "arguments": arguments}
    status, obj = request_json(args.base, "POST", "/api/tasks", args.token, body=body)
    if args.json:
        dump_json(obj)
        if status < 200 or status >= 300:
            raise SystemExit(1)
        return
    ok_or_error(status, obj)
    state = str(obj.get("state", ""))
    if state == "timeout":
        print("文件操作已下发，15 秒内未返回。task_id：%s（用 task 子命令查询）" % obj.get("task_id"))
        return
    result = obj.get("result") or {}
    if not result.get("ok"):
        print("失败：%s" % result.get("error", "未知错误"))
        raise SystemExit(1)
    if args.file_op == "file_read":
        import base64
        data = base64.b64decode(result.get("content", ""))
        out = args.local_file or "-"
        if out == "-":
            try:
                __import__("sys").stdout.buffer.write(data)
            except Exception:
                print(data.decode("utf-8", "replace"))
        else:
            with open(out, "wb") as fh:
                fh.write(data)
            print("已保存到本地：%s（%d 字节，来自设备 %s）" % (out, result.get("size", 0), args.path))
    elif args.file_op == "file_list":
        print("目录：%s（共 %s 项）" % (result.get("path"), result.get("count")))
        for e in result.get("entries", []):
            print("  %-6s %10s  %s" % (e.get("type"), e.get("size", ""), e.get("name")))
    else:
        print("已写入：%s（%d 字节%s）" % (
            result.get("path"), result.get("size", 0),
            "，覆盖" if result.get("overwrite") else ""))


# ---------------------------------------------------------------------------
# pull：来源预检、严格解码与安全原子落盘
# ---------------------------------------------------------------------------
def _split_remote_path(path: str) -> Tuple[str, str]:
    """同时支持 Windows/POSIX 分隔符，返回 (父目录, basename)。"""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise RemoteError("远程路径不能为空且不能含 NUL", kind="rejected")
    normalized = path.replace("\\", "/")
    if normalized.endswith("/"):
        raise RemoteError("远程路径必须指向文件，不能以目录分隔符结尾", kind="rejected")
    parent, separator, name = normalized.rpartition("/")
    if not separator:
        if len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":":
            parent = normalized[:2] + "."
            name = normalized[2:]
        else:
            parent = "."
    elif not parent:
        parent = "/"
    elif len(parent) == 2 and parent[0].isalpha() and parent[1] == ":":
        parent += "/"
    if not name or name in (".", ".."):
        raise RemoteError("无法从远程路径取得合法文件名：%s" % path, kind="rejected")
    return parent, name


def _is_absolute_remote_path(path: str) -> bool:
    """判断 POSIX 绝对路径、UNC 路径或 Windows 盘符绝对路径。"""
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    return (len(normalized) >= 3 and normalized[0].isalpha()
            and normalized[1] == ":" and normalized[2] == "/")


def _join_remote_path(parent: str, name: str) -> str:
    """按远程路径风格拼接父目录与文件名，不依赖控制端 os.path。"""
    if parent.endswith(("/", "\\")):
        return parent + name
    separator = "\\" if "\\" in parent else "/"
    return parent + separator + name


def _normalize_remote_path(path: str, windows: bool) -> str:
    """规范化远程路径仅用于来源一致性比较，不改变下发路径。"""
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized.casefold() if windows else normalized


def _validate_download_name(name: str) -> str:
    """--name 只允许单个 basename，拒绝穿越、空名和目录名。"""
    if (not isinstance(name, str) or not name or not name.strip()
            or name in (".", "..") or "\x00" in name
            or "/" in name or "\\" in name):
        raise RemoteError(
            "--name 只允许单个文件名，不能含路径分隔符、空名、. 或 ..",
            kind="rejected",
        )
    return name


def _download_root() -> Path:
    """返回解析后的 ~/Downloads 根目录。"""
    try:
        return Path("~/Downloads").expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RemoteError("无法解析下载目录：%s" % exc, kind="io_error")


def _prepare_destination(root: Path, name: str) -> Path:
    """校验目标位于下载根内，并拒绝目录、最终软链接和逃逸父链接。"""
    _validate_download_name(name)
    candidate = root / name
    if candidate == root:
        raise RemoteError("目标不能是下载根本身", kind="rejected")
    if candidate.is_symlink():
        raise RemoteError("拒绝写入最终软链接：%s" % candidate, kind="rejected")
    try:
        resolved = candidate.resolve(strict=False)
        parent = candidate.parent.resolve(strict=False)
        if parent != root or os.path.commonpath([str(root), str(resolved)]) != str(root):
            raise RemoteError("目标经父链接逃逸下载目录：%s" % candidate, kind="rejected")
        if resolved == root:
            raise RemoteError("目标不能是下载根本身", kind="rejected")
    except RemoteError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RemoteError("目标路径解析失败：%s" % exc, kind="io_error")
    if candidate.exists() and candidate.is_dir():
        raise RemoteError("目标已存在且不是普通文件：%s" % candidate, kind="conflict")
    return resolved


def _confirm_overwrite(
    args: argparse.Namespace,
    source_path: str,
    destination: Path,
    expected_bytes: int,
) -> bool:
    """目标已存在时默认冲突；交互确认后返回是否允许覆盖。"""
    if not destination.exists():
        return bool(args.overwrite)
    if args.overwrite:
        return True
    message = (
        "目标已存在，覆盖会替换原文件。\n"
        "源：%s\n目标：%s\n大小：%d 字节\n"
        % (source_path, destination, expected_bytes)
    )
    interactive = not args.json
    if interactive:
        try:
            interactive = bool(sys.stdin.isatty())
        except (OSError, ValueError):
            interactive = False
    if not interactive:
        raise RemoteError(
            message + "非交互模式已中止；确认覆盖请显式加 --overwrite。",
            kind="conflict",
        )
    print(message, end="")
    try:
        answer = input("请输入精确“确认”继续，其它输入（含“取消”）均取消：")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "确认":
        raise RemoteError("已取消覆盖，未写入目标文件", kind="cancel")
    return True


def _publish_exclusive(destination: Path, data: bytes) -> None:
    """硬链接不可用时，以 O_EXCL 创建目标并复制，失败清理已创建文件。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(destination), flags, 0o600)
    except FileExistsError:
        raise RemoteError("目标在发布时已存在，未覆盖：%s" % destination, kind="conflict")
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise RemoteError("目标在发布时已存在，未覆盖：%s" % destination,
                              kind="conflict")
        raise
    try:
        fh = os.fdopen(fd, "wb")
        fd = -1
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        with open(str(destination), "rb") as check:
            if check.read() != data:
                raise OSError("目标文件字节复核失败")
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(str(destination))
        except OSError:
            pass
        raise


def _atomic_publish(root: Path, destination: Path, data: bytes, overwrite: bool) -> int:
    """同目录临时件写盘/fsync/复核后原子发布，返回最终字节数。"""
    root.mkdir(parents=True, exist_ok=True)
    rechecked = _prepare_destination(root, destination.name)
    if rechecked != destination:
        raise RemoteError("下载根或目标在发布前发生变化，已中止", kind="rejected")

    fd, tmp_name = tempfile.mkstemp(
        prefix=".%s." % destination.name,
        suffix=".part",
        dir=str(root),
    )
    tmp_path: Optional[Path] = Path(tmp_name)
    try:
        fh = os.fdopen(fd, "w+b")
        fd = -1
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            fh.seek(0)
            if fh.read() != data:
                raise OSError("临时文件字节复核失败")

        _prepare_destination(root, destination.name)
        if overwrite:
            os.replace(str(tmp_path), str(destination))
            tmp_path = None
        else:
            try:
                os.link(str(tmp_path), str(destination))
            except FileExistsError:
                raise RemoteError("目标在发布时已存在，未覆盖：%s" % destination,
                                  kind="conflict")
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise RemoteError("目标在发布时已存在，未覆盖：%s" % destination,
                                      kind="conflict")
                _publish_exclusive(destination, data)
            else:
                os.unlink(str(tmp_path))
                tmp_path = None

        actual = destination.stat().st_size
        if actual != len(data):
            raise OSError("发布后字节数不符：预期 %d，实际 %d" % (len(data), actual))
        with open(str(destination), "rb") as check:
            if check.read() != data:
                raise OSError("发布后字节内容复核失败")
        return actual
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass


def cmd_pull(args: argparse.Namespace) -> Dict[str, Any]:
    """预检来源、严格解码并安全拉取单个设备文件到 ~/Downloads。"""
    started = time.monotonic()
    receipt: Dict[str, Any] = {
        "task_id": None,
        "preflight_task_id": None,
        "target": _initial_target(args.device_id),
        "source_path": None,
        "destination_path": None,
        "status": "protocol_error",
        "expected_bytes": None,
        "actual_bytes": None,
        "elapsed_ms": 0,
        "error": None,
    }
    try:
        if args.name is not None:
            _validate_download_name(args.name)
        parent, source_name = _split_remote_path(args.remote_path)
        device = resolve_device(args.base, args.token, args.device_id,
                                getattr(args, "expect_hostname", None))
        receipt["target"] = _device_target(device)
        windows = str(device.get("platform", "")).lower() in ("windows", "win", "nt")

        # 第一步：只读 file_list 预检，确认来源仍是大小已知的普通文件。
        preflight_body = {
            "device_id": device["device_id"],
            "tool": "file_list",
            "arguments": {"path": parent},
        }
        status, obj = request_json(args.base, "POST", "/api/tasks", args.token,
                                   body=preflight_body)
        receipt["preflight_task_id"] = _task_id(obj, required=False)
        if status < 200 or status >= 300:
            raise _http_error(status, obj)
        receipt["preflight_task_id"] = _task_id(obj)
        preflight = _task_result(obj, receipt, "preflight_task_id", "来源预检")
        if preflight.get("ok") is False:
            raise RemoteError("来源不存在或不可预检：%s" % (
                preflight.get("error") or args.remote_path), kind="source_not_found")
        if preflight.get("ok") is not True:
            raise RemoteError("来源预检结果缺少布尔 ok 字段", kind="protocol_error")
        remote_parent = preflight.get("path")
        entries = preflight.get("entries")
        if not isinstance(remote_parent, str) or not _is_absolute_remote_path(remote_parent):
            raise RemoteError("来源预检 path 不是合法绝对路径", kind="protocol_error")
        if not isinstance(entries, list):
            raise RemoteError("来源预检缺少 entries 数组", kind="protocol_error")

        matches: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise RemoteError("来源预检 entries 格式错误", kind="protocol_error")
            if entry["name"] == source_name:
                matches.append(entry)
            elif windows and entry["name"].casefold() == source_name.casefold():
                matches.append(entry)
        if not matches:
            raise RemoteError("来源文件不存在：%s" % args.remote_path, kind="source_not_found")
        if len(matches) > 1:
            raise RemoteError("来源文件名匹配不唯一：%s" % source_name, kind="source_changed")
        entry = matches[0]
        if entry.get("type") != "file":
            raise RemoteError("来源不是普通文件：%s" % source_name, kind="source_not_found")
        expected = entry.get("size")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise RemoteError("来源预检 size 必须是非负整数", kind="protocol_error")
        source_path = _join_remote_path(remote_parent, entry["name"])
        receipt["source_path"] = source_path
        receipt["expected_bytes"] = expected

        # 第二步：file_read 严格 base64 解码，并同时核对预检大小与传输大小。
        read_body = {
            "device_id": device["device_id"],
            "tool": "file_read",
            "arguments": {"path": source_path},
        }
        status, obj = request_json(args.base, "POST", "/api/tasks", args.token, body=read_body)
        receipt["task_id"] = _task_id(obj, required=False)
        if status < 200 or status >= 300:
            raise _http_error(status, obj)
        receipt["task_id"] = _task_id(obj)
        read_result = _task_result(obj, receipt, "task_id", "文件读取")
        if read_result.get("ok") is False:
            raise RemoteError("来源在预检后读取失败：%s" % (
                read_result.get("error") or source_path), kind="source_changed")
        if read_result.get("ok") is not True:
            raise RemoteError("文件读取结果缺少布尔 ok 字段", kind="protocol_error")
        read_path = read_result.get("path")
        read_size = read_result.get("size")
        content = read_result.get("content")
        encoding = read_result.get("encoding")
        if not isinstance(read_path, str) or not _is_absolute_remote_path(read_path):
            raise RemoteError("文件读取 path 不是合法绝对路径", kind="protocol_error")
        if _normalize_remote_path(read_path, windows) != _normalize_remote_path(source_path, windows):
            raise RemoteError("来源在预检后发生变化：%s -> %s" % (source_path, read_path),
                              kind="source_changed")
        if not isinstance(read_size, int) or isinstance(read_size, bool) or read_size < 0:
            raise RemoteError("文件读取 size 必须是非负整数", kind="protocol_error")
        if encoding is not None and encoding != "base64":
            raise RemoteError("文件读取 encoding 不是 base64", kind="protocol_error")
        if not isinstance(content, str):
            raise RemoteError("文件读取缺少字符串 content", kind="protocol_error")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
            raise RemoteError("文件读取 base64 严格解码失败：%s" % exc, kind="protocol_error")
        receipt["actual_bytes"] = len(data)
        if len(data) != read_size:
            raise RemoteError(
                "文件读取字节数不符：声明 %d，实际 %d" % (read_size, len(data)),
                kind="protocol_error",
            )
        if read_size != expected:
            raise RemoteError(
                "来源在预检后大小变化：预检 %d，读取 %d" % (expected, read_size),
                kind="source_changed",
            )

        # 第三步：所有来源与字节校验通过后，才处理本地冲突并写临时件。
        root = _download_root()
        stem, suffix = os.path.splitext(source_name)
        download_name = args.name or "%s_%s%s" % (stem, receipt["task_id"], suffix)
        download_name = _validate_download_name(download_name)
        destination = _prepare_destination(root, download_name)
        receipt["destination_path"] = str(destination)
        overwrite = _confirm_overwrite(args, source_path, destination, expected)
        receipt["actual_bytes"] = _atomic_publish(root, destination, data, overwrite)
        receipt["status"] = "ok"
    except RemoteError as exc:
        receipt["status"] = exc.kind
        receipt["error"] = str(exc)
    except OSError as exc:
        receipt["status"] = "io_error"
        receipt["error"] = "本地文件写入失败：%s" % exc
    receipt["elapsed_ms"] = _elapsed_ms(started)
    return _emit_receipt(receipt, bool(args.json))


def cmd_register(args: argparse.Namespace) -> None:
    """注册第一步：请求服务端向邮箱发送验证码（无需控制 token）。"""
    import os
    status, obj = request_json(args.base, "POST", "/api/register/send-code",
                               "", body={"email": args.email})
    if args.json:
        dump_json({"status": status, **obj}); return
    if status != 200:
        err = obj.get("error", "") if isinstance(obj, dict) else ""
        fail("发送验证码失败（HTTP %d）%s" % (status, ("：" + err) if err else ""))
    print("验证码已发送到邮箱 %s，请查收后运行 register-confirm 完成注册。" % args.email)


def cmd_register_confirm(args: argparse.Namespace) -> None:
    """注册第二步：校验验证码 + 设密码，成功后把专属 token 写入 config.local.json。"""
    import os
    status, obj = request_json(
        args.base, "POST", "/api/register/verify", "",
        body={"email": args.email, "code": args.code, "password": args.password},
    )
    if args.json:
        dump_json({"status": status, **obj}); return
    if status != 200:
        err = obj.get("error", "") if isinstance(obj, dict) else ""
        fail("注册失败（HTTP %d）%s" % (status, ("：" + err) if err else ""))
    token = str(obj.get("control_token", ""))
    # 保存 token 到脚本同目录配置（与 myclaw 免参调用一致），权限仅本人可读
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
    try:
        existing = _load_local_config()
        cfg = {"server": args.base, "token": token}
        # 若已有其它配置且用户传了 --keep-server 之类，这里简单覆盖（注册即绑定新身份）
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
        saved = True
    except Exception:
        saved = False
    print("注册成功：%s" % args.email)
    if saved:
        print("你的专属控制 token 已安全保存到本地配置（不在屏幕回显），现在可直接用 list / exec。")
    else:
        # 配置写入失败时无法免参调用，必须让用户拿到 token，但不写日志
        print("注意：配置自动保存失败，请手动把下面 token 写入 config.local.json 的 token 字段：")
        print(token)


# ---------------------------------------------------------------------------
# 参数解析与入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构建控制端 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        description="远程设备控制（控制端）：通过 hub REST API 列设备 / 下发命令 / 查任务 / 管设备。",
    )
    parser.add_argument(
        "--base", default=None,
        help="服务端根地址（默认读 REMOTE_SERVER 或 config.local.json）",
    )
    parser.add_argument(
        "--token", default=None,
        help="控制 token（默认读 REMOTE_TOKEN）；不会写入日志或回显明文",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="输出服务端原始 JSON（便于脚本处理）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="注册第一步：向邮箱发送验证码（无需 token）")
    p_reg.add_argument("email", help="你的邮箱地址")

    p_regc = sub.add_parser("register-confirm", help="注册第二步：验证码+密码完成注册（邮箱即账号）")
    p_regc.add_argument("email", help="注册时用的邮箱（即登录账号）")
    p_regc.add_argument("code", help="邮箱收到的 6 位验证码")
    p_regc.add_argument("password", help="登录密码（需满足强度要求）")

    sub.add_parser("list", help="列出所有设备（ID/名称/平台/状态/最后心跳）")

    p_exec = sub.add_parser("exec", help="下发 exec：exec [--expect-hostname HOST] <设备ID或名称> -- <命令>")
    p_exec.add_argument("--expect-hostname", default=None,
                        help="期望主机名，精确匹配 info.hostname，不匹配则拒发")
    p_exec.add_argument("device_id", help="目标设备 ID 或唯一显示名")
    # REMAINDER 捕获 -- 之后的所有内容，使 -la 这类以 - 开头的参数不被当成脚本选项。
    p_exec.add_argument("command", nargs=argparse.REMAINDER, help="命令名 + 参数（用 -- 分隔）")
    p_exec.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出统一 JSON 回执（也可放在子命令前）")

    p_pull = sub.add_parser("pull", help="安全拉取设备文件到 ~/Downloads")
    p_pull.add_argument("--expect-hostname", default=None,
                        help="期望主机名，精确匹配 info.hostname，不匹配则拒发")
    p_pull.add_argument("device_id", help="目标设备 ID 或唯一显示名")
    p_pull.add_argument("remote_path", help="设备上的源文件路径（支持 Windows/POSIX 分隔符）")
    p_pull.add_argument("--name", default=None,
                        help="本地文件名（仅单个 basename）；默认 <stem>_<task_id><suffix>")
    p_pull.add_argument("--overwrite", action="store_true",
                        help="显式确认覆盖同名本地文件；默认冲突并中止")
    p_pull.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出统一 JSON 回执（也可放在子命令前）")

    p_task = sub.add_parser("task", help="查询长任务结果：task <task_id>")
    p_task.add_argument("task_id", help="任务 ID")

    p_create = sub.add_parser("create", help="创建设备并打印一次性私钥")
    p_create.add_argument("display_name", help="设备显示名称")
    p_create.add_argument("platform", help="平台标识，如 macos / windows / linux")

    for name, enabled in (("enable", True), ("disable", False)):
        p = sub.add_parser(name, help="%s设备" % ("启用" if enabled else "禁用"))
        p.add_argument("device_id", help="目标设备 ID")
        p.set_defaults(_enabled=enabled)

    p_reset = sub.add_parser("reset-key", help="重置设备私钥（打印一次性新私钥）")
    p_reset.add_argument("device_id", help="目标设备 ID")

    p_del = sub.add_parser("delete", help="删除设备（不可逆，需 --yes）")
    p_del.add_argument("device_id", help="目标设备 ID")
    p_del.add_argument("--yes", action="store_true", help="确认删除，必须显式给出")

    pf = sub.add_parser("file-read", help="读设备文件（base64 传输）：file-read <device_id> <path> [-o 本地文件]")
    pf.add_argument("device_id"); pf.add_argument("path")
    pf.add_argument("-o", "--local-file", default="-", help="保存本地路径；默认 - 输出二进制到 stdout")
    pf.set_defaults(file_op="file_read")

    pf = sub.add_parser("file-write", help="写设备文件：file-write <device_id> <path> --from 本地文件")
    pf.add_argument("device_id"); pf.add_argument("path")
    pf.add_argument("--from", dest="local_file", required=True, help="本地源文件")
    pf.add_argument("--overwrite", action="store_true", help="显式确认覆盖已存在文件")
    pf.set_defaults(file_op="file_write")

    pf = sub.add_parser("file-list", help="列设备目录：file-list <device_id> <path>")
    pf.add_argument("device_id"); pf.add_argument("path")
    pf.set_defaults(file_op="file_list")

    return parser


def _load_local_config() -> Dict[str, str]:
    """读取脚本同目录的 config.local.json（可选）。

    用于 myclaw 等"每次 exec 都是独立子进程、export 不持久"的场景：
    首次配置把 server/token 写入该文件，之后所有命令免参免环境变量。
    该文件属本地私密配置，不应提交到版本库；读取失败一律视为空配置。
    """
    import os
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {
                "server": str(data.get("server", "")).strip(),
                "token": str(data.get("token", "")).strip(),
            }
    except Exception:
        pass
    return {"server": "", "token": ""}


def resolve_config(args: argparse.Namespace) -> None:
    """配置优先级：命令行参数 > 环境变量 > config.local.json > 默认。"""
    import os
    local = _load_local_config()
    args.base = (args.base or os.environ.get("REMOTE_SERVER")
                 or local.get("server") or DEFAULT_BASE)
    if not args.base:
        fail("未设置服务端地址：请使用 --base、REMOTE_SERVER，"
             "或在 scripts/config.local.json 中配置 server。")
    args.token = (args.token or os.environ.get("REMOTE_TOKEN")
                  or local.get("token") or "")
    # 注册流程无需 token（尚未有账号）
    if getattr(args, "cmd", "") in ("register", "register-confirm"):
        return
    if not args.token:
        # token 只从参数/环境/本地配置读取；此处不回显任何明文。
        fail("未设置控制 token：请设置 REMOTE_TOKEN 环境变量、--token 参数，"
             "或在 scripts/config.local.json 中配置 token；也可先 register 注册。")


def main(argv: Optional[List[str]] = None) -> None:
    """解析配置、分派子命令，并把未分类的远程错误转为非零退出。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    resolve_config(args)
    try:
        if args.cmd == "register":
            cmd_register(args)
        elif args.cmd == "register-confirm":
            cmd_register_confirm(args)
        elif args.cmd == "list":
            cmd_list(args)
        elif args.cmd == "exec":
            cmd_exec(args)
        elif args.cmd == "pull":
            cmd_pull(args)
        elif args.cmd == "task":
            cmd_task(args)
        elif args.cmd == "create":
            cmd_create(args)
        elif args.cmd in ("enable", "disable"):
            cmd_enable(args, args._enabled)
        elif args.cmd == "reset-key":
            cmd_reset_key(args)
        elif args.cmd == "delete":
            cmd_delete(args)
        elif args.cmd in ("file-read", "file-write", "file-list"):
            cmd_file(args)
        else:
            parser.print_help()
    except RemoteError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
