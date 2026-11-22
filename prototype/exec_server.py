#!/usr/bin/env python3
"""exec-server 原型（MCP over stdio 或 HTTP + Bearer token，仅标准库）。

Task #8 交付 stdio 最小原型；Task #12 在其上增加 HTTP 传输与鉴权。
用途：验证 #1 方案里「跨机执行代理」最关键的那部分承诺——白名单命令、可配超时、
退出码透传、输出截断、任务句柄可续查/取消，以及跨机所需的 HTTP + token 鉴权。

边界（Task #19 后）：HTTP 默认只绑 127.0.0.1；只有显式设 EXEC_BIND_HOST 才允许绑
具体局域网地址，且此时必须配 token 否则拒绝启动；0.0.0.0/:: 宽监听一律拒绝；
job 输出只写系统临时目录；不访问 ~/.ssh；不装任何第三方包。
Windows 兼容仅做了静态修复与本机模拟测试，真机未验（见 README「Windows 上还要验什么」）。

stdin/stdout 启动（stdio 模式）：python3 prototype/exec_server.py
HTTP 启动（需要 token，默认绑 127.0.0.1）：
    EXEC_TOKEN='<随机串>' EXEC_HTTP_PORT=8100 python3 prototype/exec_server.py --http
HTTP 绑局域网（必须带 token，否则退出码 2 拒绝启动）：
    EXEC_TOKEN='<随机串>' EXEC_BIND_HOST=192.168.1.10 python3 prototype/exec_server.py --http
单测：python3 -m unittest discover -s prototype/tests -v
"""

from __future__ import annotations

import hmac
import json
import locale
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# POSIX 默认白名单（echo/sleep/ls 等是 POSIX 命令）。
# Windows 上这些命令大多不是可执行文件（echo/ls/cat 是 cmd 内建），直接 Popen 会 FileNotFoundError，
# 所以 Windows 用另一套原生命令默认值，见 default_allowlist()。
DEFAULT_ALLOWLIST = ("echo", "sleep", "ls", "pwd", "whoami", "cat", "false", "true")
# Windows 默认白名单含真正的 shell（cmd/powershell）+ 常用诊断 + 开发常用命令，
# 保证「远程写代码/调试」至少有 cmd /c ... 可用（cmd 在名单里即可执行任意子命令）。
WINDOWS_DEFAULT_ALLOWLIST = (
    "cmd", "powershell", "whoami", "hostname", "ipconfig", "tasklist", "taskkill", "where",
    "python", "python3", "py", "pip", "pip3", "git", "node", "npm", "npx",
)
# POSIX-only 命令：在 Windows 上不是独立可执行文件（echo/ls/cat 是 cmd 内建，其余不存在），
# 设备端配置若残留这些（如 UI「全选」写入的 POSIX 列表），Popen(shell=False) 会 FileNotFoundError。
# Windows 归一化时剔除它们，并兜底补上一条真实 shell，避免 exec 形同虚设。
POSIX_ONLY_COMMANDS = frozenset({
    "echo", "sleep", "ls", "pwd", "cat", "false", "true", "uname", "uptime", "date",
    "mkdir", "rm", "rmdir", "cp", "mv", "chmod", "chown", "head", "tail", "grep",
    "sed", "awk", "which", "touch", "kill", "ps", "df", "du", "sh", "bash",
})
WINDOWS_SHELL_COMMANDS = ("cmd", "powershell")
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_OUTPUT_BYTES = 8192
JOB_POLL_INTERVAL_SECONDS = 0.05

# Task #23：并发与频率限制。
# 并发上限默认 4——本服务单机部署、命令都是白名单里的短命令，正常使用节奏（含 #3 演示里
# 三机扇出，每台机器一个客户端）最多同时 1~3 条；给到 4 既有余量，又能挡住拿到 token 的
# 客户端用大量并发命令把机器拖死。EXEC_MAX_CONCURRENCY 可配，最小 1。
DEFAULT_MAX_CONCURRENCY = 4
# 频率上限默认 30 次 / 10 秒（平均每秒 3 次）——演示脚本的节奏是「一条命令 → 看回执 → 再下一条」，
# 实测级每秒 3 次远高于人机正常节奏，绝不会误伤；EXEC_RATE_MAX=0 表示关闭频率限制。
DEFAULT_RATE_MAX = 30
DEFAULT_RATE_WINDOW_SECONDS = 10.0

