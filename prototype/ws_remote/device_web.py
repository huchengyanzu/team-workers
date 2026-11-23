#!/usr/bin/env python3
"""设备端本地 Web 配置服务（免登录，仅绑定 127.0.0.1）。

安全口径：
  - 只监听回环地址，不对外暴露；公网不可达
  - 免登录但防 CSRF：POST 必须带 X-Local-Config: 1 且 Origin 校验
    （跨站简单请求无法带自定义头，会触发预检并被拒绝）
  - 私钥在页面只回显掩码，完整值只在配置时写入，不通过 GET 下发

接口：
  GET  /                     -> ui/device.html
  GET  /theme.css            -> ui/theme.css
  GET  /api/local/config     -> {configured, server_url, device_id, key_masked,
                                 display_name, platform, allowlist}
  POST /api/local/config     -> 保存配置（body 同上，key 为空表示保持原值）
  GET  /api/local/status     -> {status, error, server_url, device_id, last_change}
  GET  /api/local/info       -> 本机信息
  GET  /api/local/browse?path= -> 列子目录（供配置页选择器点选；无 path 返回候选根目录）
  GET  /api/local/autostart  -> {enabled, supported, platform}
  POST /api/local/autostart  -> {enabled: true/false} 开关开机自启动（CSRF 同 config）
  POST /api/local/exit       -> 请求设备端进程优雅退出（CSRF 同 config）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape as _xml_escape


def _resource_dir() -> Path:
    """兼容 PyInstaller 单文件：运行时资源在 sys._MEIPASS；源码运行用本文件目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent


THIS_DIR = _resource_dir()
UI_DIR = THIS_DIR / "ui"
ALLOWED_ORIGINS = {"http://127.0.0.1", "http://localhost"}

HTTP_REASON = {200: "OK", 400: "Bad Request", 403: "Forbidden",
               404: "Not Found", 413: "Payload Too Large", 500: "Internal Server Error"}
MAX_BODY = 128 * 1024


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@dataclass
class DeviceState:
    """设备端运行时状态，Web 与 WebSocket 循环共享。"""
    config: dict = field(default_factory=dict)
    config_path: str = ""
    status: str = "unconfigured"   # unconfigured/connecting/online/offline/auth_failed
    error: str = ""
    last_change: float = 0.0
    conn: object = None            # 当前 ws，配置变更时关闭以触发重连
    generation: int = 0            # 配置版本，供主循环感知变更
    shutdown: bool = False         # 用户在配置页点了“退出程序”

    def public(self) -> dict:
        cfg = self.config
        return {
            "configured": bool(cfg.get("server_url") and cfg.get("device_id") and cfg.get("key")),
            "server_url": cfg.get("server_url", ""),
            "device_id": cfg.get("device_id", ""),
            "key_masked": mask_key(cfg.get("key", "")),
            "display_name": cfg.get("display_name", ""),
            "platform": cfg.get("platform", ""),
            "allowlist": cfg.get("allowlist", []),
            "allowed_roots": cfg.get("allowed_roots", []),
        }


