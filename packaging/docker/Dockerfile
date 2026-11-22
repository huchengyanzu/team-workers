FROM python:3.11-slim

# chroot(coreutils) + docker CLI 合并为一次 apt 调用，减少镜像层：
#   - coreutils 提供 chroot：exec 通道用它进入 /host 执行宿主命令；
#   - docker.io 只安装 CLI、不启动 daemon，配合挂载的 docker.sock 管理宿主 Docker。
RUN apt-get update \
 && apt-get install -y --no-install-recommends coreutils docker.io \
 && rm -rf /var/lib/apt/lists/*

# 与三端唯一的第三方依赖保持一致；锁定版本便于 amd64 / arm64 复现
RUN pip install --no-cache-dir websockets==12.0

WORKDIR /app

# 必须保留 prototype/ 目录结构：
# device.py 非 frozen 时 PROTOTYPE_DIR = 脚本目录.parent，从 parent import exec_server.py，
# 若把 ws_remote 源码平铺到 /app 会导致 import 失败。
COPY prototype /app/prototype

COPY docker-entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && mkdir -p /config

ENV PYTHONUNBUFFERED=1 \
    SERVER_URL="" \
    DEVICE_ID="" \
    DEVICE_KEY="" \
    PLATFORM="nas-docker" \
    HOST_ROOT="/host" \
    ALLOWLIST="chroot,sh,bash,docker" \
    POLICY_MODE="enforce"

ENTRYPOINT ["/app/entrypoint.sh"]