# Windows 上 tempfile.gettempdir() 返回 %TEMP%（通常是 C:\Users\<user>\AppData\Local\Temp），
# os.path.join 会拼出反斜杠路径；POSIX 下是 /tmp 或 /var/folders/...。两者都可写、都不在用户文档目录。
JOB_ROOT = os.path.join(tempfile.gettempdir(), "exec-server-prototype-jobs")


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    """按 UTF-8 字节数截断；截断时追加明确标注。"""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    cut = raw[:limit].decode("utf-8", "ignore")
    return cut + "\n...[输出已截断：超过 %d 字节阈值]" % limit, True


def default_allowlist(platform: str | None = None) -> tuple[str, ...]:
    """按平台给默认白名单；platform 可显式传 "nt"/"posix" 供测试模拟，不传就看当前系统。"""
    plat = platform if platform is not None else ("nt" if os.name == "nt" else "posix")
    return WINDOWS_DEFAULT_ALLOWLIST if plat == "nt" else DEFAULT_ALLOWLIST


def parse_allowlist(raw: str | None, platform: str | None = None) -> set[str]:
    if not raw:
        return set(default_allowlist(platform))
    return {item.strip() for item in raw.split(",") if item.strip()}


def normalize_allowlist(allowlist, *, windows_mode: bool) -> set[str]:
    """按运行平台归一化白名单，修掉「Windows 设备端配了 POSIX 命令导致 exec 全废」的问题。

    规则：
      1. POSIX 平台：原样返回（大小写不敏感地去重），保持既有行为。
      2. Windows 平台：剔除 POSIX-only 伪命令（echo/ls/cat/... 在 Windows 不是独立 exe）；
         保留其余自定义命令（大小写归一到小写便于比对）；
         若归一化后一条可用的都没有，回退到 WINDOWS_DEFAULT_ALLOWLIST；
         最后强制保证至少有一条真实 shell（cmd/powershell），否则 exec 无法用于写代码/调试。

    allowlist 为空时按平台默认值。返回的是新集合，不改入参。
    """
    raw = [str(x).strip() for x in (allowlist or []) if str(x).strip()]
    if not windows_mode:
        # POSIX 平台保持原样（含大小写），与修复前行为完全一致
        return set(raw) or set(DEFAULT_ALLOWLIST)
    items = {x.lower() for x in raw}  # Windows 命令名大小写不敏感，归一便于比对
    cleaned = {x for x in items if x not in POSIX_ONLY_COMMANDS}
    if not cleaned:
        cleaned = {x.lower() for x in WINDOWS_DEFAULT_ALLOWLIST}
    if not any(x in WINDOWS_SHELL_COMMANDS for x in cleaned):
        cleaned.add("cmd")
    return cleaned


def basename_any(path: str) -> str:
    """同时按 / 和 \\ 取文件名。

    原写法 os.path.basename 在 POSIX 上只认 "/"，遇到 "C:\\Tools\\echo.exe" 会整串返回，
    白名单校验永远不匹配（Windows 上虽然 os.path.basename 认反斜杠，但测试/日志跨平台时会踩）。
    """
    cleaned = str(path).strip().strip('"').strip("'")
    return cleaned.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def split_command(command, windows: bool | None = None) -> list[str]:
    """把命令拆成 argv。

    关键平台差异：shlex.split 默认 posix=True，会把反斜杠当转义符——
    "C:\\Program Files\\a.exe" 会被吃成 "C:Program Filesa.exe"。
    Windows 上改用 posix=False（保留反斜杠），再剥掉 shlex 保留的两侧引号。
    windows 参数可显式传 True 供测试在 macOS 上模拟 Windows 拆词。
    """
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    if not isinstance(command, str):
        raise ValueError("command 必须是字符串或字符串数组")
    use_windows = os.name == "nt" if windows is None else windows
    if not use_windows:
        return shlex.split(command)
    parts = shlex.split(command, posix=False)
    cleaned = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in ("'", '"'):
            part = part[1:-1]
        cleaned.append(part)
    return cleaned


