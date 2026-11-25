# team-workers 设备端 · Linux 桌面版

适用于 Ubuntu / Debian / Fedora 等带图形界面的 Linux 发行版。

这个包不重做配置界面，而是复用设备端内置的本地配置页（`http://127.0.0.1:18770/`）：
从应用菜单启动后，会自动打开默认浏览器进入配置页，填入服务端地址、设备 ID 与私钥即可连接。
体验接近普通桌面 App，但配置界面仍然是浏览器页面。服务器 / 容器 / 无桌面环境请继续用命令行版。

## 安装

解压后在本目录执行：

```bash
chmod +x install-desktop.sh
./install-desktop.sh
```

脚本会把程序安装到 `~/.local/bin/team-workers-device-linux`，并写入用户级菜单项和图标，**不需要 sudo**。
之后在应用菜单搜索「team-workers 设备端」启动即可。

如果浏览器没有自动打开，手动访问 `http://127.0.0.1:18770/`。

## 卸载

```bash
rm -f "$HOME/.local/bin/team-workers-device-linux" \
      "$HOME/.local/share/applications/team-workers-device.desktop" \
      "$HOME/.local/share/icons/hicolor/256x256/apps/team-workers-device.png"
```

配置文件默认在程序同目录（`~/.local/bin/device.json` 或 `team-workers-device.json`），如需彻底清理请自行删除。
