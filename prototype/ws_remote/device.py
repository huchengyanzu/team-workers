#!/usr/bin/env python3
"""WebSocket 远程控制设备端原型。

职责：读本地配置（server_url + device_id + key）→ 连服务端 WebSocket → 启动即上线 →
收到 command 帧后调用本地 exec_server 的 McpServer.dispatch() 执行（复用现有 4 工具与
三平台分支）→ 把结果回传。断线指数退避重连，心跳保活。

配置示例（device.json）：
  {
    "server_url": "ws://127.0.0.1:8765",
    "device_id": "dev-xxxx",
    "key": "创建设备时下发的私钥",
    "display_name": "我的 Mac",
    "platform": "mac",
    "allowlist": ["echo", "pwd", "ls", "whoami", "cat", "sleep"]
  }

本文件是原型，不是产品。执行内核完全复用 prototype/exec_server.py，不重新实现工具。
"""

from __future__ import annotations

import asyncio
import argparse
import json
import webbrowser
import os
import platform
import shutil
import sys
import time
from pathlib import Path

import websockets
from websockets.asyncio.client import connect as ws_connect

# 复用现有执行内核：源码运行时 exec_server.py 在上级 prototype/；
# PyInstaller 单文件打包时它作为数据文件解到 _MEIPASS 顶层，已在 sys.path。
_MEIPASS = getattr(sys, "_MEIPASS", None)
THIS_DIR = Path(_MEIPASS) if _MEIPASS else Path(__file__).resolve().parent
PROTOTYPE_DIR = THIS_DIR if _MEIPASS else THIS_DIR.parent
if str(PROTOTYPE_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_DIR))
from exec_server import Executor, McpServer  # noqa: E402
from device_web import DeviceState, DeviceWebApi, set_local_port  # noqa: E402
from file_tools import FileTools  # noqa: E402
from docker_policy import DockerPolicy  # noqa: E402


