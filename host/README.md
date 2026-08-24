# ClawPi Host

AI 主机侧程序。`daemon.py` 是 Linux 真机守护程序，`simulator.py` 用于 Windows 本地联调。

## 真机要求

- 64 位 Debian / Raspberry Pi OS，Python 3.11+
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

至少配置 `CLAWPI_SERVER_URL` 和每台机器唯一的 `CLAWPI_SETUP_PASSWORD`。API Key 由用户在 App 中配置，不需要预装到主机镜像。安装脚本只启用服务，不会在配置完成前启动。

首次启动没有 `/var/lib/clawpi/credentials.json`，或者 NetworkManager 未检测到可用网络时，主机都会建立 `ClawPi-序列号` 热点并监听 `192.168.4.1:8090`。运行过程中掉网也会让守护进程退出并由 systemd 自动重启进入热点模式。App 通过 `GET /wifi-networks` 读取主机无线网卡扫描到的附近网络，用户选择 SSID 并填写密码。提交后主机关闭热点、连接家庭网络、认领设备并上线。随后 App 通过 FastAPI 将模型配置实时转发给主机，主机保存到权限为 `0600` 的 `/var/lib/clawpi/agent.json`。

部分无线网卡或驱动不支持在热点模式下主动扫描；此时接口会尝试使用 NetworkManager 缓存，仍然失败时 App 会自动提供手动输入 SSID 的入口。

API Key 不写入 ClawPi 数据库。以后修改同一服务商的模型时不需要重传 Key；更换服务商或更新 Key 时，App 通过 FastAPI 再次实时转发，主机收到后覆盖本地配置。

Pi 以非 root 的 `clawpi` 用户运行。每个 App 会话使用独立且稳定的 Pi session，数据保存在 `/var/lib/clawpi/sessions`；Pi 工作目录是 `/var/lib/clawpi/workspace`。

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
