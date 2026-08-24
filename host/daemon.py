import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from websockets.asyncio.client import connect

from simulator import request_json, save_credentials, websocket_url

if os.name != "nt":
    import pwd


PROVIDER_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def machine_serial() -> str:
    configured = os.getenv("CLAWPI_SERIAL", "").strip()
    if configured:
        return configured.upper()
    for path in (Path("/proc/device-tree/serial-number"), Path("/etc/machine-id")):
        try:
            value = path.read_text(encoding="utf-8").replace("\x00", "").strip()
            if value:
                return f"CP-{value[-12:].upper()}"
        except OSError:
            pass
    return "CP-DEVELOPMENT"


def run_nmcli(*arguments: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["nmcli", *arguments],
        check=check,
        capture_output=True,
        env={**os.environ, "LANG": "C", "LC_ALL": "C"},
        text=True,
        timeout=timeout,
    )


def has_network_connection() -> bool:
    result = run_nmcli(
        "--terse",
        "--fields",
        "STATE",
        "general",
        timeout=10,
        check=False,
    )
    state = result.stdout.strip().lower()
    return result.returncode == 0 and state in {
        "connected",
        "connected (global)",
        "connected (site only)",
    }


def split_nmcli_terse(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def scan_wifi_networks(interface: str) -> list[dict]:
    arguments = (
        "--terse",
        "--escape",
        "yes",
        "--fields",
        "SSID,SIGNAL,SECURITY",
        "device",
        "wifi",
        "list",
        "ifname",
        interface,
    )
    result = run_nmcli(*arguments, "--rescan", "yes", check=False)
    if result.returncode:
        result = run_nmcli(*arguments, "--rescan", "no", check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "主机当前无法扫描 Wi-Fi")

    networks: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        fields = split_nmcli_terse(line)
        if len(fields) != 3 or not fields[0].strip():
            continue
        try:
            signal = max(0, min(100, int(fields[1])))
        except ValueError:
            continue
        ssid = fields[0].strip()
        candidate = {
            "ssid": ssid,
            "signal": signal,
            "secured": fields[2].strip() not in ("", "--"),
        }
        if ssid not in networks or signal > networks[ssid]["signal"]:
            networks[ssid] = candidate
    return sorted(networks.values(), key=lambda item: (-item["signal"], item["ssid"]))


def start_hotspot(interface: str, connection: str, ssid: str, password: str) -> None:
    run_nmcli("connection", "delete", connection, check=False)
    run_nmcli(
        "device",
        "wifi",
        "hotspot",
        "ifname",
        interface,
        "con-name",
        connection,
        "ssid",
        ssid,
        "password",
        password,
    )
    run_nmcli(
        "connection",
        "modify",
        connection,
        "ipv4.method",
        "shared",
        "ipv4.addresses",
        "192.168.4.1/24",
        "ipv6.method",
        "disabled",
        "connection.autoconnect",
        "yes",
    )
    run_nmcli("connection", "down", connection)
    run_nmcli("connection", "up", connection)


def connect_wifi(interface: str, hotspot_connection: str, ssid: str, password: str) -> None:
    run_nmcli("connection", "modify", hotspot_connection, "connection.autoconnect", "no")
    run_nmcli("connection", "down", hotspot_connection, check=False)
    result = run_nmcli(
        "--wait",
        "45",
        "device",
        "wifi",
        "connect",
        ssid,
        "password",
        password,
        "ifname",
        interface,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "无法连接家庭 Wi-Fi")


class ProvisioningServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, cloud_url: str, allow_http: bool, wifi_interface: str = "wlan0"):
        super().__init__(address, ProvisioningHandler)
        self.cloud_url = cloud_url.rstrip("/")
        self.allow_http = allow_http
        self.wifi_interface = wifi_interface
        self.completed = False
        self.result: queue.Queue[dict] = queue.Queue(maxsize=1)


