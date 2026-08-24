# ClawPi

面向 ClawPi AI 主机的 Android 客户端、云端控制面和主机程序。

## 目录

```text
app/       Android Expo 客户端
backend/   FastAPI 账号、设备管理和消息中继
host/      AI 主机程序与开发模拟器
docs/      产品和架构文档
```

## 快速开始

Android App：

```powershell
cd app
npm install
adb reverse tcp:8081 tcp:8081
adb reverse tcp:8000 tcp:8000
adb reverse tcp:8090 tcp:8090
npm start
```

FastAPI 后端：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:CLAWPI_JWT_SECRET = "replace-with-a-long-random-secret"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Linux AI 主机：

```bash
cd host
sudo ./install.sh
```

模块说明：

- [Android App](app/README.md)
- [FastAPI 后端](backend/README.md)
- [AI 主机](host/README.md)
- [产品定义](docs/PRODUCT.md)
