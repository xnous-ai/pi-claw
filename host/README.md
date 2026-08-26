# ClawPi Host

AI 主机侧程序。`daemon.py` 是 Linux 真机守护程序，`simulator.py` 用于 Windows 本地联调。

## 真机要求

- 64 位 Debian / Ubuntu / Raspberry Pi OS，Python 3.10+
- NetworkManager 与 `nmcli`
- Node.js 22.19+
- Pi `@earendil-works/pi-coding-agent@0.84.2`

## 安装

```bash
cd host
sudo ./install.sh
sudo nano /etc/clawpi/clawpi.env
sudo systemctl start clawpi
sudo journalctl -u clawpi -f
```

在 Debian/Ubuntu 上，如果系统缺少当前 Python 版本的 `venv` 包，安装脚本会通过 `apt-get` 自动安装。

至少配置 `CLAWPI_SERVER_URL` 和每台机器唯一的 `CLAWPI_SETUP_PASSWORD`。API Key 由用户在 App 中配置，不需要预装到主机镜像。首次安装只启用服务，不会在配置完成前启动；更新已运行的服务时，安装脚本会自动停止并重新启动服务。

只有系统没有 IPv4 或 IPv6 默认路由时，主机才会建立 `ClawPi-序列号` 热点并监听 `192.168.4.1:8090`。主机首次启动时如果已经联网，不会切换无线网卡，而是在当前局域网的 `8090` 端口等待绑定。运行过程中掉网会让守护进程退出并由 systemd 自动重启进入热点模式。主机会在开启热点前扫描并缓存附近网络，App 通过 `GET /wifi-networks` 读取该列表，用户选择 SSID 并填写密码。手动刷新时主机会短暂关闭热点、重新扫描并恢复同名热点，App 自动等待重连。提交后主机关闭热点、连接家庭网络、认领设备并上线。随后 App 通过 FastAPI 将模型配置实时转发给主机，主机将配置保存到权限为 `0600` 的 `/var/lib/clawpi/agent.json`。

部分无线网卡或驱动不支持在热点模式下主动扫描；此时接口会尝试使用 NetworkManager 缓存，仍然失败时 App 会自动提供手动输入 SSID 的入口。

API Key 不写入 ClawPi 数据库。以后修改同一服务商的模型时不需要重传 Key；更换服务商或更新 Key 时，App 通过 FastAPI 再次实时转发，主机收到后覆盖本地配置。

ClawPi 守护进程及其启动的 Pi 进程均以 root 运行，不创建额外的系统用户，也不使用 systemd 文件系统沙箱。每个 App 会话使用独立且稳定的 Pi session，数据保存在 `/var/lib/clawpi/sessions`；Pi 工作目录是 `/var/lib/clawpi/workspace`。这意味着 Agent、插件和 Skill 可以修改整台主机，仅应在用户独占且可信的设备上使用。

管理员在云端后台发布 Skill 或插件后，用户可在 App 的“主机 > 能力管理”中安装。Skill 安装到 `/var/lib/clawpi/pi-config/skills`；插件通过 Pi 自带的 `pi install` / `pi remove` 管理。安装状态保存在 `/var/lib/clawpi/capabilities.json`，实际能力代码仍只在用户自己的主机运行。

安装脚本还会部署内置的 `ask_user` Pi 工具。Agent 调用该工具时，App 会显示选择按钮，并把答案送回同一个 Pi RPC 会话继续执行。

## 模拟器

先启动 `backend/`，然后在仓库根目录运行：

```powershell
.\backend\.venv\Scripts\python.exe host\simulator.py --serial "CP-DEMO-001"
```

首次启动时，模拟器会在 `0.0.0.0:8090` 等待 App 提交配网信息。开发时执行 `adb reverse tcp:8090 tcp:8090`，App 就可以通过 `http://127.0.0.1:8090` 模拟访问主机热点。

主机认领成功后会把云端地址、设备 ID 和设备令牌保存在 `host/clawpi-host.json`，不会保存 Wi-Fi 密码。完成首次绑定后可直接重连：

```powershell
.\backend\.venv\Scripts\python.exe host\simulator.py
```

模拟器遵循与真实主机相同的绑定、模型配置、心跳和聊天 WebSocket 协议。模拟器会确认模型配置消息，但不会保存 API Key。

要模拟一台全新的主机，使用独立凭据文件和序列号：

```powershell
.\backend\.venv\Scripts\python.exe host\simulator.py --credentials host\app-dev-host.json --serial CP-APP-001
```
