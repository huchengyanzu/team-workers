#!/usr/bin/env python3
"""在 Windows 本机重新打包设备端单文件 exe（含配置页 CSRF 修复）。

用法（Windows 命令行 / PowerShell）：
    python -m pip install pyinstaller websockets
    python build_win.py

产物：packaging/dist/nexus-device-win.exe
"""
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
SRC = ROOT / "prototype" / "ws_remote"
OUT = BASE / "dist"
WS = BASE / "ws_remote_tmp"

OUT.mkdir(parents=True, exist_ok=True)
WS.mkdir(parents=True, exist_ok=True)

cmd = [
    sys.executable, "-m", "PyInstaller", "--onefile", "--clean", "--noconfirm",
    "--name", "nexus-device-win",
    "--add-data", f"{SRC / 'exec_server.py'};.",
    "--add-data", f"{SRC / 'ui' / 'device.html'};ui",
    "--add-data", f"{SRC / 'ui' / 'theme.css'};ui",
    "--hidden-import", "websockets",
    "--hidden-import", "exec_server",
    "--hidden-import", "docker_policy",
    "--hidden-import", "shlex",
    "--hidden-import", "locale",
    "--hidden-import", "signal",
    "--hidden-import", "subprocess",
    "--hidden-import", "tempfile",
    "--hidden-import", "threading",
    "--hidden-import", "uuid",
    "--hidden-import", "hmac",
    "--hidden-import", "http.server",
    "--workpath", str(WS),
    "--specpath", str(WS),
    "--distpath", str(OUT),
    str(SRC / "device.py"),
]

print("打包：", " ".join(str(c) for c in cmd), flush=True)
subprocess.run(cmd, cwd=str(SRC), check=True)
print("完成：", OUT / "nexus-device-win.exe")
