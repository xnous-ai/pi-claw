# ClawPi FastAPI Backend

ClawPi 的账号、设备控制面和实时中继。聊天正文和模型 API Key 均不写入数据库，Pi agent 运行在 AI 主机上。

## 本地运行

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:CLAWPI_JWT_SECRET = "replace-with-a-long-random-secret"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

默认使用当前目录的 `clawpi.db`。服务启动后可访问：

- API：`http://127.0.0.1:8000`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`

切换 PostgreSQL：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[postgres]"
$env:CLAWPI_DATABASE_URL = "postgresql+psycopg://user:password@127.0.0.1/clawpi"
```

## 配网绑定

1. App 登录后调用 `POST /v1/provisioning/sessions`，申请十分钟有效的 `claimToken`。
2. App 连接主机热点，将家庭 Wi-Fi 和 `claimToken` 写入主机。
3. 主机联网后调用 `POST /v1/provisioning/claim`，换取只返回一次的 `hostToken`。
4. 主机保存 `hostToken`，连接 `/v1/hosts/{deviceId}/ws`。
5. App 调用 `POST /v1/devices/{deviceId}/agent-config`，FastAPI 将模型配置实时转发给在线主机并等待回执。

家庭 Wi-Fi 密码只应在 App 和主机的局域网配网连接中传递，不应发送到云端后端。

模型 API Key 会经过 FastAPI 进程内存和主机 WebSocket，但不落库、不写日志。更新同一服务商的模型时可以省略 `apiKey`；更换服务商时主机会要求提供新 Key。生产环境必须使用 HTTPS/WSS。

主机模拟器和后续真实守护程序位于 `host/`，运行方式见 [host/README.md](../host/README.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试覆盖注册、一次性令牌、跨账号隔离、自动绑定、模型配置转发、无 Key 模型更新、主机 WebSocket 上线、消息往返和解绑。
