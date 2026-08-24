import argparse
import asyncio
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from websockets.asyncio.client import connect


def request_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = json.load(error) if error.headers.get_content_type() == "application/json" else {}
        raise RuntimeError(body.get("detail") or f"HTTP {error.code}") from error


def websocket_url(server: str, device_id: str) -> str:
    parsed = urlparse(server)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/v1/hosts/{quote(device_id)}/ws"


def save_credentials(path: Path, claimed: dict, server: str) -> dict:
    credentials = {
        "server": server.rstrip("/"),
        "deviceId": claimed["device"]["id"],
        "hostToken": claimed["hostToken"],
    }
    path.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return credentials


class SetupServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, credentials_path: Path, serial: str, version: str):
        super().__init__(address, SetupHandler)
        self.credentials_path = credentials_path
        self.serial = serial
        self.version = version
        self.result: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=1)
        self.completed = False


class SetupHandler(BaseHTTPRequestHandler):
    server: SetupServer

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/wifi-networks":
            self._send_json(404, {"detail": "接口不存在"})
            return
        self._send_json(
            200,
            {
                "networks": [
                    {"ssid": "ClawPi Lab", "signal": 92, "secured": True},
                    {"ssid": "Home WiFi", "signal": 76, "secured": True},
                    {"ssid": "Guest", "signal": 48, "secured": False},
                ]
            },
        )

    def do_POST(self) -> None:
        if self.path != "/provision":
            self._send_json(404, {"detail": "接口不存在"})
            return
        if self.server.completed:
            self._send_json(409, {"detail": "主机已经完成配网"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65_536:
                raise ValueError("请求大小无效")
            payload = json.loads(self.rfile.read(length))
            cloud_url = str(payload["cloudUrl"]).rstrip("/")
            claim_token = str(payload["claimToken"])
            if not payload.get("wifiName") or len(str(payload.get("wifiPassword", ""))) < 8:
                raise ValueError("Wi-Fi 信息无效")
            claimed = request_json(
                f"{cloud_url}/v1/provisioning/claim",
                {
                    "claimToken": claim_token,
                    "serial": self.server.serial,
                    "version": self.server.version,
                },
            )
            credentials = save_credentials(self.server.credentials_path, claimed, cloud_url)
            self.server.completed = True
            self.server.result.put((cloud_url, credentials))
            self._send_json(200, {"accepted": True, "device": claimed["device"]})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._send_json(400, {"detail": str(error) or "配网数据无效"})
        except RuntimeError as error:
            self._send_json(502, {"detail": str(error)})

    def log_message(self, format: str, *args) -> None:
        return


def wait_for_provisioning(args) -> tuple[str, dict]:
    server = SetupServer(
        (args.setup_host, args.setup_port), args.credentials, args.serial, args.version
    )
    print(f"等待 App 配网：http://{args.setup_host}:{args.setup_port}/provision")
    try:
        server.serve_forever()
        return server.result.get_nowait()
    finally:
        server.server_close()


async def heartbeat(websocket) -> None:
    while True:
        await asyncio.sleep(20)
        await websocket.send(json.dumps({"type": "heartbeat"}))


async def run_host(server: str, credentials: dict) -> None:
    delay = 1
    while True:
        try:
            async with connect(
                websocket_url(server, credentials["deviceId"]),
                additional_headers={"Authorization": f"Bearer {credentials['hostToken']}"},
            ) as websocket:
                delay = 1
                heartbeat_task = asyncio.create_task(heartbeat(websocket))
                print(f"主机已连接：{credentials['deviceId']}")
                try:
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        if message.get("type") == "agent.configure":
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "agent.configured",
                                        "requestId": message.get("requestId"),
                                        "provider": message.get("provider"),
                                        "model": message.get("model") or "",
                                    },
                                    ensure_ascii=False,
                                )
                            )
                            continue
                        if message.get("type") != "chat.request":
                            continue
                        text = f"Pi agent 模拟回复：已收到“{message['text']}”"
                        midpoint = max(1, len(text) // 2)
                        for chunk in (text[:midpoint], text[midpoint:]):
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "chat.delta",
                                        "requestId": message["requestId"],
                                        "delta": chunk,
                                    },
                                    ensure_ascii=False,
                                )
                            )
                            await asyncio.sleep(0.15)
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "chat.complete",
                                    "requestId": message["requestId"],
                                    "messageId": f"sim-{time.time_ns()}",
                                }
                            )
                        )
                finally:
                    heartbeat_task.cancel()
        except (OSError, TimeoutError, RuntimeError) as error:
            print(f"连接断开：{error}；{delay} 秒后重试")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def main() -> None:
    parser = argparse.ArgumentParser(description="ClawPi AI 主机模拟器")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--claim-token")
    parser.add_argument("--serial", default="CLAWPI-SIM-001")
    parser.add_argument("--version", default="ClawPi OS simulator")
    parser.add_argument("--setup-host", default="0.0.0.0")
    parser.add_argument("--setup-port", type=int, default=8090)
    parser.add_argument(
        "--credentials", type=Path, default=Path(__file__).with_name("clawpi-host.json")
    )
    args = parser.parse_args()

    if args.claim_token:
        claimed = request_json(
            f"{args.server.rstrip('/')}/v1/provisioning/claim",
            {"claimToken": args.claim_token, "serial": args.serial, "version": args.version},
        )
        credentials = save_credentials(args.credentials, claimed, args.server)
        print(f"绑定完成：{claimed['device']['name']} ({claimed['device']['serial']})")
    elif args.credentials.exists():
        credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
        args.server = credentials.get("server", args.server)
    else:
        args.server, credentials = wait_for_provisioning(args)
        print(f"自动绑定完成：{credentials['deviceId']}")

    asyncio.run(run_host(args.server.rstrip("/"), credentials))


if __name__ == "__main__":
    main()