def decode_output(raw: bytes, fallback_encoding: str | None = None) -> str:
    """子进程输出解码。

    原写法一律按 UTF-8 解码（errors="replace"）：Windows 中文系统上 cmd/powershell
    默认输出的是 cp936/GBK，UTF-8 强解会变乱码，replace 还会把中文吃掉成 "�"。
    现在的顺序：EXEC_OUTPUT_ENCODING 显式指定 > UTF-8 严格 > 系统首选编码（Windows 多为 cp936）。
    这是启发式兜底，不能保证所有程序都正确——真 Windows 上仍需按 README「Windows 上还要验什么」实测。
    """
    if not raw:
        return ""
    explicit = os.environ.get("EXEC_OUTPUT_ENCODING")
    if explicit:
        return raw.decode(explicit, "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        fallback = fallback_encoding or locale.getpreferredencoding(False)
        return raw.decode(fallback, "replace")


def configure_stdio_utf8() -> None:
    """把 stdio JSON-RPC 流固定成 UTF-8 + LF。

    Windows 控制台默认代码页下，sys.stdin/stdout 可能是 cp936，且文本模式写 "\\n" 会变成 "\\r\\n"；
    下游按行解析 JSON-RPC 虽能容忍 CRLF，但统一成 LF 更稳。reconfigure 在非 TextIOWrapper
    （如测试用的 StringIO）上不可用，静默跳过。
    """
    for stream, kwargs in ((sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
                           (sys.stdout, {"encoding": "utf-8", "errors": "replace", "newline": "\n"})):
        try:
            stream.reconfigure(**kwargs)
        except (AttributeError, ValueError, OSError):
            pass


class Executor:
    """四个工具的实现核心；transport 无关，方便单测直接调用。"""

    def __init__(self, allowlist=None, max_output_bytes: int | None = None,
                 default_timeout: float | None = None, windows_mode: bool | None = None,
                 max_concurrency: int | None = None):
        # windows_mode 默认按当前系统判断；测试可在 macOS 上显式传 True 模拟 Windows 分支，
        # 真机部署时也可用 EXEC_WINDOWS_MODE=1 强制（见 README「Windows 上还要验什么」）。
        forced = os.environ.get("EXEC_WINDOWS_MODE")
        if forced is not None:
            windows_mode = forced.strip().lower() in ("1", "true", "yes")
        self.windows_mode = (os.name == "nt") if windows_mode is None else windows_mode
        platform_name = "nt" if self.windows_mode else "posix"
        raw_allowlist = (set(allowlist) if allowlist
                         else parse_allowlist(os.environ.get("EXEC_ALLOWLIST"), platform=platform_name))
        # 关键修复：设备端 device.json 里残留的 POSIX-only 白名单在 Windows 上全部不可执行，
        # 归一化后剔除无效命令并保证有 shell，避免 exec 彻底不可用。POSIX 平台行为不变。
        self.allowlist = normalize_allowlist(raw_allowlist, windows_mode=self.windows_mode)
        self.output_fallback_encoding = (
            os.environ.get("EXEC_OUTPUT_FALLBACK_ENCODING") or locale.getpreferredencoding(False)
        )
        self.max_output_bytes = int(
            max_output_bytes
            if max_output_bytes is not None
            else os.environ.get("EXEC_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)
        )
        self.default_timeout = float(
            default_timeout
            if default_timeout is not None
            else os.environ.get("EXEC_DEFAULT_TIMEOUT", DEFAULT_TIMEOUT)
        )
        # Task #23：全局并发上限（exec 执行中 + start_job 运行中共享同一预算，不排队、到顶即拒绝）。
        raw_conc = max_concurrency if max_concurrency is not None else os.environ.get("EXEC_MAX_CONCURRENCY")
        try:
            self.max_concurrency = max(1, int(raw_conc if raw_conc is not None else DEFAULT_MAX_CONCURRENCY))
        except (TypeError, ValueError):
            self.max_concurrency = DEFAULT_MAX_CONCURRENCY
        self._active = 0  # 当前占用并发槽位数（只在 self._lock 下读写）
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------- 公共工具 ----------

    def _prepare(self, command) -> list[str]:
        argv = split_command(command, windows=self.windows_mode)
        if not argv:
            raise ValueError("command 不能为空")
        base = basename_any(argv[0])
        if base not in self.allowlist:
            raise ValueError(
                "命令不在白名单：%s（当前白名单：%s）" % (base, ",".join(sorted(self.allowlist)))
            )
        return argv

    def _spawn_kwargs(self) -> dict:
        """Popen 的平台参数。

        POSIX 用 start_new_session=True 拿独立进程组，便于整组 killpg；
        Windows 没有进程组/setsid，start_new_session 会被忽略，改用 CREATE_NEW_PROCESS_GROUP，
        终止时再配合 taskkill /T 杀整棵进程树。CREATE_NEW_PROCESS_GROUP 只在 Windows 存在，用 getattr 保护。
        """
        if self.windows_mode:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            return {"creationflags": flags} if flags else {}
        return {"start_new_session": True}

    def _force_kill_tree(self, proc: subprocess.Popen) -> None:
        """强制终止进程及其子进程。

        Windows 没有 os.killpg/os.getpgid，原写法一调用就 AttributeError；这里改走 taskkill /T /F
        （/T 连带子进程树，/F 强杀），失败再退回 proc.kill()。
        POSIX 仍按进程组 SIGKILL 收掉整组（避免 sleep 之类子进程变孤儿）。
        """
        if self.windows_mode:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.kill()
                except OSError:
                    pass
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, AttributeError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _terminate_tree(self, proc: subprocess.Popen) -> None:
        """先礼后兵：POSIX 先 SIGTERM 给 1.5s，再 SIGKILL 整组；Windows 直接 taskkill /T /F。"""
        if self.windows_mode:
            self._force_kill_tree(proc)
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    # ---------- 工具 1：exec ----------

    # ---------- Task #23：并发槽位（exec 与 start_job 共享同一全局预算） ----------

    def _sweep_finished_job_slots_locked(self) -> None:
        """回收已退出的 start_job 占用的并发槽位（必须持 self._lock 调用）。

        只回收槽位、不改 job 状态：状态仍由 query_job/cancel_job 里的 _finalize 决定，
        保持 #8 以来的旧行为不变。已退出的判定用 proc.poll()，与 _finalize 同一机制。
        """
        for job in self._jobs.values():
            if job.get("slot_held") and job["proc"].poll() is not None:
                job["slot_held"] = False
                self._active -= 1

    def _acquire_slot(self, what: str) -> tuple[bool, dict | None]:
        """占一个并发槽位；已到上限立即返回拒绝信息（不排队、不静默丢弃）。

        「立即拒绝而不排队」是刻意的：排队会让客户端以为命令在执行、等一个未知时长，
        与「超时必须给明确错误」的既有约定冲突；到顶直接说清楚，调用方稍后重试即可。
        """
        with self._lock:
            self._sweep_finished_job_slots_locked()
            if self._active >= self.max_concurrency:
                return False, {"active": self._active, "limit": self.max_concurrency}
            self._active += 1
            return True, None

    def _release_slot(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    @staticmethod
    def _concurrency_rejected(what: str, info: dict) -> dict:
        return {
            "ok": False,
            "error_code": "concurrency_limit",
            "error": "已达到并发上限（上限 %d，执行中 %d）：%s 被拒绝；不排队，请稍后重试"
                     % (info["limit"], info["active"], what),
            "active": info["active"],
            "limit": info["limit"],
        }

    def concurrency_status(self) -> dict:
        """给 /health 自检用：当前执行中数量与上限。"""
        with self._lock:
            self._sweep_finished_job_slots_locked()
            return {"active": self._active, "limit": self.max_concurrency}

    def exec_command(self, command, timeout: float | None = None) -> dict:
        try:
            argv = self._prepare(command)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "timed_out": False}

        acquired, info = self._acquire_slot("exec")
        if not acquired:
            rejected = self._concurrency_rejected("exec", info)
            rejected["timed_out"] = False
            return rejected
        try:
            limit = float(timeout) if timeout is not None else self.default_timeout
            started = time.monotonic()
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **self._spawn_kwargs(),
            )
            try:
                out_b, err_b = proc.communicate(timeout=limit)
                timed_out = False
            except subprocess.TimeoutExpired:
                self._force_kill_tree(proc)
                out_b, err_b = proc.communicate()
                timed_out = True

            duration_ms = int((time.monotonic() - started) * 1000)
            stdout, cut_out = truncate_text(
                decode_output(out_b, self.output_fallback_encoding), self.max_output_bytes)
            stderr, cut_err = truncate_text(
                decode_output(err_b, self.output_fallback_encoding), self.max_output_bytes)
            if timed_out:
                return {
                    "ok": False,
                    "error": "命令执行超时（timeout=%.1fs），已终止；超时判定见 duration_ms" % limit,
                    "timed_out": True,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": None,
                    "duration_ms": duration_ms,
                    "truncated": {"stdout": cut_out, "stderr": cut_err},
                }
            return {
                "ok": True,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": proc.returncode,
                "duration_ms": duration_ms,
                "timed_out": False,
                "truncated": {"stdout": cut_out, "stderr": cut_err},
            }
        finally:
            # 无论成功、超时还是异常，exec 占用的槽位都立刻归还
            self._release_slot()

    # ---------- 工具 2：start_job ----------

    def start_job(self, command, timeout: float | None = None) -> dict:
        try:
            argv = self._prepare(command)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        acquired, info = self._acquire_slot("start_job")
        if not acquired:
            # 队列行为：不排队。start_job 与 exec 共享同一并发预算，满则直接拒绝，
            # 调用方可用 query_job 观察存量任务、稍后重试（见 README「并发与频率限制」）。
            return self._concurrency_rejected("start_job", info)
        job_id = "job-" + uuid.uuid4().hex[:12]
        try:
            job_dir = os.path.join(JOB_ROOT, job_id)
            os.makedirs(job_dir, exist_ok=True)
            out_path = os.path.join(job_dir, "stdout.txt")
            err_path = os.path.join(job_dir, "stderr.txt")
            out_f = open(out_path, "wb")
            err_f = open(err_path, "wb")
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                **self._spawn_kwargs(),
            )
            out_f.close()
            err_f.close()
            job = {
                "job_id": job_id,
                "argv": argv,
                "proc": proc,
                "stdout_path": out_path,
                "stderr_path": err_path,
                "started_at": time.monotonic(),
                "timeout": float(timeout) if timeout is not None else None,
                "status": "running",
                "cancelled": False,
                "exit_code": None,
                "note": "",
                "slot_held": True,  # Task #23：该 job 占用一个并发槽位，直到进程退出被回收
            }
        except Exception:
            # 起进程失败：把刚占的槽位还回去，不让失败的 start_job 泄漏预算
            self._release_slot()
            raise
        with self._lock:
            self._jobs[job_id] = job
        return {"ok": True, "job_id": job_id, "status": "running", "pid": proc.pid}

    # ---------- 工具 3：query_job ----------

    def _finalize(self, job: dict) -> None:
        if job["status"] != "running":
            return
        proc: subprocess.Popen = job["proc"]
        code = proc.poll()
        if code is None and job.get("timeout") is not None:
            if time.monotonic() - job["started_at"] > job["timeout"]:
                self._force_kill_tree(proc)
                proc.wait()
                code = proc.returncode
                job["status"] = "timeout"
                job["note"] = "任务执行超时（timeout=%.1fs），已终止" % job["timeout"]
                job["exit_code"] = code
                return
        if code is None:
            return
        job["exit_code"] = code
        if job.get("cancelled"):
            job["status"] = "cancelled"
            job["note"] = job.get("note") or "已取消"
        else:
            job["status"] = "finished" if code == 0 else "failed"

    def _read_job_output(self, job: dict) -> tuple[str, str, dict]:
        def read(path: str) -> str:
            try:
                with open(path, "rb") as fh:
                    # Windows 上 job 输出按系统代码页写（cmd 常为 cp936），沿用统一解码兜底
                    return decode_output(fh.read(), self.output_fallback_encoding)
            except FileNotFoundError:
                return ""

        stdout, cut_out = truncate_text(read(job["stdout_path"]), self.max_output_bytes)
        stderr, cut_err = truncate_text(read(job["stderr_path"]), self.max_output_bytes)
        return stdout, stderr, {"stdout": cut_out, "stderr": cut_err}

    def query_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "job 不存在：%s" % job_id}
        self._finalize(job)
        elapsed_ms = int((time.monotonic() - job["started_at"]) * 1000)
        snapshot = {
            "ok": True,
            "job_id": job["job_id"],
            "status": job["status"],
            "pid": job["proc"].pid,
            "elapsed_ms": elapsed_ms,
            "exit_code": job["exit_code"],
            "note": job.get("note", ""),
        }
        if job["status"] != "running":
            stdout, stderr, cut = self._read_job_output(job)
            snapshot.update(stdout=stdout, stderr=stderr, truncated=cut)
        return snapshot

    # ---------- 工具 4：cancel_job ----------

    def cancel_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "job 不存在：%s" % job_id}
        if job["status"] != "running":
            return self.query_job(job_id)
        proc: subprocess.Popen = job["proc"]
        self._terminate_tree(proc)
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            self._force_kill_tree(proc)
            proc.wait()
        job["cancelled"] = True
        job["note"] = "已按 cancel_job 取消"
        self._finalize(job)
        return self.query_job(job_id)

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job["status"] == "running":
                self.cancel_job(job["job_id"])


