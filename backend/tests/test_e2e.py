import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
    admin_key: str | None = None,
):
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if admin_key:
        headers["X-Admin-Key"] = admin_key
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, json.loads(body) if body else None


class BackendFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        database_path = Path(self.tempdir.name, "test.db")
        with sqlite3.connect(database_path) as database:
            database.execute(
                """
                CREATE TABLE users (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    name VARCHAR(80) NOT NULL,
                    email VARCHAR(320) NOT NULL UNIQUE,
                    password_hash VARCHAR(256) NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        env = {
            **os.environ,
            "CLAWPI_DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
            "CLAWPI_JWT_SECRET": "test-secret-not-for-production",
            "CLAWPI_ADMIN_KEY": "test-admin-key",
            "CLAWPI_ARTIFACT_DIR": str(Path(self.tempdir.name, "artifacts")),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.server = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=Path(__file__).parents[1],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            try:
                if http_json(f"{self.base}/health")[0] == 200:
                    break
            except urllib.error.URLError:
                time.sleep(0.1)
        else:
            self.fail("后端没有启动")

    def tearDown(self) -> None:
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        self.tempdir.cleanup()

    def test_register_bind_relay_and_release(self) -> None:
        with urllib.request.urlopen(f"{self.base}/admin", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("账号管理", response.read().decode("utf-8"))

        status, session = http_json(
            f"{self.base}/v1/auth/register",
            "POST",
            {"name": "测试用户", "phone": "13800138000", "password": "password123"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(session["user"]["phone"], "13800138000")
        self.assertEqual(session["devices"], [])
        token = session["token"]

        status, provisioning = http_json(
            f"{self.base}/v1/provisioning/sessions",
            "POST",
            {"name": "客厅主机"},
            token,
        )
        self.assertEqual(status, 201)

        status, claimed = http_json(
            f"{self.base}/v1/provisioning/claim",
            "POST",
            {
                "claimToken": provisioning["claimToken"],
                "serial": "CP-TEST-001",
                "version": "test-os",
            },
        )
        self.assertEqual(status, 200)
        device = claimed["device"]

        status, _ = http_json(
            f"{self.base}/v1/provisioning/claim",
            "POST",
            {
                "claimToken": provisioning["claimToken"],
                "serial": "CP-TEST-001",
                "version": "test-os",
            },
        )
        self.assertEqual(status, 410)

        _, other_session = http_json(
            f"{self.base}/v1/auth/register",
            "POST",
            {"name": "其他用户", "phone": "13900139000", "password": "password123"},
        )
        status, _ = http_json(
            f"{self.base}/v1/devices/{device['id']}", token=other_session["token"]
        )
        self.assertEqual(status, 404)

        ready = threading.Event()

        async def fake_host() -> None:
            async with connect(
                f"ws://127.0.0.1:{self.port}/v1/hosts/{device['id']}/ws",
                additional_headers={"Authorization": f"Bearer {claimed['hostToken']}"},
            ) as websocket:
                await websocket.recv()
                ready.set()
                config_request = json.loads(await websocket.recv())
                self.assertEqual(config_request["type"], "agent.configure")
                self.assertEqual(config_request["apiKey"], "sk-test-key")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "agent.configured",
                            "requestId": config_request["requestId"],
                            "provider": config_request["provider"],
                            "model": config_request["model"],
                        }
                    )
                )
                model_request = json.loads(await websocket.recv())
                self.assertEqual(model_request["type"], "agent.configure")
                self.assertIsNone(model_request["apiKey"])
                self.assertEqual(model_request["model"], "deepseek-next")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "agent.configured",
                            "requestId": model_request["requestId"],
                            "provider": model_request["provider"],
                            "model": model_request["model"],
                        }
                    )
                )
                read_request = json.loads(await websocket.recv())
                self.assertEqual(read_request["type"], "agent.config.get")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "agent.config",
                            "requestId": read_request["requestId"],
                            "configured": True,
                            "provider": "deepseek",
                            "model": "deepseek-next",
                            "providers": [
                                {"id": "openai", "label": "OpenAI"},
                                {"id": "deepseek", "label": "DeepSeek"},
                            ],
                            "models": [
                                {
                                    "id": "deepseek-next",
                                    "name": "DeepSeek Next",
                                    "reasoning": True,
                                    "contextWindow": 200000,
                                }
                            ],
                        }
                    )
                )
                commands_request = json.loads(await websocket.recv())
                self.assertEqual(commands_request["type"], "agent.commands.get")
                await websocket.send(json.dumps({
                    "type": "agent.commands",
                    "requestId": commands_request["requestId"],
                    "commands": [
                        {
                            "name": "weather",
                            "description": "查询天气",
                            "source": "extension",
                        },
                        {
                            "name": "skill:writer",
                            "description": "写作助手",
                            "source": "skill",
                        },
                    ],
                }))
                system_request = json.loads(await websocket.recv())
                self.assertEqual(system_request["type"], "system.status.get")
                await websocket.send(json.dumps({
                    "type": "system.status",
                    "requestId": system_request["requestId"],
                    "cpuPercent": 18.5,
                    "memoryPercent": 42.0,
                    "memoryUsedBytes": 4_200_000_000,
                    "memoryTotalBytes": 10_000_000_000,
                    "diskPercent": 61.5,
                    "diskUsedBytes": 61_500_000_000,
                    "diskTotalBytes": 100_000_000_000,
                    "sampledAt": "2026-08-26T08:00:00Z",
                }))
                request = json.loads(await websocket.recv())
                self.assertEqual(request["type"], "chat.request")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "chat.delta",
                            "requestId": request["requestId"],
                            "delta": "测试",
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "chat.complete",
                            "requestId": request["requestId"],
                            "messageId": "reply-1",
                        }
                    )
                )
                stream_request = json.loads(await websocket.recv())
                self.assertEqual(stream_request["type"], "chat.request")
                await websocket.send(json.dumps({
                    "type": "chat.progress",
                    "requestId": stream_request["requestId"],
                    "progressId": "progress-1",
                    "text": "我先查询相关资料",
                }))
                await websocket.send(json.dumps({
                    "type": "chat.status",
                    "requestId": stream_request["requestId"],
                    "statusId": "tool-1",
                    "label": "正在查询资料",
                    "state": "running",
                }))
                await websocket.send(json.dumps({
                    "type": "chat.delta",
                    "requestId": stream_request["requestId"],
                    "delta": "我需要你",
                }))
                await websocket.send(json.dumps({
                    "type": "chat.interaction",
                    "requestId": stream_request["requestId"],
                    "interactionId": "choice-1",
                    "method": "select",
                    "title": "选择执行方式",
                    "message": "请选择下一步",
                    "options": ["继续", "停止"],
                }))
                interaction_response = json.loads(await websocket.recv())
                self.assertEqual(interaction_response["type"], "chat.interaction.response")
                self.assertEqual(interaction_response["interactionId"], "choice-1")
                self.assertEqual(interaction_response["response"], {"value": "继续"})
                await websocket.send(json.dumps({
                    "type": "chat.status",
                    "requestId": stream_request["requestId"],
                    "statusId": "tool-1",
                    "label": "资料查询完成",
                    "state": "done",
                }))
                await websocket.send(json.dumps({
                    "type": "chat.delta",
                    "requestId": stream_request["requestId"],
                    "delta": "选择继续",
                }))
                await websocket.send(json.dumps({
                    "type": "chat.complete",
                    "requestId": stream_request["requestId"],
                    "messageId": "reply-stream-1",
                }))
                cancelled_request = json.loads(await websocket.recv())
                self.assertEqual(cancelled_request["type"], "chat.request")
                cancel_message = json.loads(await websocket.recv())
                self.assertEqual(cancel_message, {
                    "type": "chat.cancel",
                    "requestId": cancelled_request["requestId"],
                })
                capability_list = json.loads(await websocket.recv())
                self.assertEqual(capability_list["type"], "capability.list")
                await websocket.send(json.dumps({
                    "type": "capability.result",
                    "requestId": capability_list["requestId"],
                    "data": {"installed": [{
                        "id": "local-skill:writer",
                        "name": "writer",
                        "kind": "skill",
                        "version": "",
                        "local": True,
                        "managed": False,
                    }]},
                }))
                capability_install = json.loads(await websocket.recv())
                self.assertEqual(capability_install["type"], "capability.install")
                self.assertEqual(capability_install["capability"]["id"], "test-extension")
                await websocket.send(json.dumps({
                    "type": "capability.result",
                    "requestId": capability_install["requestId"],
                    "data": {"installed": [{
                        "id": "test-extension",
                        "name": "测试插件",
                        "kind": "extension",
                        "version": "1.0.0",
                    }]},
                }))
                capability_remove = json.loads(await websocket.recv())
                self.assertEqual(capability_remove["type"], "capability.remove")
                await websocket.send(json.dumps({
                    "type": "capability.result",
                    "requestId": capability_remove["requestId"],
                    "data": {"installed": []},
                }))

        host = threading.Thread(target=lambda: asyncio.run(fake_host()), daemon=True)
        host.start()
        self.assertTrue(ready.wait(5))

        status, devices = http_json(f"{self.base}/v1/devices", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(devices[0]["status"], "online")

        status, configured = http_json(
            f"{self.base}/v1/devices/{device['id']}/agent-config",
            "POST",
            {"provider": "deepseek", "model": "deepseek-test", "apiKey": "sk-test-key"},
            token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(configured, {"provider": "deepseek", "model": "deepseek-test"})

        status, configured = http_json(
            f"{self.base}/v1/devices/{device['id']}/agent-config",
            "POST",
            {"provider": "deepseek", "model": "deepseek-next"},
            token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(configured, {"provider": "deepseek", "model": "deepseek-next"})

        status, config = http_json(
            f"{self.base}/v1/devices/{device['id']}/agent-config",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            config,
            {
                "configured": True,
                "provider": "deepseek",
                "model": "deepseek-next",
                "providers": [
                    {"id": "openai", "label": "OpenAI"},
                    {"id": "deepseek", "label": "DeepSeek"},
                ],
                "models": [
                    {
                        "id": "deepseek-next",
                        "name": "DeepSeek Next",
                        "reasoning": True,
                        "contextWindow": 200000,
                    }
                ],
            },
        )

        status, commands = http_json(
            f"{self.base}/v1/devices/{device['id']}/commands",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(commands[0]["name"], "weather")
        self.assertEqual(commands[1]["source"], "skill")

        status, system_status = http_json(
            f"{self.base}/v1/devices/{device['id']}/system-status",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(system_status["online"])
        self.assertEqual(system_status["cpuPercent"], 18.5)
        self.assertEqual(system_status["memoryPercent"], 42.0)
        self.assertEqual(system_status["diskPercent"], 61.5)

        status, reply = http_json(
            f"{self.base}/v1/devices/{device['id']}/messages",
            "POST",
            {"conversationId": "conversation-1", "text": "你好"},
            token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(reply["text"], "测试")

        async def streamed_chat() -> list[dict]:
            events = []
            async with connect(f"ws://127.0.0.1:{self.port}/v1/chat/ws") as websocket:
                await websocket.send(json.dumps({"type": "auth", "token": token}))
                ready_message = json.loads(await websocket.recv())
                self.assertEqual(ready_message["type"], "chat.ready")
                await websocket.send(json.dumps({
                    "type": "chat.start",
                    "clientMessageId": "client-1",
                    "deviceId": device["id"],
                    "conversationId": "conversation-stream",
                    "text": "请执行任务",
                }))
                while True:
                    event = json.loads(await websocket.recv())
                    events.append(event)
                    if event["type"] == "chat.interaction":
                        await websocket.send(json.dumps({
                            "type": "chat.interaction.response",
                            "requestId": event["requestId"],
                            "interactionId": event["interactionId"],
                            "response": {"value": "继续"},
                        }))
                    if event["type"] in ("chat.complete", "chat.error"):
                        return events

        stream_events = asyncio.run(streamed_chat())
        self.assertEqual(
            [event["type"] for event in stream_events],
            [
                "chat.started",
                "chat.progress",
                "chat.status",
                "chat.delta",
                "chat.interaction",
                "chat.status",
                "chat.delta",
                "chat.complete",
            ],
        )
        self.assertEqual(stream_events[1]["text"], "我先查询相关资料")
        self.assertEqual(stream_events[4]["options"], ["继续", "停止"])
        self.assertEqual(stream_events[-1]["message"]["text"], "我需要你选择继续")

        async def cancelled_chat() -> None:
            async with connect(f"ws://127.0.0.1:{self.port}/v1/chat/ws") as websocket:
                await websocket.send(json.dumps({"type": "auth", "token": token}))
                await websocket.recv()
                await websocket.send(json.dumps({
                    "type": "chat.start",
                    "clientMessageId": "client-disconnect",
                    "deviceId": device["id"],
                    "conversationId": "conversation-disconnect",
                    "text": "开始后断开",
                }))
                started = json.loads(await websocket.recv())
                self.assertEqual(started["type"], "chat.started")
                await websocket.send(json.dumps({"type": "chat.cancel"}))
                cancelled = json.loads(await websocket.recv())
                self.assertEqual(cancelled["type"], "chat.cancelled")

        asyncio.run(cancelled_chat())

        status, unauthorized = http_json(f"{self.base}/v1/admin/overview")
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["detail"], "管理密钥无效")
        status, users = http_json(
            f"{self.base}/v1/admin/users?q=13800138000",
            admin_key="test-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(users[0]["phone"], "13800138000")
        status, admin_devices = http_json(
            f"{self.base}/v1/admin/devices?q=CP-TEST-001",
            admin_key="test-admin-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(admin_devices[0]["owner"]["phone"], "13800138000")

        capability_payload = {
            "id": "test-extension",
            "name": "测试插件",
            "kind": "extension",
            "description": "测试能力安装链路",
            "version": "1.0.0",
            "source": "npm:test-extension@1.0.0",
            "permissions": ["网络访问"],
            "enabled": True,
        }
        status, capability = http_json(
            f"{self.base}/v1/admin/capabilities",
            "POST",
            capability_payload,
            admin_key="test-admin-key",
        )
        self.assertEqual(status, 201)
        self.assertTrue(capability["enabled"])

        status, catalog = http_json(
            f"{self.base}/v1/devices/{device['id']}/capabilities",
            token=token,
        )
        self.assertEqual(status, 200)
        local_capability = next(item for item in catalog if item["id"] == "local-skill:writer")
        self.assertTrue(local_capability["installed"])
        self.assertTrue(local_capability["local"])
        self.assertFalse(local_capability["managed"])
        store_capability = next(item for item in catalog if item["id"] == "test-extension")
        self.assertFalse(store_capability["installed"])
        status, installed = http_json(
            f"{self.base}/v1/devices/{device['id']}/capabilities/test-extension",
            "POST",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(installed["installed"][0]["id"], "test-extension")
        status, removed = http_json(
            f"{self.base}/v1/devices/{device['id']}/capabilities/test-extension",
            "DELETE",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(removed["installed"], [])
        host.join(timeout=5)

        status, offline_catalog = http_json(
            f"{self.base}/v1/devices/{device['id']}/capabilities",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(offline_catalog[0]["id"], "test-extension")

        status, _ = http_json(
            f"{self.base}/v1/devices/{device['id']}/claim", "DELETE", token=token
        )
        self.assertEqual(status, 204)
        self.assertEqual(http_json(f"{self.base}/v1/devices", token=token)[1], [])


if __name__ == "__main__":
    unittest.main()
