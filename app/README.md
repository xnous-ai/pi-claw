# ClawPi Android

Android 配套客户端，包含账号登录、无屏主机配网、多主机管理、多会话聊天和账号服务页面。

## 运行

```powershell
cd app
npm install
adb reverse tcp:8081 tcp:8081
adb reverse tcp:8000 tcp:8000
adb reverse tcp:8090 tcp:8090
npm start
```

手机开启 USB 调试并授权当前电脑，然后在 Expo Go 中打开 `exp://127.0.0.1:8081`。默认使用离线模式，未配置后端地址时运行演示模式。

## 接入后端

复制 `app/.env.example` 为 `app/.env.local`。USB 开发时把后端和模拟主机的本地配网端口转发到 Android：

```powershell
adb reverse tcp:8000 tcp:8000
adb reverse tcp:8090 tcp:8090
```

```text
EXPO_PUBLIC_API_URL=http://127.0.0.1:8000
EXPO_PUBLIC_HOST_SETUP_URL=http://127.0.0.1:8090
EXPO_PUBLIC_SUPPORT_EMAIL=support@example.com
```

开发模拟器启动后会在 `8090` 端口等待 App 配网，并提供附近 Wi-Fi 列表。App 创建一次性绑定令牌，用户选择 Wi-Fi 并填写密码后交给主机；主机向后端认领并上线后，App 再通过 FastAPI 设置模型服务和 API Key。FastAPI 只实时转发配置，Key 最终保存在主机本地。真实硬件上，`EXPO_PUBLIC_HOST_SETUP_URL` 应指向主机热点内的固定地址，例如 `http://192.168.4.1`。

## 检查

```powershell
cd app
npm run check
```