# ---------- MCP stdio 传输层（JSON-RPC 2.0，换行分隔） ----------

TOOL_DEFINITIONS = [
    {
        "name": "exec",
        "description": "执行一条白名单命令并等待结束；支持超时、退出码透传、超长输出截断。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "oneOf": [
                        {"type": "string", "description": "命令字符串，如 \"echo hi\"（会按 shlex 拆成 argv）"},
                        {"type": "array", "items": {"type": "string"}, "description": "argv 数组，如 [\"echo\", \"hi\"]"},
                    ]
                },
                "timeout": {"type": "number", "description": "超时秒数，缺省用 EXEC_DEFAULT_TIMEOUT"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "start_job",
        "description": "后台起一个长任务，立刻返回 job_id；用 query_job 续查、cancel_job 取消。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "timeout": {"type": "number", "description": "可选：任务整体超时秒数"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "query_job",
        "description": "按 job_id 查任务状态与输出；job 不存在时返回明确错误。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "cancel_job",
        "description": "取消一个运行中的任务；取消后可用 query_job 查到 cancelled 状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]

SERVER_INFO = {"name": "exec-server-prototype", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"


class McpServer:
    def __init__(self, executor: Executor):
        self.executor = executor

    def dispatch(self, message: dict) -> dict | None:
        """处理一条 JSON-RPC 消息；通知类消息返回 None（不回包）。"""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if method == "initialize":
            return self._result(msg_id, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOL_DEFINITIONS})
        if method == "tools/call":
            return self._result(msg_id, self._call_tool(params))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}

    @staticmethod
    def _result(msg_id, result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name == "exec":
                result = self.executor.exec_command(
                    arguments["command"], timeout=arguments.get("timeout"))
            elif name == "start_job":
                result = self.executor.start_job(
                    arguments["command"], timeout=arguments.get("timeout"))
            elif name == "query_job":
                result = self.executor.query_job(arguments["job_id"])
            elif name == "cancel_job":
                result = self.executor.cancel_job(arguments["job_id"])
            else:
                result = {"ok": False, "error": "未知工具：%s" % name}
        except KeyError as exc:
            result = {"ok": False, "error": "缺少参数：%s" % exc}
        except Exception as exc:  # 工具层绝不把异常直接抛给 client
            result = {"ok": False, "error": "工具执行异常：%s: %s" % (type(exc).__name__, exc)}
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "isError": result.get("ok") is False,
        }


