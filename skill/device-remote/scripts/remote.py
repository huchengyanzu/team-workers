#!/usr/bin/env python3
"""远程设备控制 —— 控制端命令行工具（纯 Python 标准库）。

通过远程控制服务端（hub）的 REST API 操作受控设备，仅用 urllib 发请求，
不依赖第三方库，不直连设备、不读取任何私钥文件。

鉴权：所有请求头带 X-Control-Token。配置优先级（高→低）：
  --token / --base 参数 > 环境变量 REMOTE_TOKEN / REMOTE_SERVER >
  脚本同目录 config.local.json > 默认值。
config.local.json 形如 {"server":"https://hub.example.com","token":"<token>"}，
用于 exec 子进程不共享环境变量的场景（如 myclaw），配置一次后免参调用；该文件勿提交版本库。
服务端默认 https://hub.example.com，请求路径 = base + /api/...。

用法示例：
    python3 remote.py list
    python3 remote.py exec dev-xxxx -- echo hi
    python3 remote.py exec dev-xxxx -- ls -la /tmp
    python3 remote.py task task-xxxxxxxx
    python3 remote.py create "客厅 Mac mini" macos
    python3 remote.py --json list
    python3 remote.py --base http://127.0.0.1:8766 --token <t> list
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# 服务端默认对外前缀（Caddy 反代），可用 --base / REMOTE_SERVER 覆盖。
DEFAULT_BASE = "https://hub.example.com"
# 服务端同步等待任务结果的超时为 15s，客户端 HTTP 超时放宽到 30s，
# 以便能收到服务端返回的 {state: "timeout"} 响应而不是自己先超时。
HTTP_TIMEOUT = 30

# 工具白名单与服务端 HttpApi.TOOLS 保持一致。
TOOLS = ("exec", "start_job", "query_job", "cancel_job",
         "file_read", "file_write", "file_list")


class RemoteError(Exception):
    """面向用户的可读错误（网络失败 / 超时等）。"""


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
        raw = exc.read().decode("utf-8", "replace")
        status = int(exc.code)
    except urllib.error.URLError as exc:
        raise RemoteError("网络错误：无法连接服务端（%s）" % exc.reason)
    except socket.timeout:
        raise RemoteError("请求超时：服务端无响应或网络过慢")
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


def cmd_exec(args: argparse.Namespace) -> None:
    """POST /api/tasks：下发 exec 命令。

    command 经 argparse.REMAINDER 捕获（用户用 -- 分隔），命令名 + 参数以数组传入。
    15s 内出结果打印 stdout / 退出码；超时打印 task_id 并提示用 task 子命令查询。
    """
    command: List[str] = list(args.command or [])
    # 兼容不同 Python 版本：若分隔符 "--" 被保留，去掉它。
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        fail("缺少要执行的命令：用法 remote.py exec <device_id> -- <命令> [参数...]")
    body = {"device_id": args.device_id, "tool": "exec", "arguments": {"command": command}}
    status, obj = request_json(args.base, "POST", "/api/tasks", args.token, body=body)
    if args.json:
        # --json 也尊重错误码（仍输出服务端原始响应，便于排查）。
        if status < 200 or status >= 300:
            dump_json(obj)
            raise SystemExit(1)
        dump_json(obj)
        return
    ok_or_error(status, obj)
    task_id = str(obj.get("task_id", ""))
    state = str(obj.get("state", ""))
    if state == "timeout":
        print("命令已下发，但 15 秒内未返回结果（长任务）。")
        print("task_id：%s" % task_id)
        print("请用以下命令查询结果：python3 %s task %s" % (sys.argv[0], task_id))
        return
    if state == "done":
        print("task_id：%s  状态：done" % task_id)
        print_result(obj.get("result"))
        return
    # 兜底：其它状态（如 queued/dispatched/failed）打印完整响应。
    print("task_id：%s  状态：%s" % (task_id, state))
    dump_json(obj)


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
    parser = argparse.ArgumentParser(
        description="远程设备控制（控制端）：通过 hub REST API 列设备 / 下发命令 / 查任务 / 管设备。",
    )
    parser.add_argument(
        "--base", default=None,
        help="服务端根地址（默认读 REMOTE_SERVER，否则 %s）" % DEFAULT_BASE,
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

    p_exec = sub.add_parser("exec", help="下发 exec 命令：exec <device_id> -- <命令> [参数...]")
    p_exec.add_argument("device_id", help="目标设备 ID")
    # REMAINDER 捕获 -- 之后的所有内容，使 -la 这类以 - 开头的参数不被当成脚本选项。
    p_exec.add_argument("command", nargs=argparse.REMAINDER, help="命令名 + 参数（用 -- 分隔）")

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
