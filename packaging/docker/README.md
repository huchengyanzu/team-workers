# NAS 容器化设备端（第四种设备节点）

把现有设备端源码跑进一个特权容器，作为第四种设备节点连 Hub 上线。控制端仍用原 `agent CLI`（`remote.py`），服务端零改动。

## ⚠️ 一屏知情声明（部署前必读）

- **`privileged: true` + `-v /:/host` = 把宿主 root 交给容器**。容器一旦被攻破（依赖漏洞、私钥泄露、命令注入），攻击者可直接读写宿主全部文件、杀任意进程、控制宿主 Docker 及所有容器。这是你明确要求的"权限越大越好、控制 NAS 上所有东西"的必然结果，容器技术无法再收窄。
- **挂载 `docker.sock` 等价于宿主 root**（能创建特权容器即能拿到宿主），这是 Docker 的已知性质，不是本方案的额外放大。
- **不要公网裸奔**：平台账号/控制 token 泄露即等于该 NAS root 泄露。请给平台强口令、妥善保管一次性私钥。
- **红级 `confirm_token` 只防 agent 误触/幻觉，不是身份认证**。WS command 帧无 owner_id，token 沿同一连接返回同一 agent 自确认，证明不了"是哪个自然人点的确认"。**不要把它当成防人越权的安全边界**。
- **审计日志存在宿主 `/config` 卷，宿主 root 可删除**；最强权限下审计不可防篡改，这是已知局限。
- 上述取舍均已由你知情并接受，本容器仅按此设计，不再降级为只读。

## 前置条件

- 已在平台创建设备，拿到 `DEVICE_ID` 与一次性 `DEVICE_KEY`（只显示一次）。
- NAS 支持 Docker 且可 SSH 登录；目标为飞牛 fnOS（Debian 12 原生 Docker）或同类。
- 无镜像仓库账号也能用：只在本地 `docker build`，不推送任何仓库。

## 飞牛 fnOS 部署步骤（SSH + compose 主路径）

1. 飞牛 NAS 打开 SSH（控制面板 → 终端/SSH），用 ssh 登录。
2. 把**整个仓库目录**上传到飞牛，例如 `/vol1/docker/team-workers`。至少要确保仓库根的 `prototype/`（含 `exec_server.py` 与 `ws_remote/`）和 `packaging/docker/`（本目录 4 个文件）都在，构建上下文以仓库根为准（见下方"构建上下文"）。
3. SSH 进入仓库根目录，编辑 `packaging/docker/docker-compose.yml`，把 `DEVICE_ID`、`DEVICE_KEY`（及必要时 `SERVER_URL`）改成真实值。
4. 在**仓库根目录**执行构建与启动（用 `-f` 显式指定 compose 文件）：
   ```sh
   cd /vol1/docker/team-workers
   docker compose -f packaging/docker/docker-compose.yml build
   docker compose -f packaging/docker/docker-compose.yml up -d
   ```
5. 看上线日志：
   ```sh
   docker logs -f team-workers-device
   # 出现 “[device] 已上线：dev-xxxx（wss://...）” 即成功
   ```

### 构建上下文（重要）

- 本 compose 的 `build.context: ../..` 是**相对 compose 文件所在目录**（`packaging/docker/`）解析的，即指向**仓库根**；`dockerfile: packaging/docker/Dockerfile` 是相对 context 根的路径。这样 `Dockerfile` 里的 `COPY prototype /app/prototype` 才能找到仓库根的 `prototype/`，并保留 `prototype/ws_remote/device.py` 从 parent 目录 import `exec_server.py` 所需的结构。
- 因此**不能在只有 4 个文件的空目录里 build**——缺 `prototype/` 会在 COPY 步骤失败。必须在仓库根目录用 `docker compose -f packaging/docker/docker-compose.yml build` 执行。
- 配置卷 `./config:/config:rw` 中的 `./config` 相对 **compose 文件所在目录**解析，实际落在 `packaging/docker/config`（不是仓库根，也不是你的当前工作目录）。它持久化 `device.json`（含 device_id/key）与审计日志；卸载时不要删它。

### 图形界面（GUI）说明