def serve_stdio(server: McpServer, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error: %s" % exc},
            }) + "\n")
            stdout.flush()
            continue
        if not isinstance(message, dict):
            continue
        response = server.dispatch(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


# ---------- HTTP 传输层（Streamable HTTP 风格的 POST + JSON-RPC） ----------

HTTP_BIND_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8100
MAX_HTTP_BODY_BYTES = 1024 * 1024
ALLOWED_HTTP_PATHS = ("/mcp", "/")
HEALTH_PATHS = ("/health", "/healthz")

# Task #19：默认仍只绑 127.0.0.1；只有显式设置 EXEC_BIND_HOST 才允许绑局域网地址。
# 0.0.0.0 / :: 这类宽监听永远拒绝，不提供开关。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]", "*")


def is_loopback_host(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def is_wildcard_host(host: str) -> bool:
    return (host or "").strip().lower() in WILDCARD_HOSTS


def resolve_bind_host(raw: str | None = None) -> str:
    """解析绑定地址：默认 127.0.0.1；环境变量 EXEC_BIND_HOST 只在显式设置时生效。"""
    host = (raw if raw is not None else os.environ.get("EXEC_BIND_HOST") or HTTP_BIND_HOST).strip()
    return host or HTTP_BIND_HOST


def _banner_max_concurrency() -> int:
    """横幅显示用：与 Executor.__init__ 的解析口径一致（非法值退回默认、最小 1）。"""
    raw = os.environ.get("EXEC_MAX_CONCURRENCY")
    try:
        return max(1, int(raw)) if raw not in (None, "") else DEFAULT_MAX_CONCURRENCY
    except ValueError:
        return DEFAULT_MAX_CONCURRENCY


def binding_banner(host: str, port: int) -> str:
    """启动横幅：绑局域网时明确警告 token 是唯一门槛，并给出 /health 自检地址；
    另打印生效的并发/频率限制（Task #23），装完一眼核对配置。"""
    limiter = RateLimiter()
    limits = ("限流：并发上限 %d 条（EXEC_MAX_CONCURRENCY，exec 与 start_job 共享，到顶即拒不排队）；"
              "频率上限 %s（仅 HTTP，EXEC_RATE_MAX=0 关闭）" % (
                  _banner_max_concurrency(),
                  ("%d 次 / %g 秒" % (limiter.max_requests, limiter.window_seconds))
                  if limiter.enabled else "关闭"))
    if is_loopback_host(host):
        return ("exec-server HTTP 模式：http://%s:%d/mcp（默认仅回环地址，只有本机能访问）\n%s"
                % (host, port, limits))
    return ("exec-server HTTP 模式：http://%s:%d/mcp\n"
            "⚠ 本次监听在局域网地址 %s，任何能访问该地址的人在没有 token 时会被拒（HTTP 401）。\n"
            "自检：http://%s:%d/health 可核对当前绑定地址；未配置 token 时拒绝启动。\n%s"
            % (host, port, host, host, port, limits))


def load_token() -> str:
    """token 只从环境变量或文件读取，代码里不出现任何默认 token。"""
    token = (os.environ.get("EXEC_TOKEN") or "").strip()
    if token:
        return token
    token_file = os.environ.get("EXEC_TOKEN_FILE")
    if token_file:
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            print("读取 EXEC_TOKEN_FILE 失败：%s" % exc, file=sys.stderr)
    return ""


def _extract_bearer(headers) -> str:
    raw = headers.get("Authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


class RateLimiter:
    """按客户端的滑动窗口频率限制（Task #23；只挂在 HTTP 传输上，见 README 说明）。

    - EXEC_RATE_MAX 次 / EXEC_RATE_WINDOW 秒，默认 30 次 / 10 秒；EXEC_RATE_MAX=0 关闭。
    - key 由调用方给（HTTP 用客户端 IP）：同一局域网里不同机器互不影响，
      单台机器超限只挡它自己，符合「单客户端频率上限」的口径。
    - clock 可注入，单测用假时钟避免真的 sleep。
    """

    def __init__(self, max_requests: int | None = None, window_seconds: float | None = None,
                 clock=time.monotonic):
        try:
            if max_requests is None:
                raw = os.environ.get("EXEC_RATE_MAX")
                max_requests = int(raw) if raw not in (None, "") else DEFAULT_RATE_MAX
            if window_seconds is None:
                raw = os.environ.get("EXEC_RATE_WINDOW")
                window_seconds = float(raw) if raw not in (None, "") else DEFAULT_RATE_WINDOW_SECONDS
        except ValueError:  # 环境变量写错不炸启动，退回默认值
            max_requests = DEFAULT_RATE_MAX if max_requests is None else max_requests
            window_seconds = (DEFAULT_RATE_WINDOW_SECONDS
                              if window_seconds is None else window_seconds)
        self.max_requests = max(0, int(max_requests))  # 0 = 关闭频率限制
        self.window_seconds = max(0.001, float(window_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    @property
    def enabled(self) -> bool:
        return self.max_requests > 0

    def check(self, key: str) -> tuple[bool, float]:
        """记一次请求；返回 (是否放行, retry_after_ms)。拒绝时不记账，窗口自然滑出。"""
        if not self.enabled:
            return True, 0.0
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if now - t < self.window_seconds]
            if len(hits) >= self.max_requests:
                retry_after_ms = int(max(hits[0] + self.window_seconds - now, 0.001) * 1000)
                self._hits[key] = hits
                return False, float(retry_after_ms)
            hits.append(now)
            self._hits[key] = hits
            return True, 0.0

    def describe(self) -> dict:
        return {"max": self.max_requests, "window_seconds": self.window_seconds,
                "enabled": self.enabled}


class _HttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "exec-server-prototype/0.2"

    def log_message(self, fmt, *args):  # 访问日志只写 stderr，避免污染 stdout
        print("http: " + (fmt % args), file=sys.stderr)

    def _send_json(self, status: int, payload: dict, close: bool = False,
                   extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in HEALTH_PATHS:  # 无需 token：只暴露绑定地址与是否需要鉴权，便于装完自检
            server = self.server
            payload = {
                "ok": True,
                "bind_host": getattr(server, "bind_host", ""),
                "bind_port": getattr(server, "bind_port", 0),
                "loopback_only": is_loopback_host(getattr(server, "bind_host", "")),
                "auth_required": True,
            }
            executor = getattr(getattr(server, "mcp_server", None), "executor", None)
            if executor is not None:  # Task #23：自检时一并看到并发限流的生效配置与当前占用
                payload["concurrency"] = executor.concurrency_status()
            limiter = getattr(server, "rate_limiter", None)
            if limiter is not None:
                payload["rate_limit"] = limiter.describe()
            self._send_json(200, payload)
            return
        self._send_json(405, {"error": "只支持 POST /mcp；自检用 GET /health"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        expected = self.server.expected_token
        provided = _extract_bearer(self.headers)

        # 鉴权先于一切执行：没有 token 或 token 不对直接 401，绝不 dispatch。
        authorized = bool(expected) and bool(provided) and hmac.compare_digest(provided, expected)
        if not authorized:
            if 0 < length <= MAX_HTTP_BODY_BYTES:
                self.rfile.read(length)  # 读掉请求体，保持连接干净（不做任何解析与执行）
            self._send_json(401, {"error": "unauthorized", "message": "缺少或错误的 Bearer token"})
            return

        if self.path not in ALLOWED_HTTP_PATHS:
            self._send_json(404, {"error": "not found"})
            return
        if length <= 0 or length > MAX_HTTP_BODY_BYTES:
            self._send_json(413, {"error": "请求体为空或超过 %d 字节" % MAX_HTTP_BODY_BYTES})
            return
        raw = self.rfile.read(length)
        # Task #23：单客户端频率上限（按客户端 IP 的滑动窗口）。鉴权已在上面完成，这里拦的是
        # 已验证 token 的客户端本身；GET /health 自检不计数，未带 token 的请求走 401 不计数。
        client_key = self.client_address[0] if self.client_address else "unknown"
        allowed, retry_after_ms = self.server.rate_limiter.check(client_key)
        if not allowed:
            limiter = self.server.rate_limiter
            self._send_json(429, {
                "ok": False,
                "error_code": "rate_limit",
                "error": "请求频率超限（客户端 %s）：限制 %d 次 / %g 秒，%g 秒后可重试"
                         % (client_key, limiter.max_requests, limiter.window_seconds,
                            retry_after_ms / 1000.0),
                "retry_after_ms": retry_after_ms,
            }, extra_headers={"Retry-After": str(max(1, int(retry_after_ms // 1000) or 1))})
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "Parse error"}})
            return
        if not isinstance(message, dict):
            self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32600, "message": "Invalid Request"}})
            return
        response = self.server.mcp_server.dispatch(message)
        if response is None:  # 通知类消息：Streamable HTTP 语义回 202，无响应体
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, response)


class HttpTransportServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, mcp_server: McpServer, token: str):
        self.mcp_server = mcp_server
        self.expected_token = token
        self.rate_limiter = RateLimiter()  # Task #23：频率限制只挂在 HTTP 传输上
        super().__init__(address, _HttpHandler)
        self.bind_host = self.server_address[0]
        self.bind_port = self.server_address[1]


def make_http_server(mcp_server: McpServer, token: str, port: int = 0,
                     host: str | None = None) -> HttpTransportServer:
    """建 HTTP 服务。

    Task #19 的绑定策略：
    - 不传 host 且未设 EXEC_BIND_HOST → 127.0.0.1（默认回环，绝不默认宽监听）；
    - 显式 host / EXEC_BIND_HOST → 允许具体局域网地址，但 0.0.0.0 / :: 一律拒绝；
    - 绑非回环地址时 token 必填，没 token 直接拒绝建服务（调用方 main 里也会拒绝启动）。
    """
    bind_host = resolve_bind_host(host)
    if is_wildcard_host(bind_host):
        raise ValueError("拒绝宽监听地址：%s（0.0.0.0/:: 一律不允许；请显式指定具体网卡地址）" % bind_host)
    if not is_loopback_host(bind_host) and not token:
        raise ValueError("绑定非回环地址 %s 时必须配置 token（EXEC_TOKEN 或 EXEC_TOKEN_FILE）；拒绝启动" % bind_host)
    return HttpTransportServer((bind_host, int(port)), mcp_server, token)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    http_mode = "--http" in argv or os.environ.get("EXEC_TRANSPORT", "").lower() == "http"

    executor = Executor()
    server = McpServer(executor)

    if not http_mode:
        configure_stdio_utf8()
        try:
            serve_stdio(server)
        except KeyboardInterrupt:
            return 130
        finally:
            executor.shutdown()
        return 0

    token = load_token()
    bind_host = resolve_bind_host()
    if not token:
        if is_loopback_host(bind_host):
            message = "HTTP 模式必须在 EXEC_TOKEN 或 EXEC_TOKEN_FILE 中提供 token；拒绝启动（不生成默认 token）。"
        else:
            message = ("绑定非回环地址 %s 时必须配置 token（EXEC_TOKEN 或 EXEC_TOKEN_FILE）；"
                       "拒绝启动，避免把无鉴权的执行服务暴露到局域网。" % bind_host)
        print(message, file=sys.stderr)
        executor.shutdown()
        return 2
    try:
        port = int(os.environ.get("EXEC_HTTP_PORT", DEFAULT_HTTP_PORT))
    except ValueError:
        print("EXEC_HTTP_PORT 不是整数", file=sys.stderr)
        executor.shutdown()
        return 2
    try:
        httpd = make_http_server(server, token, port, host=bind_host)
    except ValueError as exc:
        # 宽监听等配置错误：给出一行明确原因并以退出码 2 拒绝启动，
        # 与上面「无 token 拒绝启动」保持同一退出码，不甩 traceback。
        print(str(exc), file=sys.stderr)
        executor.shutdown()
        return 2
    print(binding_banner(httpd.bind_host, httpd.bind_port), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        httpd.server_close()
        executor.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