class DeviceWebApi:
    def __init__(self, state: DeviceState, port: int, collect_info):
        self.state = state
        self.port = port
        self.collect_info = collect_info

    @staticmethod
    def _origin_ok(headers: dict) -> bool:
        origin = headers.get("origin", "")
        if not origin:
            return True  # 本机 curl/脚本无 Origin
        parsed = urlparse(origin)
        base = f"{parsed.scheme}://{parsed.hostname}"
        if base in ALLOWED_ORIGINS and (parsed.port in (None, self_port())):
            return True
        return False

    def _write(self, writer, status: int, body: bytes, ctype: str) -> None:
        head = (
            f"HTTP/1.1 {status} {HTTP_REASON.get(status, 'OK')}\r\n"
            f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\nCache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\n\r\n"
        )
        writer.write(head.encode("utf-8") + body)

    async def handle(self, reader, writer) -> None:
        try:
            raw = await reader.readline()
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                return
            parts = line.split()
            if len(parts) < 2:
                return
            target = urlparse(parts[1])
            method, path = parts[0].upper(), target.path
            query = parse_qs(target.query)
            headers = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                if b":" in h:
                    k, v = h.decode("utf-8", "replace").split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0)
            if length > MAX_BODY:
                self._write(writer, 413, b'{"error":"body too large"}', "application/json")
                return
            body_bytes = await reader.readexactly(length) if length else b""
            try:
                body = json.loads(body_bytes) if body_bytes else {}
            except json.JSONDecodeError:
                body = {}
            if not isinstance(body, dict):
                body = {}

            if method == "GET" and path in ("/", "/device.html"):
                p = UI_DIR / "device.html"
                if p.is_file():
                    self._write(writer, 200, p.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._write(writer, 404, b"device.html missing", "text/plain; charset=utf-8")
                return
            if method == "GET" and path == "/theme.css":
                p = UI_DIR / "theme.css"
                if p.is_file():
                    self._write(writer, 200, p.read_bytes(), "text/css; charset=utf-8")
                else:
                    self._write(writer, 404, b"", "text/css; charset=utf-8")
                return
            if method == "GET" and path == "/api/local/config":
                self._json(writer, 200, self.state.public())
                return
            if method == "GET" and path == "/api/local/status":
                self._json(writer, 200, {
                    # state/status 双字段兼容不同版本页面
                    "state": self.state.status,
                    "status": self.state.status,
                    "last_error": self.state.error,
                    "error": self.state.error,
                    "connected_at": self.state.last_change,
                    "server_url": self.state.config.get("server_url", ""),
                    "device_id": self.state.config.get("device_id", ""),
                    "last_change": self.state.last_change,
                })
                return
            if method == "GET" and path == "/api/local/info":
                self._json(writer, 200, self.collect_info())
                return
            if method == "GET" and path == "/api/local/browse":
                self._json(writer, 200, self._browse(query.get("path", [""])[0]))
                return
            if method == "GET" and path == "/api/local/autostart":
                self._json(writer, 200, self._autostart_status())
                return
            if method == "POST" and path == "/api/local/autostart":
                if not self._origin_ok(headers) or headers.get("x-local-config") != "1":
                    self._json(writer, 403, {"error": "CSRF check failed"})
                    return
                self._json(writer, 200, self._set_autostart(bool(body.get("enabled"))))
                return
            if method == "POST" and path == "/api/local/exit":
                if not self._origin_ok(headers) or headers.get("x-local-config") != "1":
                    self._json(writer, 403, {"error": "CSRF check failed"})
                    return
                self.state.shutdown = True
                self._json(writer, 200, {"ok": True})
                return
            if method == "POST" and path == "/api/local/config":
                if not self._origin_ok(headers) or headers.get("x-local-config") != "1":
                    self._json(writer, 403, {"error": "CSRF check failed"})
                    return
                self._save_config(body)
                self._json(writer, 200, {"ok": True, "config": self.state.public()})
                return
            self._json(writer, 404, {"error": "not found"})
        except Exception as exc:
            self._json(writer, 500, {"error": str(exc)})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _candidate_roots() -> list[dict]:
        """无路径时给出的起始候选：Windows 列盘符，Unix 列用户主目录与根。"""
        out: list[dict] = []
        home = Path(os.path.expanduser("~"))
        if os.name == "nt":
            # 列 A-Z 盘中存在的
            import string
            for letter in string.ascii_uppercase:
                d = Path(f"{letter}:\\")
                try:
                    if d.exists():
                        out.append({"name": f"{letter}:\\", "path": str(d)})
                except OSError:
                    continue
            out.append({"name": "用户主目录 " + str(home), "path": str(home)})
        else:
            out.append({"name": "用户主目录 ~", "path": str(home)})
            out.append({"name": "根目录 /", "path": "/"})
        return out

    def _browse(self, raw_path: str) -> dict:
        """列出某目录下的子目录，供配置页选择器逐级点选。只读本机文件系统。"""
        if not raw_path:
            return {"ok": True, "path": "", "parent": None,
                    "dirs": self._candidate_roots(), "roots": True}
        try:
            p = Path(os.path.expanduser(raw_path))
            if not p.is_dir():
                return {"ok": False, "error": "不是目录: " + raw_path, "dirs": []}
            p = p.resolve()
            dirs = []
            try:
                for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                    try:
                        if child.is_dir() and not child.is_symlink():
                            dirs.append({"name": child.name, "path": str(child)})
                    except OSError:
                        continue
                    if len(dirs) >= 500:
                        break
            except OSError as exc:
                return {"ok": False, "error": "列目录失败: %s" % exc, "dirs": []}
            parent = str(p.parent) if p.parent != p else None
            return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs, "roots": False}
        except (OSError, RuntimeError) as exc:
            return {"ok": False, "error": "路径解析失败: %s" % exc, "dirs": []}

    # ---------- 开机自启动（仅操作当前用户级启动项，无需管理员权限） ----------
    _AUTOSTART_LABEL = "com.teamworkers.device"

    def _mac_plist(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / (self._AUTOSTART_LABEL + ".plist")

    def _launch_command(self) -> list[str]:
        """自启动时要执行的命令。macOS 通过 open -a 启动 app，避免绕开 bundle 环境。"""
        if sys.platform == "darwin":
            exe = Path(sys.executable).resolve()
            # launchd 直接拉起 bundle 内的 Mach-O：环境变量（TEAM_WORKERS_NO_BROWSER=1）
            # 一定可达进程；用 open -a 时 GUI app 未必继承 launchd 的环境。
            if ".app/Contents/" in str(exe):
                return [str(exe)]
            # 源码/旧版启动场景：逐级向上找 bundle，复用启动台入口。
            for parent in exe.parents:
                if parent.name.endswith(".app") and parent.is_dir():
                    return ["/usr/bin/open", "-a", str(parent)]
            launcher = exe.parent / "launcher"
            if launcher.is_file():
                return [str(launcher)]
            return [str(exe)]
        return [sys.executable]

    def _win_run_command(self) -> str:
        args = list(self._launch_command())
        args += ["--config", self.state.config_path or "device.json",
                 "--web-port", "18770", "--no-browser"]
        import subprocess as _sp
        return _sp.list2cmdline(args)

    def _autostart_status(self) -> dict:
        if sys.platform == "darwin":
            return {"enabled": self._mac_plist().is_file(), "supported": True,
                    "platform": "mac", "hint": "登录后自动在后台启动设备端，不自动弹浏览器"}
        if os.name == "nt":
            import winreg
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_READ,
                ) as key:
                    winreg.QueryValueEx(key, "TeamWorkersDevice")
                    enabled = True
            except FileNotFoundError:
                enabled = False
            except Exception:
                enabled = False
            return {"enabled": enabled, "supported": True, "platform": "win",
                    "hint": "登录后自动在后台启动设备端"}
        return {"enabled": False, "supported": False, "platform": "other",
                "hint": "当前平台暂不支持一键开关，请自行添加到系统启动项"}

    @staticmethod
    def _launchctl(action: str, target: Path) -> tuple[bool, str]:
        try:
            r = subprocess.run(["/bin/launchctl", action, str(target)],
                               capture_output=True, text=True, timeout=10)
            return r.returncode == 0, (r.stderr or r.stdout).strip()
        except Exception as exc:
            return False, str(exc)

    def _set_autostart(self, enabled: bool) -> dict:
        if sys.platform == "darwin":
            plist = self._mac_plist()
            if plist.is_file():
                self._launchctl("unload", plist)
            if not enabled:
                try:
                    plist.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    return {"ok": False, "error": "删除启动项失败: %s" % exc,
                            **self._autostart_status()}
                return {"ok": True, **self._autostart_status()}
            plist.parent.mkdir(parents=True, exist_ok=True)
            args_xml = "\n".join(
                "      <string>%s</string>" % _xml_escape(str(x)) for x in self._launch_command())
            plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                '  <key>Label</key>\n  <string>%s</string>\n'
                '  <key>ProgramArguments</key>\n  <array>\n%s\n  </array>\n'
                '  <key>EnvironmentVariables</key>\n  <dict>\n'
                '    <key>TEAM_WORKERS_NO_BROWSER</key><string>1</string>\n'
                '  </dict>\n'
                '  <key>RunAtLoad</key>\n  <true/>\n'
                '  <key>KeepAlive</key>\n  <false/>\n'
                '</dict>\n</plist>\n' % (self._AUTOSTART_LABEL, args_xml),
                encoding="utf-8")
            ok, err = self._launchctl("load", plist)
            if not ok and "already loaded" not in err.lower():
                return {"ok": False, "error": "注册启动项失败: %s" % err,
                        **self._autostart_status()}
            return {"ok": True, **self._autostart_status()}
        if os.name == "nt":
            import winreg
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
                ) as key:
                    if enabled:
                        winreg.SetValueEx(key, "TeamWorkersDevice", 0,
                                          winreg.REG_SZ, self._win_run_command())
                    else:
                        try:
                            winreg.DeleteValue(key, "TeamWorkersDevice")
                        except FileNotFoundError:
                            pass
                return {"ok": True, **self._autostart_status()}
            except Exception as exc:
                return {"ok": False, "error": "设置启动项失败: %s" % exc,
                        **self._autostart_status()}
        return {"ok": False, "error": "当前平台不支持", **self._autostart_status()}

    def _json(self, writer, status: int, obj: dict) -> None:
        self._write(writer, status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")

    def _save_config(self, body: dict) -> None:
        cfg = dict(self.state.config)
        for k in ("server_url", "device_id", "display_name", "platform"):
            if k in body:
                cfg[k] = str(body[k])[:256].strip()
        # key 为空表示保持原值（页面只拿到掩码，不可能回传完整 key）
        if body.get("key"):
            cfg["key"] = str(body["key"])[:512].strip()
        allowlist = body.get("allowlist")
        if isinstance(allowlist, list):
            cfg["allowlist"] = [str(x)[:64].strip() for x in allowlist if str(x).strip()][:64]
        roots = body.get("allowed_roots")
        if isinstance(roots, list):
            cfg["allowed_roots"] = [str(x)[:512].strip() for x in roots if str(x).strip()][:32]

        url = cfg.get("server_url", "")
        if url and not (url.startswith("ws://") or url.startswith("wss://")):
            raise ValueError("server_url must start with ws:// or wss://")
        if not self.state.config_path:
            raise ValueError("config path not set")
        p = Path(self.state.config_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.state.config = cfg
        self.state.generation += 1
        self.state.status = "connecting" if cfg.get("server_url") else "unconfigured"
        self.state.error = ""
        self.state.last_change = time.time()
        # 关闭当前连接，让主循环用新配置重连
        conn = self.state.conn
        if conn is not None:
            try:
                import asyncio
                asyncio.create_task(conn.close(code=4004, reason="config changed"))
            except Exception:
                pass


def self_port() -> int:
    """Origin 端口校验用；由启动时注入实际端口。"""
    return _PORT.value


class _Port:
    value = 18770


_PORT = _Port()


def set_local_port(port: int) -> None:
    _PORT.value = port
