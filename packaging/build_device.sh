#!/bin/bash
# 打包设备端为单文件可执行程序（在目标平台本机运行此脚本）
# 用法：bash build_device.sh          # 当前平台
#       bash build_device.sh macos   # 显式指定
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/prototype/ws_remote"
OUT="$ROOT/packaging/dist"
PLAT="${1:-$(uname -s)}"
case "$PLAT" in
  Darwin|macos|MacOS) NAME="nexus-device-mac" ;;
  Linux|linux)        NAME="nexus-device-linux" ;;
  *)                  NAME="nexus-device" ;;
esac

echo "=== 打包设备端: $NAME (平台 $PLAT) ==="
mkdir -p "$OUT"
cd "$SRC"

# 单文件 + 内置执行内核/配置页；--add-data 源:目标（mac/linux 用 :）
python3 -m PyInstaller --onefile --clean --noconfirm \
  --name "$NAME" \
  --add-data "../exec_server.py:." \
  --add-data "ui/device.html:ui" \
  --add-data "ui/theme.css:ui" \
  --hidden-import websockets \
  --hidden-import exec_server \
  --hidden-import shlex --hidden-import locale --hidden-import signal \
  --hidden-import subprocess --hidden-import tempfile --hidden-import threading \
  --hidden-import uuid --hidden-import hmac --hidden-import http.server \
  --distpath "$OUT" \
  device.py

echo "=== 产物 ==="
ls -lh "$OUT/$NAME" 2>/dev/null || ls -lh "$OUT/$NAME.exe" 2>/dev/null
echo "=== 用法 ==="
echo "  把可执行文件放到任意目录，直接运行；首次启动会开本机配置页 http://127.0.0.1:18770/"
echo "  填服务端地址 wss://hub.example.com/ws + device_id + 私钥即可上线"
