#!/bin/sh
# 容器化设备端 entrypoint：由环境变量生成 device.json 后 exec 设备端主程序。
# 若 /config/device.json 已存在（高级用户自行挂载），则跳过生成直接使用。
set -eu

CFG=/config/device.json

if [ -f "$CFG" ]; then
  echo "[entrypoint] 使用已挂载的 $CFG（跳过环境变量生成）"
else
  # 身份三要素必须显式提供，否则连不上 Hub；拒绝用空值静默启动。
  if [ -z "${DEVICE_ID:-}" ] || [ -z "${DEVICE_KEY:-}" ]; then
    echo "[entrypoint] 错误：DEVICE_ID 与 DEVICE_KEY 不能为空，请在 compose 的 environment 中填写，" >&2
    echo "[entrypoint]       或挂载已有的 device.json 到 $CFG 后重启容器。" >&2
    exit 1
  fi
  # umask 077：私钥落盘仅本人可读（:600）
  OLD_UMASK=$(umask)
  umask 077
  python3 - "$CFG" <<'PY'
import json
import os
import sys

cfg = {
    "server_url": os.environ.get("SERVER_URL", ""),
    "device_id": os.environ.get("DEVICE_ID", ""),
    "key": os.environ.get("DEVICE_KEY", ""),
    "platform": os.environ.get("PLATFORM", "nas-docker"),
    "display_name": os.environ.get("DISPLAY_NAME", "NAS Docker Node"),
    "allowlist": [x.strip() for x in os.environ.get("ALLOWLIST", "chroot,sh,bash,docker").split(",") if x.strip()],
    "allowed_roots": [os.environ.get("HOST_ROOT", "/host")],
    # 以下键供设备端新增代码读取（另一成员实现）：
    #   host_root   —— FileTools virtual_root 路径映射的宿主根前缀
    #   policy_mode —— 容器端分级策略钩子模式 off/audit_only/enforce
    #   audit_path  —— 策略审计日志路径，默认落 /config 卷（已持久化，重建不丢）
    "host_root": os.environ.get("HOST_ROOT", "/host"),
    "policy_mode": os.environ.get("POLICY_MODE", "enforce"),
    "audit_path": os.environ.get("AUDIT_PATH", "/config/policy-audit.jsonl"),
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
PY
  umask "$OLD_UMASK"
  echo "[entrypoint] 已生成 $CFG（权限 600，platform=${PLATFORM:-nas-docker}）"
fi

# --no-web：容器内禁用本地配置页；--no-browser：不自动开浏览器；"$@" 透传追加参数
exec python3 /app/prototype/ws_remote/device.py \
  --config "$CFG" --no-web --no-browser "$@"
