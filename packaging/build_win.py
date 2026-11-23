#!/usr/bin/env python3
"""在 Windows 本机重新打包设备端单文件 exe。

用法（Windows PowerShell，务必用 anaconda python）：
    C:\\Users\\user\\anaconda3\\python.exe build_win.py

产物：packaging/dist/team-workers-device-win.exe

【重要】必须用 anaconda 环境打包，不能用系统 Python/Python313：
  - anaconda: Python 3.11.15 + websockets 17.4 + OpenSSL 3.5.5 -> WS 正常
  - Python313: Python 3.13.12 + websockets 16.0 + OpenSSL 3.0.18 -> 握手被重置 WinError 64
  且必须 --add-binary 带上 libssl-3-x64.dll / libcrypto-3-x64.dll，否则运行时 _ssl DLL load failed。
  本脚本会自动切到 anaconda python 重执行（若当前不是）。
"""
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
SRC = ROOT / "prototype" / "ws_remote"
OUT = BASE / "dist"
WS = BASE / "ws_remote_tmp"

# ---- 强制使用 anaconda python（TLS 栈正确）----
CONDA = Path(r"C:\Users\user\anaconda3\python.exe")
if sys.platform == "win32" and CONDA.exists() and Path(sys.executable).resolve() != CONDA.resolve():
    print("切换到 anaconda python 重执行：", CONDA, flush=True)
    rc = subprocess.run([str(CONDA), str(Path(__file__).resolve())]).returncode
    raise SystemExit(rc)

OUT.mkdir(parents=True, exist_ok=True)
WS.mkdir(parents=True, exist_ok=True)

# exec_server.py 兼容两种布局：prototype/exec_server.py（仓库结构）
# 与 Downloads/exec_server.py（公司电脑临时构建结构）
exec_server = ROOT / "prototype" / "exec_server.py"
if not exec_server.exists():
    alt = Path(r"C:\Users\user\Downloads\exec_server.py")
    if alt.exists():
        exec_server = alt
if not exec_server.exists():
    raise SystemExit("找不到 exec_server.py")

# SSL DLL（anaconda 环境）
dll_dir = Path(r"C:\Users\user\anaconda3\Library\bin")
libssl = dll_dir / "libssl-3-x64.dll"
libcrypto = dll_dir / "libcrypto-3-x64.dll"
if sys.platform == "win32" and (not libssl.exists() or not libcrypto.exists()):
    raise SystemExit("找不到 anaconda SSL DLL：%s / %s" % (libssl, libcrypto))

cmd = [
    sys.executable, "-m", "PyInstaller", "--onefile", "--clean", "--noconfirm",
    "--name", "team-workers-device-win",
    "--add-data", f"{exec_server};.",
    "--add-data", f"{SRC / 'ui' / 'device.html'};ui",
    "--add-data", f"{SRC / 'ui' / 'theme.css'};ui",
]
if sys.platform == "win32":
    cmd += ["--add-binary", f"{libssl};.", "--add-binary", f"{libcrypto};."]
cmd += [
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

print("打包（env: %s）：" % sys.version.split()[0], " ".join(str(c) for c in cmd), flush=True)
subprocess.run(cmd, cwd=str(SRC), check=True)
print("完成：", OUT / "team-workers-device-win.exe")
