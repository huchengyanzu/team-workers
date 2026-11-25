#!/usr/bin/env bash
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
BIN="$BASE/team-workers-device-linux"
DESKTOP="$BASE/team-workers-device.desktop"
ICON="$BASE/team-workers-device.png"

for f in "$BIN" "$DESKTOP" "$ICON"; do
  if [ ! -f "$f" ]; then
    echo "缺少文件: $f" >&2
    exit 1
  fi
done

chmod +x "$BIN"
mkdir -p "$HOME/.local/bin" \
         "$HOME/.local/share/applications" \
         "$HOME/.local/share/icons/hicolor/256x256/apps"

install -m 755 "$BIN" "$HOME/.local/bin/team-workers-device-linux"
install -m 644 "$DESKTOP" "$HOME/.local/share/applications/team-workers-device.desktop"
install -m 644 "$ICON" "$HOME/.local/share/icons/hicolor/256x256/apps/team-workers-device.png"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi

echo "已安装 team-workers 设备端（用户级，无需 sudo）。"
echo "从应用菜单启动「team-workers 设备端」，会自动打开本地配置页；"
echo "若浏览器未自动打开，请手动访问 http://127.0.0.1:18770/ 。"
