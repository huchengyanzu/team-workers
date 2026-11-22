#!/usr/bin/env python3
"""控制端 CLI 原型（skill 脚本的雏形）。

用 stdlib urllib 调服务端 REST API：列设备 / 查状态 / 下发命令 / 查任务。
真实交付会把它包进通用 agent skill 的 scripts/ 目录并加 SKILL.md；此处只验证链路。

用法：
  python3 client.py --token <CONTROL_TOKEN> list
  python3 client.py --token <CONTROL_TOKEN> devices
  python3 client.py --token <CONTROL_TOKEN> exec <device_id> echo hi
  python3 client.py --token <CONTROL_TOKEN> task <task_id>
  python3 client.py --token <CONTROL_TOKEN> create <显示名> <平台>   # 创建设备并下发私钥
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def request(base: str, token: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Control-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": "HTTP %s" % exc.code, "detail": exc.read().decode("utf-8", "replace")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8766")
    ap.add_argument("--token", required=True)
    ap.add_argument("action", choices=["list", "devices", "exec", "task", "create"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    if a.action in ("list", "devices"):
        out = request(a.base, a.token, "GET", "/api/devices")
        if "error" in out:
            print(json.dumps(out, ensure_ascii=False)); return
        for d in out.get("devices", []):
            print("%s | %s | %s | %s | %s" % (
                d["device_id"], d["display_name"], d["platform"], d["status"], d["last_seen"]))
    elif a.action == "create":
        name = a.args[0] if a.args else "未命名"
        plat = a.args[1] if len(a.args) > 1 else ""
        out = request(a.base, a.token, "POST", "/api/admin/devices",
                      {"display_name": name, "platform": plat})
        print("device_id: %s\nkey: %s\n（私钥只显示这一次，填到设备端配置）" % (out.get("device_id"), out.get("key")))
    elif a.action == "exec":
        if len(a.args) < 2:
            sys.exit("用法: exec <device_id> <命令> [参数...]（参数含 - 时用 -- 隔开）")
        # 允许 -- 之后出现以 - 开头的命令参数（如 rm -rf 的调试），argparse 会把它们收进 a.args
        device_id, command = a.args[0], a.args[1:]
        out = request(a.base, a.token, "POST", "/api/tasks",
                      {"device_id": device_id, "tool": "exec", "arguments": {"command": command}})
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif a.action == "task":
        if not a.args:
            sys.exit("用法: task <task_id>")
        out = request(a.base, a.token, "GET", "/api/tasks/" + urllib.parse.quote(a.args[0]))
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