def collect_info() -> dict:
    info = {
        "platform": platform.system(),
        "arch": platform.machine(),
        "hostname": platform.node(),
        "python": platform.python_version(),
    }
    try:
        info["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    try:
        info["disk_free_gb"] = round(shutil.disk_usage(".").free / 1024**3, 1)
    except Exception:
        pass
    return info


def build_server(allowlist: list[str] | None):
    kwargs = {}
    if allowlist:
        kwargs["allowlist"] = allowlist
    executor = Executor(**kwargs)
    return McpServer(executor)


async def run(state: DeviceState, once: bool = False) -> None:
    """连接主循环。配置热更新：device_web 保存配置后会关闭当前 ws，
    本循环感知 generation 变化后用新配置重连；执行内核随 allowlist 重建。"""
    backoff = 1.0
    while True:
        cfg = dict(state.config)
        if not cfg.get("server_url") or not cfg.get("device_id") or not cfg.get("key"):
            state.status = "unconfigured"
            await asyncio.sleep(1.0 if not once else 0.2)
            if once:
                return
            continue
        gen_at_start = state.generation
        server = build_server(cfg.get("allowlist"))
        files = FileTools(cfg.get("allowed_roots") or [], virtual_root=cfg.get("host_root"))
        policy_mode = cfg.get("policy_mode") or "off"
        # 默认 off：完全不实例化策略，三端行为与引入本特性前逐字节一致
        # （不建审计目录、不写日志、不做任何路径映射之外的判断）。
        if policy_mode == "off":
            policy = None
        else:
            # audit_path 由 device.json（entrypoint 生成）提供，原样透传给 DockerPolicy；
            # 不回退到容器内 $HOME 临时层，保证审计随 /config 卷持久化。
            try:
                policy = DockerPolicy(policy_mode, audit_path=cfg.get("audit_path"))
            except Exception:
                policy = None  # 策略初始化失败不影响设备主循环，降级为无策略
        info = collect_info()
        if cfg.get("platform"):
            info["label"] = cfg["platform"]
        state.status = "connecting"
        try:
            # proxy=None：显式禁用系统代理。设备直连公网服务端，不走本机代理；
            # 否则在设置了 HTTP(S)/SOCKS 代理的终端里，websockets 会读代理环境变量，
            # SOCKS 代理还需要未打包的 python-socks 依赖，导致启动即崩溃。
            async with ws_connect(cfg["server_url"], open_timeout=10,
                                  ping_interval=20, ping_timeout=20,
                                  proxy=None) as ws:
                state.conn = ws
                await ws.send(json.dumps({
                    "type": "auth", "device_id": cfg["device_id"], "key": cfg["key"],
                    "platform": cfg.get("platform", info["platform"]), "info": info,
                }, ensure_ascii=False))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                ack = json.loads(raw)
                if not ack.get("ok"):
                    reason = ack.get("reason", "unknown")
                    state.status = "auth_failed"
                    state.error = reason
                    print("[device] auth 失败：%s；本地配置页保持可用，请在页面更新凭证后保存" % reason, flush=True)
                    state.conn = None
                    # 关键：不退出主循环。保留本地配置页，用户可进页面改私钥/设备ID并保存。
                    # 分段 sleep：用户一保存（generation 变化）立即重连，无需等满间隔；
                    # 未改动时每 10 秒才重试一次，避免快速刷屏。
                    backoff = 10.0
                    waited = 0.0
                    while waited < backoff and state.generation == gen_at_start:
                        await asyncio.sleep(0.5)
                        waited += 0.5
                else:
                    state.status = "online"
                    state.error = ""
                    print("[device] 已上线：%s（%s）" % (cfg["device_id"], cfg["server_url"]), flush=True)
                    backoff = 1.0

                    async def heartbeat():
                        while True:
                            await asyncio.sleep(20)
                            try:
                                await ws.send(json.dumps({
                                    "type": "heartbeat", "info": collect_info(),
                                }, ensure_ascii=False))
                            except Exception:
                                return

                    hb = asyncio.create_task(heartbeat())
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if msg.get("type") != "command":
                                continue
                            task_id = msg.get("task_id")
                            tool = msg.get("tool")
                            arguments = msg.get("arguments") or {}
                            if not isinstance(arguments, dict):
                                # 畸形帧归一化为 {}：避免 .get 抛异常打断主循环；
                                # 同时让策略按空参判红（exec 无 command），不降级 allow 绕过。
                                arguments = {}

                            # ---- 设备端本地策略钩子（服务端无感；容器端异常 fail-closed）----
                            # 容器节点持有宿主 root，策略不可用时宁可拒绝/要求确认也不能放行：
                            # 任何异常/空返回都不得降级为 allow。三端 policy=None 不进此分支。
                            if policy is None:
                                verdict = {"action": "allow", "risk": "green"}
                            else:
                                try:
                                    verdict = policy.check(tool, arguments)
                                    if not isinstance(verdict, dict):
                                        raise ValueError("策略返回格式异常: %r" % (verdict,))
                                except Exception as exc:
                                    # 策略异常绝不降级 allow：先签发一次性确认令牌按红级拦截；
                                    # 连令牌都签不出时才直接 deny。
                                    try:
                                        verdict = policy.issue_confirm_token(
                                            tool, arguments,
                                            "策略检查异常，按高危拦截: %s" % exc)
                                    except Exception as exc2:
                                        verdict = {"action": "deny", "risk": "red",
                                                   "reason": "策略异常且无法签发确认令牌，拒绝执行: %s" % exc2}
                            # 空 action 同样不得降级 allow：容器节点持有宿主 root，
                            # 策略结果缺失一律视为高危拦截（签令牌时已带 action，
                            # 此处仅防御 check 将来实现回归返回空 dict 的情况）。
                            action = verdict.get("action") or "deny"
                            result = None
                            if action == "deny":
                                result = {"ok": False,
                                          "error": verdict.get("reason") or "策略拒绝",
                                          "error_code": "policy_denied"}
                            elif action == "confirm_required":
                                # red 且无有效 confirm_token：回传一次性令牌，跳过执行
                                result = {"ok": False, "error_code": "confirm_required",
                                          "error": verdict.get("reason") or "高危操作需二次确认",
                                          "confirm_token": verdict.get("confirm_token"),
                                          "preview": verdict.get("preview"),
                                          "expires_at": verdict.get("expires_at")}
                            elif action == "ack_required" and arguments.get("ack_risk") is not True:
                                # yellow 且未确认风险：跳过执行（带 ack_risk=true 时 check 已返回 allow）
                                result = {"ok": False, "error_code": "ack_required",
                                          "error": verdict.get("reason") or "需确认风险"}

                            if result is None:
                                if tool in ("file_read", "file_write", "file_list"):
                                    # 文件工具：走独立实现（路径白名单 + base64）
                                    try:
                                        if tool == "file_read":
                                            result = files.read(arguments.get("path", ""))
                                        elif tool == "file_write":
                                            result = files.write(
                                                arguments.get("path", ""),
                                                arguments.get("content_b64", ""),
                                                bool(arguments.get("overwrite", False)),
                                            )
                                        else:
                                            result = files.list_dir(arguments.get("path", ""))
                                    except Exception as exc:
                                        result = {"ok": False, "error": "文件工具异常: %s" % exc}
                                else:
                                    # 转成现有 exec_server 认识的工具调用，复用 dispatch
                                    rpc = {
                                        "jsonrpc": "2.0", "id": task_id, "method": "tools/call",
                                        "params": {"name": tool, "arguments": arguments},
                                    }
                                    resp = server.dispatch(rpc) or {}
                                    if "error" in resp:
                                        result = {"ok": False, "error": resp["error"]}
                                    else:
                                        content = (resp.get("result") or {}).get("content") or []
                                        if content and content[0].get("type") == "text":
                                            try:
                                                result = json.loads(content[0]["text"])
                                            except json.JSONDecodeError:
                                                result = {"ok": True, "output": content[0]["text"]}
                                        else:
                                            result = {"ok": False, "error": "工具返回格式异常"}

                            # ---- 本地审计 + 风险字段合并（失败均不影响主流程）----
                            try:
                                if isinstance(result, dict):
                                    result["risk"] = verdict.get("risk")
                            except Exception:
                                pass
                            try:
                                if policy is not None:
                                    policy.audit(task_id, tool, arguments, verdict, result)
                            except Exception:
                                pass

                            await ws.send(json.dumps({
                                "type": "result", "task_id": task_id, **result,
                            }, ensure_ascii=False))
                            print("[device] 任务 %s 完成：ok=%s" % (task_id, result.get("ok")), flush=True)
                    finally:
                        hb.cancel()
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            state.status = "offline"
            state.error = "%s: %s" % (type(exc).__name__, exc)
            print("[device] 连接断开：%s；%g 秒后重连" % (type(exc).__name__, backoff), flush=True)
            if once:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        except KeyboardInterrupt:
            return
        finally:
            state.conn = None
        if state.generation != gen_at_start:
            backoff = 1.0  # 配置变更：立即用新配置重连
            continue
        if once:
            return


def main() -> None:
    # 从 .app 启动时没有命令行参数，这里给一套适合桌面程序的默认值：
    # 配置写用户目录、固定本机配置页端口、自动开浏览器（开机自启经 NEXUS_NO_BROWSER=1 关掉）。
    _exe = str(Path(sys.executable).resolve())
    app_mode = (bool(getattr(sys, "frozen", False)) and sys.platform == "darwin"
                and ".app/Contents/" in _exe)
    default_config = (str(Path.home() / "Library" / "Application Support" /
                          "Nexus Remote" / "device.json") if app_mode else "device.json")

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=default_config, help="设备配置文件路径")
    ap.add_argument("--once", action="store_true", help="只连接一次，断开即退出（测试用）")
    ap.add_argument("--web-port", type=int, default=0,
                    help="本地配置页端口；0=自动选 18770（被占用则 18771…），-1=禁用")
    ap.add_argument("--no-web", action="store_true", help="禁用本地配置页")
    # 三端统一：启动即自动打开本地配置页；开机自启用 --no-browser / NEXUS_NO_BROWSER=1 抑制。
    ap.add_argument("--open-browser", action="store_true",
                    default=(not os.environ.get("NEXUS_NO_BROWSER")),
                    help="配置页就绪后用系统默认浏览器自动打开本地配置页（默认开启）")
    ap.add_argument("--no-browser", action="store_true",
                    help="不自动打开浏览器（开机自启/后台运行时用）")
    args = ap.parse_args()
    if args.no_browser:
        args.open_browser = False

    # 双保险：websockets 已显式 proxy=None，这里再清掉环境变量，
    # 防止任何底层库隐式读取系统代理导致 wss 握手异常。
    for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                       "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(_proxy_var, None)

    cfg_path = Path(args.config)
    # utf-8-sig 容忍 Windows 记事本/PowerShell 保存的 BOM（否则 json.loads 报 Unexpected UTF-8 BOM）
    config = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.is_file() else {}
    state = DeviceState(config=config, config_path=str(cfg_path),
                        last_change=time.time() if config else 0.0)
    web = None
    web_port = 0
    if not args.no_web:
        web_port = args.web_port
        if web_port == 0:
            for candidate in range(18770, 18780):
                import socket
                s = socket.socket()
                try:
                    s.bind(("127.0.0.1", candidate))
                    web_port = candidate
                    break
                except OSError:
                    continue
                finally:
                    s.close()
        set_local_port(web_port)
        web = DeviceWebApi(state, web_port, collect_info)
    try:
        async def _main():
            server = None
            if web:
                server = await asyncio.start_server(web.handle, "127.0.0.1", web_port)
                print("[device] 本地配置页 http://127.0.0.1:%d/ （仅回环，免登录）" % web_port,
                      flush=True)
                if args.open_browser:
                    # 服务已监听，异步打开默认浏览器，避免阻塞主循环/Dock 卡在启动态
                    try:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None, webbrowser.open, "http://127.0.0.1:%d/" % web_port)
                    except Exception as exc:
                        print("[device] 自动打开浏览器失败，请手动访问 http://127.0.0.1:%d/ (%s)"
                              % (web_port, exc), flush=True)
            run_task = asyncio.create_task(run(state, once=args.once))

            async def _watch_shutdown():
                # 配置页“退出程序”只置标志，由这里取消主循环，保证连接与端口优雅收尾
                while not state.shutdown:
                    await asyncio.sleep(0.2)
                run_task.cancel()

            watcher = asyncio.create_task(_watch_shutdown())
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            finally:
                watcher.cancel()
                if server:
                    server.close()
                    await server.wait_closed()
                if state.shutdown:
                    print("[device] 已按用户请求退出", flush=True)
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
