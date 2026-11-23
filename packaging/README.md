# team-workers 设备端 · 三端可执行程序

把本目录的程序拷到任意机器，**双击/运行即可**——不需要装 Python、不需要装依赖。首次运行后用浏览器填一次服务端地址和私钥，就永久上线（重启自动重连）。

## 三个平台

| 平台 | 文件 | 运行方式 |
|---|---|---|
| macOS (Apple Silicon) | `dist/team-workers-device-mac` | 终端里 `./team-workers-device-mac`，或双击（会开终端） |
| Linux (x86_64) | `dist/team-workers-device-linux` | `chmod +x team-workers-device-linux && ./team-workers-device-linux` |
| Windows (x86_64) | `dist/team-workers-device-win.exe` | 双击运行 |

> 单文件内置：执行内核（4 工具）、文件工具、Web 配置页。配置文件 `device.json` 生成在**程序运行目录**，可随时用配置页改。

## 首次配置（三端相同）

1. 运行程序，它会**自动用系统默认浏览器打开配置页** `http://127.0.0.1:18770/`（终端也会打印这个地址）；18770 被占用时自动试 18771…，浏览器会跟着打开实际端口
2. 配置页只绑本机回环，别人访问不到；若浏览器没弹出，手动打开终端里打印的地址即可
3. 填写：
   - **服务端地址**：`wss://hub.example.com/ws`
   - **设备 ID** 和 **私钥**：由服务端管理台创建设备时下发（只在创建时显示一次）
   - **显示名 / 平台**：随便填，平台会自动识别
   - **命令白名单**：点「全选」或按需勾选
   - **文件访问范围**：默认「不限目录」——文件工具可读写这台机器上的任意路径（适合工作区多、无法逐一列举的机器）；写入仍需二次确认、单文件上限 8MB。若只想开放部分目录，选「仅指定目录」，再点「＋ 选择目录」逐层点选添加（不用手写路径）。
4. 点「保存并连接」，状态卡显示「在线」即成功

之后每次启动自动用保存的配置连服务端，断线自动重连。

> **关闭进程 / 开机自启动**：三端配置页顶部都有「开机自启动」开关和「退出程序」按钮。
> - Windows：开启后写入当前用户的启动项（HKCU Run），登录后后台启动；
> - macOS：写入用户级 LaunchAgent，登录后后台启动且不弹浏览器（详见下方 macOS 段）；
> - Linux：暂无一键开关，开关会置灰，可自行加 systemd/cron。
>
> 点「退出程序」即可优雅关闭本机进程，不用去 Dock 或托盘里找图标。
> 开机自启走后台启动，**不会弹浏览器**；手动双击运行才会弹。

> **关于私钥**：出于安全，已保存的私钥不会在配置页回显。以后要改白名单、文件访问范围等，
> 直接改完保存即可——私钥留空表示保持不变；只有想更换私钥时才在私钥框填新值。
> 首次配置时私钥必填。

## 获取设备凭证（device_id + 私钥）

在服务端管理台 `https://hub.example.com/` 登录后 → 设备管理 → 创建设备 → 复制下发的 ID 和私钥（私钥只显示一次）。

## 排错

- 配置页打不开：确认程序在运行；端口 18770 被占用时程序会自动试 18771…
- 状态「认证失败」：device_id 或私钥不对，重新核对；私钥可能已被服务端重置
- 状态「离线」但配置对：设备是出站连接，不需要公网 IP/开端口，但要能对该域名 8443 做 TLS 握手。已实测：本机 Mac 与云端可正常连；**windows-dev 所在网络对 8443 的 TLS 握手有中间设备拦截（ConnectionReset），该网络下连不上**——换网络或用手机热点可验证。这是网络环境问题，不是程序问题。
- Windows 双击后窗口一闪而过：在 cmd 里 `team-workers-device-win.exe` 运行看报错
- Windows 配置页保存时报 `CSRF check failed`：这是**旧版 exe** 的问题——它内置的页面漏了 `X-Local-Config` 头。
  当前 `dist/team-workers-device-win.exe` **已重新打包并实测修复**（带头 POST 200、无头 403），换上新 exe 即可正常网页保存。
  若一时无法替换，也可用 `windows-setup.ps1` 直接写配置绕过。

## macOS 安装（启动台 + 登录自启）

已安装应用到 `/Applications/team-workers-device.app`，启动台显示「team-workers 设备端」。
app 的 Dock 图标正常显示；如 Dock 被系统设为自动隐藏，把鼠标移到屏幕底部即可看到。
配置保存在 `~/Library/Application Support/team-workers/device.json`（权限 600）。

**关闭进程 / 开关自启**：打开自动弹出的配置页（`http://127.0.0.1:18770/` 或被占用时的 18771…），
顶部有「开机自启动」勾选框和「退出程序」按钮：
- 勾选「开机自启动」会写入用户级 `~/Library/LaunchAgents/com.teamworkers.device.plist`，登录后自动后台启动，**不自动弹浏览器**；
- 点「退出程序」会优雅关闭本机设备端进程，无需在 Dock 上找图标。

旧的 LaunchAgent plist（指向已废弃的 `launcher` 脚本）会在首次打开新版配置页后由开关重写；
如需手工清理：`launchctl bootout gui/$(id -u)/com.teamworkers.device` 后删除该 plist。

## Windows CSRF 报错的解决方式

**首选：换成修复版 exe**。`dist/team-workers-device-win.exe` 已于 2026-11-20 重打包并在 Windows 实测：
配置页带头 POST 返回 200、无头 POST 返回 403，网页保存不再报 CSRF。用新 exe 替换家里机器上旧的即可。

**备用：不改 exe 直接写配置**。把 `dist/windows-setup.ps1` 与 exe 放同一目录，
右键「使用 PowerShell 运行」，按提示填设备 ID 和私钥，脚本直接写 `device.json`，重启即上线。

**自行重打**：在 Windows 上先 `python -m pip install pyinstaller websockets`，
再 `python build_win.py`，产物覆盖 `dist/team-workers-device-win.exe`。

## skill 分发

`dist/device-remote/`（和 `device-remote.zip`）是可分发的 agent skill。
安装：复制到 `~/.workbuddy/skills/device-remote/` 等 skills 目录，设置 `REMOTE_SERVER` / `REMOTE_TOKEN` 后即可使用。

## 自己重新打包（可选）

`build_device.sh` 是打包脚本，需在**对应平台本机**运行（PyInstaller 不能跨平台）：
- Mac：`bash build_device.sh macos`
- Linux：`bash build_device.sh linux`
- Windows：在 Windows 上运行 `packaging/build_win.py`（脚本已补齐，见上）

源码在 `../prototype/ws_remote/`（device.py / device_web.py / file_tools.py）+ `../prototype/exec_server.py` + ui 页面。