飞牛/群晖的 Docker GUI 一般能勾"特权模式"与"host 网络"，但**挂系统级路径（宿主根 `/`）通常有保护、GUI 不完整暴露**，因此宿主根挂载与 host PID/IPC **必须走 compose 文件**。建议全程用 SSH + compose，避免 GUI 与 compose 配置不一致导致权限缺失。

## 🔴 路径约定（务必先读，否则极易误操作）

两个通道对路径的理解已统一为"**绝对路径永远指向宿主**"：

- **exec 通道**：命令里 `chroot /host` 之后的绝对路径即宿主路径。例如
  `chroot /host /bin/sh -c 'cat /etc/passwd'` 读的是**宿主**的 `/etc/passwd`，不是容器的。
- **file 工具通道**：配置了 `virtual_root=/host` 后，传 `/etc/passwd` 会自动映射为 `/host/etc/passwd`，同样指向**宿主**；写显式 `/host/etc/passwd` 也兼容（幂等）。
- **后果**：file 工具**永远指向宿主，不提供读取容器自身文件的路径**。想查容器内部状态请用非 chroot 的 `exec`（作用于容器内）或 `docker logs`。
- 访问边界由 `allowed_roots` 决定（`virtual_root` 只做路径前缀映射、本身不构成独立边界）；本交付默认 `allowed_roots` 与 `host_root` 同为 `/host`。`../` 穿越、符号链接逃逸出 `allowed_roots` 会被拒。

## 命令示例（控制端零改动，用现有 remote.py）

```sh
# 只读：列宿主进程 / 读宿主文件（exec 走宿主需 chroot /host，ALLOWLIST 已含 chroot）
python3 skill/device-remote/scripts/remote.py exec dev-xxxx -- chroot /host /bin/sh -c 'ps aux | head -20'
python3 skill/device-remote/scripts/remote.py exec dev-xxxx -- chroot /host /bin/sh -c 'cat /etc/hosts'

# 管理宿主 Docker（CLI 容器已装、走 docker.sock）
python3 skill/device-remote/scripts/remote.py exec dev-xxxx -- docker ps -a

# 文件：读宿主文件（virtual_root 已映射，/etc→/host/etc）
python3 skill/device-remote/scripts/remote.py file-read dev-xxxx /etc/hosts
python3 skill/device-remote/scripts/remote.py file-list dev-xxxx /volume1

# 长任务（docker pull / 大 logs 会超 15s 同步窗口，返回 task_id 后轮询）
python3 skill/device-remote/scripts/remote.py task <task_id>
```

## 升级 / 卸载 / 故障排查

**升级**（私钥不丢）：改源码或 compose 后，在仓库根目录执行
```sh
cd /vol1/docker/team-workers
docker compose -f packaging/docker/docker-compose.yml build && \
docker compose -f packaging/docker/docker-compose.yml up -d
```
`packaging/docker/config` 卷保留 `device.json`（含私钥）与审计日志，重建容器不丢。

**卸载**：在仓库根目录执行
```sh
cd /vol1/docker/team-workers
docker compose -f packaging/docker/docker-compose.yml down
```
**不要加 `--volumes`**——那会删除 `packaging/docker/config` 里的私钥与审计日志，不可逆。

**故障排查**：
| 现象 | 原因 / 处理 |
|---|---|
| `Operation not permitted`（chroot） | 漏了 `privileged: true`，检查 compose 并重建容器 |
| `host_root_missing` | `/` 没挂到 `/host`，或 `HOST_ROOT` 与实际挂载点不一致 |
| `docker_socket_missing` | `docker.sock` 未挂载；chroot 方式仍可用，docker 命令不可用 |
| 下发命令返回 409 离线 | 容器正在重启窗口（Hub 记 offline→online），**非静默丢失**，稍后重试 |
| 容器反复重启 | 检查 `DEVICE_ID/DEVICE_KEY` 是否为空（entrypoint 会报错退出）、Hub 地址是否可达 |

## 双架构说明

- `python:3.11-slim` 官方镜像含 amd64 / arm64 manifest，`docker build` 在本机自动匹配架构，无需额外参数。
- arm64 NAS 上首次 build 较慢属正常；`docker.io` 与 `websockets` 均为多架构可用包。
- 【待确认 / Should 暂不做】若要加速首次上线，可预构建镜像 tar 离线分发（`docker load < image.tar`），与"无镜像仓库、用户自建"不冲突；当前按规格未纳入。
