import asyncio
import json
import os
import socket
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


def http_json(url: str, method: str = "GET", payload: dict | None = None, token: str | None = None):
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
        env = {
            **os.environ,
            "CLAWPI_DATABASE_URL": f"sqlite:///{Path(self.tempdir.name, 'test.db').as_posix()}",
            "CLAWPI_JWT_SECRET": "test-secret-not-for-production",
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
        status, session = http_json(
            f"{self.base}/v1/auth/register",
            "POST",
            {"name": "测试用户", "email": "user@example.com", "password": "password123"},
        )
        self.assertEqual(status, 201)
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
            {"name": "其他用户", "email": "other@example.com", "password": "password123"},
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
                self.assertEqual(model_request["model"], "gpt-next")
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
                            "provider": "openai",
                            "model": "gpt-next",
                            "providers": [
                                {"id": "openai", "label": "OpenAI"},
                                {"id": "deepseek", "label": "DeepSeek"},
                            ],
                            "models": [
                                {
                                    "id": "gpt-next",
                                    "name": "GPT Next",
                                    "reasoning": True,
                                    "contextWindow": 200000,
                                }
                            ],
                        }
                    )
                )
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
                await asyncio.sleep(0.2)

        host = threading.Thread(target=lambda: asyncio.run(fake_host()), daemon=True)
        host.start()
        self.assertTrue(ready.wait(5))

        status, devices = http_json(f"{self.base}/v1/devices", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(devices[0]["status"], "online")

        status, configured = http_json(
            f"{self.base}/v1/devices/{device['id']}/agent-config",
            "POST",
            {"provider": "openai", "model": "gpt-test", "apiKey": "sk-test-key"},
            token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(configured, {"provider": "openai", "model": "gpt-test"})

        status, configured = http_json(
            f"{self.base}/v1/devices/{device['id']}/agent-config",
            "POST",
            {"provider": "openai", "model": "gpt-next"},
            token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(configured, {"provider": "openai", "model": "gpt-next"})

        status, config = http_json(
            f"{self.base}/v1/devices/{device['id']}/agent-config",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            config,
            {
                "configured": True,
                "provider": "openai",
                "model": "gpt-next",
                "providers": [
                    {"id": "openai", "label": "OpenAI"},
                    {"id": "deepseek", "label": "DeepSeek"},
                ],
                "models": [
                    {
                        "id": "gpt-next",
                        "name": "GPT Next",
                        "reasoning": True,
                        "contextWindow": 200000,
                    }
                ],
            },
        )

        status, reply = http_json(
            f"{self.base}/v1/devices/{device['id']}/messages",
            "POST",
            {"conversationId": "conversation-1", "text": "你好"},
            token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(reply["text"], "测试")
        host.join(timeout=5)

        status, _ = http_json(
            f"{self.base}/v1/devices/{device['id']}/claim", "DELETE", token=token
        )
        self.assertEqual(status, 204)
        self.assertEqual(http_json(f"{self.base}/v1/devices", token=token)[1], [])


if __name__ == "__main__":
    unittest.main()
