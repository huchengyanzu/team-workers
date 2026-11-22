#!/bin/bash
# 生成可双击/启动台启动的 macOS 设备端 app。
# 关键点：CFBundleExecutable 直接指向 Mach-O 可执行文件（不是 shell 脚本），
# 这样 Dock 图标正常显示；不再 codesign --deep，避免二次签坏 PyInstaller 内部二进制。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/packaging/dist"
APP="$OUT/Nexus Remote Device.app"

echo "=== 1/4 构建单文件设备端 ==="
bash "$ROOT/packaging/build_device.sh" macos

echo "=== 2/4 组装 .app bundle ==="
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$OUT/nexus-device-mac" "$APP/Contents/MacOS/nexus-device-mac"
chmod +x "$APP/Contents/MacOS/nexus-device-mac"
cp "$ROOT/packaging/icon.icns" "$APP/Contents/Resources/icon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
  <key>CFBundleDisplayName</key><string>team-workers 设备端</string>
  <key>CFBundleExecutable</key><string>nexus-device-mac</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>CFBundleIdentifier</key><string>com.nexusremote.device</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>team-workers Device</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

echo "=== 3/4 ad-hoc 签名（仅签 bundle，不 deep） ==="
# 文稿目录带 FileProvider/FinderInfo 扩展属性，直接在原地签会报
# "resource fork, Finder information, or similar detritus not allowed"。
# 先在 /tmp 用 ditto 做无属性副本，签完再复制回来。
TMP_ROOT="$(mktemp -d)"
TMP_APP="$TMP_ROOT/Nexus Remote Device.app"
ditto --norsrc --noextattr --noacl "$APP" "$TMP_APP"
xattr -cr "$TMP_APP" || true
codesign --force --sign - "$TMP_APP"
codesign --verify --verbose=2 "$TMP_APP"
rm -rf "$APP"
ditto --norsrc --noextattr --noacl "$TMP_APP" "$APP"
rm -rf "$TMP_ROOT"

echo "=== 4/4 校验 ==="
codesign --verify --verbose=2 "$APP"
/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$APP/Contents/Info.plist"
ls -lh "$APP/Contents/MacOS/nexus-device-mac"
echo "完成：$APP"
echo "安装：cp -R \"$APP\" /Applications/"