class ProvisioningHandler(BaseHTTPRequestHandler):
    server: ProvisioningServer

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

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
        try:
            self._send_json(200, {"networks": scan_wifi_networks(self.server.wifi_interface)})
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self._send_json(503, {"detail": str(error) or "主机当前无法扫描 Wi-Fi"})

    def do_POST(self) -> None:
        if self.path != "/provision":
            self._send_json(404, {"detail": "接口不存在"})
            return
        if self.server.completed:
            self._send_json(409, {"detail": "主机已经完成配置"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65_536:
                raise ValueError("请求大小无效")
            payload = json.loads(self.rfile.read(length))
            cloud_url = str(payload["cloudUrl"]).rstrip("/")
            wifi_name = str(payload["wifiName"]).strip()
            wifi_password = str(payload["wifiPassword"])
            claim_token = str(payload["claimToken"])
            if cloud_url != self.server.cloud_url:
                raise ValueError("云端地址与主机配置不一致")
            if not self.server.allow_http and not cloud_url.startswith("https://"):
                raise ValueError("生产配网必须使用 HTTPS")
            if not wifi_name or len(wifi_password) < 8 or len(claim_token) < 32:
                raise ValueError("配网信息无效")
            self.server.completed = True
            self.server.result.put(
                {
                    "claimToken": claim_token,
                    "wifiName": wifi_name,
                    "wifiPassword": wifi_password,
                }
            )
            self._send_json(202, {"accepted": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._send_json(400, {"detail": str(error) or "配网数据无效"})

    def log_message(self, format: str, *args) -> None:
        return


def wait_for_setup(
    host: str, port: int, cloud_url: str, allow_http: bool, wifi_interface: str
) -> dict:
    server = ProvisioningServer((host, port), cloud_url, allow_http, wifi_interface)
    print(f"等待 App 配网：http://{host}:{port}/provision", flush=True)
    try:
        server.serve_forever()
        return server.result.get_nowait()
    finally:
        server.server_close()


def save_agent_config(path: Path, config: dict, owner: str = "") -> dict:
    provider = str(config["provider"]).strip().lower()
    api_key = str(config["apiKey"]).strip()
    model = str(config.get("model", "")).strip()
    if (
        provider not in PROVIDER_API_KEY_ENV
        or len(api_key) < 8
        or len(api_key) > 4096
        or len(model) > 200
    ):
        raise ValueError("Agent 配置信息无效")
    saved = {"provider": provider, "apiKey": api_key, "model": model}
    path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
        if owner and os.geteuid() == 0:
            user = pwd.getpwnam(owner)
            os.chown(path, user.pw_uid, user.pw_gid)
    return saved


def load_agent_config(path: Path) -> dict | None:
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding="utf-8"))
    provider = str(config.get("provider", "")).strip().lower()
    api_key = str(config.get("apiKey", "")).strip()
    if provider not in PROVIDER_API_KEY_ENV or len(api_key) < 8:
        raise RuntimeError("主机 Agent 配置无效，请重新配网")
    return {"provider": provider, "apiKey": api_key, "model": str(config.get("model", "")).strip()}


def apply_agent_config(config: dict) -> None:
    os.environ[PROVIDER_API_KEY_ENV[config["provider"]]] = config["apiKey"]


def claim_with_retry(server: str, token: str, serial: str, version: str) -> dict:
    deadline = time.monotonic() + 120
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return request_json(
                f"{server.rstrip('/')}/v1/provisioning/claim",
                {"claimToken": token, "serial": serial, "version": version},
            )
        except (OSError, RuntimeError) as error:
            last_error = error
            time.sleep(2)
    raise RuntimeError(f"主机认领失败：{last_error}")


def drop_privileges(user_name: str) -> None:
    if os.name == "nt" or os.geteuid() != 0 or not user_name:
        return
    user = pwd.getpwnam(user_name)
    os.initgroups(user_name, user.pw_gid)
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    os.environ.update(HOME=user.pw_dir, USER=user_name, LOGNAME=user_name)


def message_text(message: dict) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class PiRpcAgent:
    def __init__(
        self,
        command: str,
        sessions: Path,
        workspace: Path,
        provider: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.command = command
        self.sessions = sessions
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.thinking = thinking
        self.system_prompt = system_prompt

    @staticmethod
    def session_id(conversation_id: str) -> str:
        return f"clawpi-{hashlib.sha256(conversation_id.encode()).hexdigest()[:32]}"

    def arguments(self, conversation_id: str) -> list[str]:
        arguments = [
            self.command,
            "--mode",
            "rpc",
            "--session-id",
            self.session_id(conversation_id),
            "--session-dir",
            str(self.sessions),
            "--approve",
        ]
        for flag, value in (
            ("--provider", self.provider),
            ("--model", self.model),
            ("--thinking", self.thinking),
            ("--system-prompt", self.system_prompt),
        ):
            if value:
                arguments.extend((flag, value))
        return arguments

    async def stream(self, conversation_id: str, text: str):
        process = await asyncio.create_subprocess_exec(
            *self.arguments(conversation_id),
            cwd=self.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        assert process.stdin and process.stdout and process.stderr
        stderr_task = asyncio.create_task(process.stderr.read())
        emitted = False
        final_text = ""
        try:
            command = {"id": "clawpi-prompt", "type": "prompt", "message": text}
            process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()
            while line := await process.stdout.readline():
                event = json.loads(line)
                if event.get("type") == "response" and not event.get("success", False):
                    raise RuntimeError(str(event.get("error") or "Pi 拒绝了请求"))
                if event.get("type") == "message_update":
                    update = event.get("assistantMessageEvent", {})
                    if update.get("type") == "text_delta" and update.get("delta"):
                        emitted = True
                        yield str(update["delta"])
                elif event.get("type") == "message_end":
                    candidate = message_text(event.get("message", {}))
                    if candidate:
                        final_text = candidate
                elif event.get("type") == "agent_settled":
                    if not emitted and final_text:
                        yield final_text
                    if not emitted and not final_text:
                        raise RuntimeError("Pi agent 未返回文本")
                    return
            error = (await stderr_task).decode(errors="replace").strip()
            raise RuntimeError(error or "Pi agent 进程意外退出")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task


async def heartbeat(websocket) -> None:
    while True:
        await asyncio.sleep(20)
        await websocket.send(json.dumps({"type": "heartbeat"}))


async def handle_chat(websocket, message: dict, agent: PiRpcAgent, timeout: int) -> None:
    request_id = str(message.get("requestId", ""))
    try:
        if not request_id or not message.get("conversationId") or not message.get("text"):
            raise ValueError("聊天请求无效")
        async with asyncio.timeout(timeout):
            async for delta in agent.stream(str(message["conversationId"]), str(message["text"])):
                await websocket.send(
                    json.dumps(
                        {"type": "chat.delta", "requestId": request_id, "delta": delta},
                        ensure_ascii=False,
                    )
                )
        await websocket.send(
            json.dumps(
                {
                    "type": "chat.complete",
                    "requestId": request_id,
                    "messageId": f"pi-{uuid4()}",
                }
            )
        )
    except Exception as error:
        await websocket.send(
            json.dumps(
                {
                    "type": "chat.error",
                    "requestId": request_id,
                    "message": str(error)[:500] or "Pi agent 执行失败",
                },
                ensure_ascii=False,
            )
        )


async def handle_agent_configuration(
    websocket, message: dict, agent: PiRpcAgent, config_path: Path
) -> None:
    request_id = str(message.get("requestId", ""))
    try:
        if not request_id:
            raise ValueError("配置请求无效")
        provider = str(message.get("provider", "")).strip().lower()
        model = str(message.get("model", "")).strip()
        api_key = str(message.get("apiKey") or "").strip()
        current = load_agent_config(config_path)
        if not api_key:
            if not current or current["provider"] != provider:
                raise ValueError("更换服务商时必须提供 API Key")
            api_key = current["apiKey"]
        config = save_agent_config(
            config_path,
            {"provider": provider, "apiKey": api_key, "model": model},
        )
        apply_agent_config(config)
        agent.provider = config["provider"]
        agent.model = config["model"] or None
        await websocket.send(
            json.dumps(
                {
                    "type": "agent.configured",
                    "requestId": request_id,
                    "provider": config["provider"],
                    "model": config["model"],
                },
                ensure_ascii=False,
            )
        )
    except Exception as error:
        await websocket.send(
            json.dumps(
                {
                    "type": "agent.config.error",
                    "requestId": request_id,
                    "message": str(error)[:300] or "Agent 配置失败",
                },
                ensure_ascii=False,
            )
        )


async def run_host(
    server: str,
    credentials: dict,
    agent: PiRpcAgent,
    agent_config_path: Path,
    timeout: int,
) -> None:
    delay = 1
    while True:
        try:
            async with connect(
                websocket_url(server, credentials["deviceId"]),
                additional_headers={"Authorization": f"Bearer {credentials['hostToken']}"},
            ) as websocket:
                delay = 1
                heartbeat_task = asyncio.create_task(heartbeat(websocket))
                print(f"主机已连接：{credentials['deviceId']}", flush=True)
                try:
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        if message.get("type") == "chat.request":
                            await handle_chat(websocket, message, agent, timeout)
                        elif message.get("type") == "agent.configure":
                            await handle_agent_configuration(
                                websocket, message, agent, agent_config_path
                            )
                finally:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
        except Exception as error:
            print(f"连接断开：{error}；{delay} 秒后重试", flush=True)
            if not has_network_connection():
                print("网络不可用，重启服务并进入热点配网模式", flush=True)
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def parse_args() -> argparse.Namespace:
    state_dir = Path(os.getenv("CLAWPI_STATE_DIR", "/var/lib/clawpi"))
    parser = argparse.ArgumentParser(description="ClawPi AI 主机守护程序")
    parser.add_argument("--server", default=os.getenv("CLAWPI_SERVER_URL", "").rstrip("/"))
    parser.add_argument("--serial", default=machine_serial())
    parser.add_argument("--version", default=os.getenv("CLAWPI_VERSION", "ClawPi OS 0.1.0"))
    parser.add_argument("--credentials", type=Path, default=state_dir / "credentials.json")
    parser.add_argument("--agent-config", type=Path, default=state_dir / "agent.json")
    parser.add_argument("--sessions", type=Path, default=state_dir / "sessions")
    parser.add_argument("--workspace", type=Path, default=state_dir / "workspace")
    parser.add_argument("--setup-host", default="0.0.0.0")
    parser.add_argument("--setup-port", type=int, default=8090)
    parser.add_argument("--setup-password", default=os.getenv("CLAWPI_SETUP_PASSWORD", "clawpi-setup"))
    parser.add_argument("--wifi-interface", default=os.getenv("CLAWPI_WIFI_INTERFACE", "wlan0"))
    parser.add_argument("--hotspot-connection", default="clawpi-setup")
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--skip-hotspot", action="store_true")
    parser.add_argument("--run-as-user", default=os.getenv("CLAWPI_RUN_AS_USER", "clawpi"))
    parser.add_argument("--pi-command", default=os.getenv("CLAWPI_PI_COMMAND", "pi"))
    parser.add_argument("--pi-provider", default=os.getenv("CLAWPI_PI_PROVIDER") or None)
    parser.add_argument("--pi-model", default=os.getenv("CLAWPI_PI_MODEL") or None)
    parser.add_argument("--pi-thinking", default=os.getenv("CLAWPI_PI_THINKING") or None)
    parser.add_argument("--agent-timeout", type=int, default=75)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.credentials.parent.mkdir(parents=True, exist_ok=True)
    args.agent_config.parent.mkdir(parents=True, exist_ok=True)
    args.sessions.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    agent_config = load_agent_config(args.agent_config)

    credentials = None
    if args.credentials.exists():
        credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
        args.server = credentials.get("server", args.server)

    if credentials is None or not has_network_connection():
        if not args.server:
            raise SystemExit("首次启动必须配置 CLAWPI_SERVER_URL")
        if credentials is not None:
            print("未检测到可用网络，进入热点配网模式", flush=True)
        if not args.skip_hotspot:
            ssid = f"ClawPi-{args.serial[-6:]}"
            start_hotspot(args.wifi_interface, args.hotspot_connection, ssid, args.setup_password)
            print(f"配网热点已启动：{ssid}", flush=True)
        setup = wait_for_setup(
            args.setup_host,
            args.setup_port,
            args.server,
            args.allow_http,
            args.wifi_interface,
        )
        time.sleep(0.5)
        if not args.skip_hotspot:
            connect_wifi(
                args.wifi_interface,
                args.hotspot_connection,
                setup["wifiName"],
                setup["wifiPassword"],
            )
        claimed = claim_with_retry(
            args.server, setup["claimToken"], args.serial, args.version
        )
        credentials = save_credentials(args.credentials, claimed, args.server)
        print(f"自动绑定完成：{credentials['deviceId']}", flush=True)

    if agent_config:
        apply_agent_config(agent_config)
        args.pi_provider = agent_config["provider"]
        args.pi_model = agent_config["model"] or None

    drop_privileges(args.run_as_user)
    agent = PiRpcAgent(
        args.pi_command,
        args.sessions,
        args.workspace,
        args.pi_provider,
        args.pi_model,
        args.pi_thinking,
        os.getenv(
            "CLAWPI_SYSTEM_PROMPT",
            "你是运行在用户私人 AI 主机上的 Pi agent。直接、准确地帮助用户完成任务。",
        ),
    )
    asyncio.run(
        run_host(args.server, credentials, agent, args.agent_config, args.agent_timeout)
    )


if __name__ == "__main__":
    main()
