import argparse
import asyncio
import base64
import binascii
import contextlib
import hashlib
import io
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

from websockets.asyncio.client import connect

from simulator import request_json, save_credentials, websocket_url


PROVIDER_CATALOG = [
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
    ("ant-ling", "Ant Ling", "ANT_LING_API_KEY"),
    ("openai", "OpenAI", "OPENAI_API_KEY"),
    ("deepseek", "DeepSeek", "DEEPSEEK_API_KEY"),
    ("nvidia", "NVIDIA NIM", "NVIDIA_API_KEY"),
    ("google", "Google Gemini", "GEMINI_API_KEY"),
    ("amazon-bedrock", "Amazon Bedrock", "AWS_BEARER_TOKEN_BEDROCK"),
    ("mistral", "Mistral", "MISTRAL_API_KEY"),
    ("groq", "Groq", "GROQ_API_KEY"),
    ("cerebras", "Cerebras", "CEREBRAS_API_KEY"),
    ("xai", "xAI", "XAI_API_KEY"),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY"),
    ("vercel-ai-gateway", "Vercel AI Gateway", "AI_GATEWAY_API_KEY"),
    ("zai", "ZAI", "ZAI_API_KEY"),
    ("zai-coding-cn", "ZAI 中国", "ZAI_CODING_CN_API_KEY"),
    ("opencode", "OpenCode Zen", "OPENCODE_API_KEY"),
    ("opencode-go", "OpenCode Go", "OPENCODE_API_KEY"),
    ("radius", "Radius", "RADIUS_API_KEY"),
    ("huggingface", "Hugging Face", "HF_TOKEN"),
    ("fireworks", "Fireworks", "FIREWORKS_API_KEY"),
    ("together", "Together AI", "TOGETHER_API_KEY"),
    ("baseten", "Baseten", "BASETEN_API_KEY"),
    ("kimi-coding", "Kimi For Coding", "KIMI_API_KEY"),
    ("minimax", "MiniMax", "MINIMAX_API_KEY"),
    ("minimax-cn", "MiniMax 中国", "MINIMAX_CN_API_KEY"),
    ("qwen-token-plan", "Qwen Token Plan", "QWEN_TOKEN_PLAN_API_KEY"),
    ("qwen-token-plan-individual", "Qwen Individual", "QWEN_TOKEN_PLAN_API_KEY"),
    ("qwen-token-plan-cn", "Qwen 中国", "QWEN_TOKEN_PLAN_CN_API_KEY"),
    ("xiaomi", "Xiaomi MiMo", "XIAOMI_API_KEY"),
    ("xiaomi-token-plan-cn", "Xiaomi Token Plan 中国", "XIAOMI_TOKEN_PLAN_CN_API_KEY"),
    ("xiaomi-token-plan-ams", "Xiaomi Token Plan Amsterdam", "XIAOMI_TOKEN_PLAN_AMS_API_KEY"),
    ("xiaomi-token-plan-sgp", "Xiaomi Token Plan Singapore", "XIAOMI_TOKEN_PLAN_SGP_API_KEY"),
]

MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024
MAX_ATTACHMENTS_BYTES = 6 * 1024 * 1024
PROVIDER_API_KEY_ENV = {provider: env for provider, _, env in PROVIDER_CATALOG}


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
    result = subprocess.run(
        ["nmcli", *arguments],
        check=False,
        capture_output=True,
        env={**os.environ, "LANG": "C", "LC_ALL": "C"},
        text=True,
        timeout=timeout,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}"
        raise RuntimeError(f"nmcli 执行失败：{detail}")
    return result


def has_network_connection() -> bool:
    try:
        ipv4_routes = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        ipv4_routes = []
    if any(
        len(fields := line.split()) >= 8
        and fields[1] == "00000000"
        and fields[7] == "00000000"
        and int(fields[3], 16) & 1
        and fields[0] != "lo"
        for line in ipv4_routes
    ):
        return True

    try:
        ipv6_routes = Path("/proc/net/ipv6_route").read_text(encoding="ascii").splitlines()
    except OSError:
        ipv6_routes = []
    return any(
        len(fields := line.split()) >= 10
        and fields[0] == "0" * 32
        and fields[1] == "00"
        and int(fields[8], 16) & 1
        and fields[9] != "lo"
        for line in ipv6_routes
    )


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
    if not 8 <= len(password) <= 63:
        raise ValueError("CLAWPI_SETUP_PASSWORD 必须为 8 到 63 个字符")
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
        "no",
    )
    run_nmcli("connection", "down", connection)
    run_nmcli("connection", "up", connection)


def stop_hotspot(connection: str) -> None:
    run_nmcli("connection", "modify", connection, "connection.autoconnect", "no", check=False)
    run_nmcli("connection", "down", connection, check=False)


def refresh_hotspot_networks(
    interface: str, connection: str, ssid: str, password: str
) -> list[dict]:
    stop_hotspot(connection)
    time.sleep(1)
    try:
        return scan_wifi_networks(interface)
    finally:
        start_hotspot(interface, connection, ssid, password)


def connect_wifi(interface: str, hotspot_connection: str, ssid: str, password: str) -> None:
    run_nmcli("connection", "modify", hotspot_connection, "connection.autoconnect", "no")
    run_nmcli("connection", "down", hotspot_connection, check=False)
    result = None
    for attempt in range(3):
        run_nmcli(
            "device", "wifi", "rescan", "ifname", interface, "ssid", ssid,
            timeout=15, check=False,
        )
        time.sleep(2)
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
        if not result.returncode:
            return
        if attempt < 2:
            time.sleep(3)
    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "无法连接家庭 Wi-Fi")


class ProvisioningServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        address,
        cloud_url: str,
        allow_http: bool,
        wifi_interface: str = "wlan0",
        wifi_networks: list[dict] | None = None,
        refresh_wifi=None,
    ):
        super().__init__(address, ProvisioningHandler)
        self.cloud_url = cloud_url.rstrip("/")
        self.allow_http = allow_http
        self.wifi_interface = wifi_interface
        self.wifi_networks = wifi_networks
        self.refresh_wifi = refresh_wifi
        self.wifi_refreshing = False
        self.wifi_refresh_error = ""
        self.wifi_refresh_lock = threading.Lock()
        self.completed = False
        self.result: queue.Queue[dict] = queue.Queue(maxsize=1)

    def start_wifi_refresh(self) -> bool:
        with self.wifi_refresh_lock:
            if self.wifi_refreshing or not self.refresh_wifi:
                return False
            self.wifi_refreshing = True
            self.wifi_refresh_error = ""

        def refresh() -> None:
            time.sleep(0.5)
            try:
                self.wifi_networks = self.refresh_wifi()
            except Exception as error:
                self.wifi_refresh_error = str(error) or "刷新 Wi-Fi 失败"
            finally:
                self.wifi_refreshing = False

        threading.Thread(target=refresh, daemon=True).start()
        return True


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
            networks = self.server.wifi_networks
            if networks is None:
                networks = scan_wifi_networks(self.server.wifi_interface)
            self._send_json(
                200,
                {
                    "networks": networks,
                    "refreshing": self.server.wifi_refreshing,
                    "refreshError": self.server.wifi_refresh_error,
                },
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self._send_json(503, {"detail": str(error) or "主机当前无法扫描 Wi-Fi"})

    def do_POST(self) -> None:
        if self.path == "/wifi-networks/refresh":
            if not self.server.refresh_wifi:
                self._send_json(409, {"detail": "当前模式不支持刷新 Wi-Fi"})
                return
            accepted = self.server.start_wifi_refresh()
            self._send_json(202, {"accepted": accepted})
            return
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
    host: str,
    port: int,
    cloud_url: str,
    allow_http: bool,
    wifi_interface: str,
    monitor_network: bool = False,
    wifi_networks: list[dict] | None = None,
    refresh_wifi=None,
) -> dict | None:
    server = ProvisioningServer(
        (host, port), cloud_url, allow_http, wifi_interface, wifi_networks, refresh_wifi
    )
    network_lost = threading.Event()

    def stop_when_offline() -> None:
        while not server.completed:
            time.sleep(2)
            if not has_network_connection():
                network_lost.set()
                server.shutdown()
                return

    monitor = None
    if monitor_network:
        monitor = threading.Thread(target=stop_when_offline, daemon=True)
        monitor.start()
    print(f"等待 App 配网：http://{host}:{port}/provision", flush=True)
    try:
        server.serve_forever()
        if network_lost.is_set():
            return None
        return server.result.get_nowait()
    finally:
        server.completed = True
        server.server_close()
        if monitor:
            monitor.join(timeout=3)


def save_agent_config(path: Path, config: dict) -> dict:
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


class InteractionBroker:
    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[dict]] = {}

    def prepare(self, interaction_id: str) -> asyncio.Future[dict]:
        future = asyncio.get_running_loop().create_future()
        self.pending[interaction_id] = future
        return future

    def resolve(self, interaction_id: str, response: dict) -> bool:
        future = self.pending.get(interaction_id)
        if not future or future.done():
            return False
        future.set_result(response)
        return True

    def finish(self, interaction_id: str) -> None:
        self.pending.pop(interaction_id, None)

    def cancel(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.cancel()
        self.pending.clear()


def tool_status_label(tool_name: str) -> str:
    return {
        "bash": "正在执行命令",
        "read": "正在读取文件",
        "write": "正在写入文件",
        "edit": "正在修改文件",
        "search": "正在搜索",
        "web_search": "正在搜索网页",
        "fetch": "正在读取网页",
        "ask_user": "正在等待你的选择",
    }.get(tool_name, f"正在使用 {tool_name}")


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

    async def available_models(self, provider: str) -> list[dict]:
        process = await asyncio.create_subprocess_exec(
            self.command,
            "--mode",
            "rpc",
            "--no-session",
            "--provider",
            provider,
            "--approve",
            "--no-tools",
            cwd=self.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        assert process.stdin and process.stdout and process.stderr
        try:
            command = {"id": "clawpi-models", "type": "get_available_models"}
            process.stdin.write((json.dumps(command) + "\n").encode())
            await process.stdin.drain()
            while line := await process.stdout.readline():
                event = json.loads(line)
                if event.get("type") != "response" or event.get("command") != "get_available_models":
                    continue
                if not event.get("success", False):
                    raise RuntimeError(str(event.get("error") or "Pi 模型目录读取失败"))
                models = event.get("data", {}).get("models", [])
                return [
                    {
                        "id": str(model.get("id", "")),
                        "name": str(model.get("name") or model.get("id") or ""),
                        "reasoning": bool(model.get("reasoning")),
                        "contextWindow": int(model.get("contextWindow") or 0),
                    }
                    for model in models
                    if isinstance(model, dict)
                    and model.get("provider") == provider
                    and model.get("id")
                ]
            detail = (await process.stderr.read()).decode(errors="replace").strip()
            raise RuntimeError(detail or "Pi 模型目录读取失败")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def available_commands(self) -> list[dict]:
        process = await asyncio.create_subprocess_exec(
            self.command,
            "--mode",
            "rpc",
            "--no-session",
            "--approve",
            "--no-tools",
            cwd=self.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        assert process.stdin and process.stdout and process.stderr
        try:
            process.stdin.write(b'{"id":"clawpi-commands","type":"get_commands"}\n')
            await process.stdin.drain()
            while line := await process.stdout.readline():
                event = json.loads(line)
                if event.get("type") != "response" or event.get("command") != "get_commands":
                    continue
                if not event.get("success", False):
                    raise RuntimeError(str(event.get("error") or "Pi 命令目录读取失败"))
                commands = event.get("data", {}).get("commands", [])
                return [
                    {
                        "name": str(command.get("name", ""))[:120],
                        "description": str(command.get("description") or "")[:300],
                        "source": str(command.get("source") or "extension"),
                    }
                    for command in commands
                    if isinstance(command, dict) and command.get("name")
                ]
            detail = (await process.stderr.read()).decode(errors="replace").strip()
            raise RuntimeError(detail or "Pi 命令目录读取失败")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def stream(
        self,
        conversation_id: str,
        text: str,
        on_event=None,
        interactions: InteractionBroker | None = None,
    ):
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
        current_text = ""
        pending_text = ""
        consumed_text = ""
        progress_index = 0

        async def emit(event: dict) -> None:
            if on_event:
                await on_event(event)

        async def flush_progress() -> None:
            nonlocal current_text, pending_text, consumed_text, progress_index
            progress = (pending_text or current_text).strip()
            if progress:
                progress_index += 1
                await emit(
                    {
                        "type": "chat.progress",
                        "progressId": f"progress-{progress_index}",
                        "text": progress,
                    }
                )
                consumed_text = progress
            current_text = ""
            pending_text = ""

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
                        current_text += str(update["delta"])
                elif event.get("type") == "tool_execution_start":
                    await flush_progress()
                    await emit(
                        {
                            "type": "chat.status",
                            "statusId": str(event.get("toolCallId") or uuid4()),
                            "label": tool_status_label(str(event.get("toolName") or "工具")),
                            "state": "running",
                        }
                    )
                elif event.get("type") == "tool_execution_end":
                    await emit(
                        {
                            "type": "chat.status",
                            "statusId": str(event.get("toolCallId") or uuid4()),
                            "label": tool_status_label(str(event.get("toolName") or "工具")),
                            "state": "error" if event.get("isError") else "done",
                        }
                    )
                elif event.get("type") == "extension_ui_request":
                    method = str(event.get("method") or "")
                    if method in {"select", "confirm", "input", "editor"}:
                        await flush_progress()
                        interaction_id = str(event.get("id") or uuid4())
                        response = {"cancelled": True}
                        if interactions:
                            waiting = interactions.prepare(interaction_id)
                            await emit(
                                {
                                    "type": "chat.interaction",
                                    "interactionId": interaction_id,
                                    "method": method,
                                    "title": str(event.get("title") or "需要你的确认"),
                                    "message": str(event.get("message") or ""),
                                    "options": [str(item) for item in event.get("options", [])],
                                    "placeholder": str(event.get("placeholder") or ""),
                                }
                            )
                            try:
                                timeout_ms = int(event.get("timeout") or 0)
                                response = await (
                                    asyncio.wait_for(waiting, timeout_ms / 1000)
                                    if timeout_ms > 0
                                    else waiting
                                )
                            except TimeoutError:
                                response = {"cancelled": True}
                            finally:
                                interactions.finish(interaction_id)
                        process.stdin.write(
                            (
                                json.dumps(
                                    {
                                        "type": "extension_ui_response",
                                        "id": interaction_id,
                                        **response,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            ).encode()
                        )
                        await process.stdin.drain()
                    elif method == "notify" and event.get("message"):
                        await emit(
                            {
                                "type": "chat.status",
                                "statusId": str(event.get("id") or uuid4()),
                                "label": str(event["message"]),
                                "state": "error" if event.get("notifyType") == "error" else "done",
                            }
                        )
                elif event.get("type") == "compaction_start":
                    await emit(
                        {
                            "type": "chat.status",
                            "statusId": "compaction",
                            "label": "正在整理会话上下文",
                            "state": "running",
                        }
                    )
                elif event.get("type") == "compaction_end":
                    await emit(
                        {
                            "type": "chat.status",
                            "statusId": "compaction",
                            "label": "会话上下文已整理",
                            "state": "error" if event.get("errorMessage") else "done",
                        }
                    )
                elif event.get("type") == "auto_retry_start":
                    await emit(
                        {
                            "type": "chat.status",
                            "statusId": "retry",
                            "label": "模型服务繁忙，正在重试",
                            "state": "running",
                        }
                    )
                elif event.get("type") == "auto_retry_end":
                    await emit(
                        {
                            "type": "chat.status",
                            "statusId": "retry",
                            "label": "重试已完成" if event.get("success") else "重试失败",
                            "state": "done" if event.get("success") else "error",
                        }
                    )
                elif event.get("type") == "message_end":
                    message = event.get("message", {})
                    if message.get("role") != "assistant":
                        continue
                    if message.get("stopReason") == "error":
                        detail = str(message.get("errorMessage") or "Pi agent 执行失败")
                        if "API key not valid" in detail or "API_KEY_INVALID" in detail:
                            raise RuntimeError(
                                f"{(self.provider or '模型服务商').title()} API Key 无效，"
                                "请在主机设置中重新配置"
                            )
                        raise RuntimeError(detail)
                    candidate = message_text(message)
                    if candidate:
                        normalized = candidate.strip()
                        pending_text = "" if normalized == consumed_text else candidate
                        current_text = ""
                        consumed_text = ""
                elif event.get("type") == "agent_settled":
                    final_text = (pending_text or current_text).strip()
                    if final_text:
                        yield final_text
                    else:
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


def cpu_usage_percent(before: str, after: str) -> float:
    def totals(value: str) -> tuple[int, int]:
        fields = value.splitlines()[0].split()
        if not fields or fields[0] != "cpu" or len(fields) < 5:
            raise ValueError("无法读取 CPU 状态")
        values = [int(item) for item in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    total_before, idle_before = totals(before)
    total_after, idle_after = totals(after)
    elapsed = total_after - total_before
    if elapsed <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (elapsed - (idle_after - idle_before)) * 100 / elapsed)), 1)


def memory_usage(meminfo: str) -> tuple[int, int, float]:
    values = {}
    for line in meminfo.splitlines():
        key, separator, raw = line.partition(":")
        if separator:
            values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    if total <= 0:
        raise ValueError("无法读取内存状态")
    used = max(0, total - available)
    return used, total, round(used * 100 / total, 1)


def collect_system_status(sample_interval: float = 0.12) -> dict:
    before = Path("/proc/stat").read_text(encoding="utf-8")
    time.sleep(sample_interval)
    after = Path("/proc/stat").read_text(encoding="utf-8")
    memory_used, memory_total, memory_percent = memory_usage(
        Path("/proc/meminfo").read_text(encoding="utf-8")
    )
    disk = shutil.disk_usage("/")
    return {
        "cpuPercent": cpu_usage_percent(before, after),
        "memoryPercent": memory_percent,
        "memoryUsedBytes": memory_used,
        "memoryTotalBytes": memory_total,
        "diskPercent": round(disk.used * 100 / disk.total, 1) if disk.total else 0.0,
        "diskUsedBytes": disk.used,
        "diskTotalBytes": disk.total,
        "sampledAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def handle_system_status(websocket, message: dict) -> None:
    request_id = str(message.get("requestId", ""))
    try:
        if not request_id:
            raise ValueError("系统状态请求无效")
        status = await asyncio.to_thread(collect_system_status)
        await websocket.send(
            json.dumps(
                {"type": "system.status", "requestId": request_id, **status},
                ensure_ascii=False,
            )
        )
    except Exception as error:
        await websocket.send(
            json.dumps(
                {
                    "type": "system.status.error",
                    "requestId": request_id,
                    "message": str(error)[:300] or "读取系统状态失败",
                },
                ensure_ascii=False,
            )
        )


def save_chat_attachments(
    workspace: Path,
    conversation_id: str,
    attachments: object,
) -> list[dict]:
    if attachments is None:
        return []
    if not isinstance(attachments, list) or len(attachments) > 3:
        raise ValueError("附件数量无效")
    directory = workspace / ".clawpi" / "attachments" / PiRpcAgent.session_id(conversation_id)
    saved = []
    total_size = 0
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("附件格式无效")
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("附件内容无效")
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("单个附件不能超过 4 MB")
        total_size += len(data)
        if total_size > MAX_ATTACHMENTS_BYTES:
            raise ValueError("附件总大小不能超过 6 MB")
        original_name = str(item.get("name") or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original_name).strip(" .")[:120]
        if not safe_name:
            safe_name = "attachment"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid4().hex[:8]}-{safe_name}"
        path.write_bytes(data)
        if os.name != "nt":
            directory.chmod(0o700)
            path.chmod(0o600)
        saved.append(
            {
                "name": original_name[:200],
                "mimeType": str(item.get("mimeType") or "application/octet-stream")[:200],
                "size": len(data),
                "path": path,
            }
        )
    return saved


def chat_prompt(text: str, attachments: list[dict]) -> str:
    if not attachments:
        return text
    lines = ["用户上传了以下附件，文件已保存在本机。请根据任务需要读取："]
    for item in attachments:
        lines.append(
            f"- {json.dumps(str(item['path']), ensure_ascii=False)} "
            f"({item['mimeType']}, {item['size']} bytes)"
        )
    if text.strip():
        lines.extend(("", "用户消息：", text))
    else:
        lines.extend(("", "请查看并处理这些附件。"))
    return "\n".join(lines)


async def handle_chat(
    websocket,
    message: dict,
    agent: PiRpcAgent,
    timeout: int,
    interactions: InteractionBroker | None = None,
) -> None:
    request_id = str(message.get("requestId", ""))
    conversation_id = str(message.get("conversationId", ""))
    session_id = agent.session_id(conversation_id) if conversation_id else "unknown"
    started_at = time.monotonic()
    try:
        text = str(message.get("text") or "")
        if not request_id or not conversation_id:
            raise ValueError("聊天请求无效")
        attachments = save_chat_attachments(
            agent.workspace,
            conversation_id,
            message.get("attachments"),
        )
        if not text.strip() and not attachments:
            raise ValueError("聊天请求无效")
        print(
            f"收到聊天请求：request={request_id} session={session_id} "
            f"provider={agent.provider or 'default'} model={agent.model or 'default'}",
            flush=True,
        )

        async def forward_response() -> None:
            async def forward_event(event: dict) -> None:
                await websocket.send(
                    json.dumps({**event, "requestId": request_id}, ensure_ascii=False)
                )

            async for delta in agent.stream(
                conversation_id,
                chat_prompt(text, attachments),
                on_event=forward_event,
                interactions=interactions,
            ):
                await websocket.send(
                    json.dumps(
                        {"type": "chat.delta", "requestId": request_id, "delta": delta},
                        ensure_ascii=False,
                    )
                )

        await asyncio.wait_for(forward_response(), timeout)
        await websocket.send(
            json.dumps(
                {
                    "type": "chat.complete",
                    "requestId": request_id,
                    "messageId": f"pi-{uuid4()}",
                }
            )
        )
        print(
            f"聊天完成：request={request_id} session={session_id} "
            f"elapsed={time.monotonic() - started_at:.1f}s",
            flush=True,
        )
    except Exception as error:
        print(
            f"聊天失败：request={request_id or 'unknown'} session={session_id} "
            f"error={str(error)[:200] or type(error).__name__}",
            flush=True,
        )
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


async def handle_agent_config_query(
    websocket, message: dict, agent: PiRpcAgent, config_path: Path
) -> None:
    request_id = str(message.get("requestId", ""))
    try:
        if not request_id:
            raise ValueError("配置请求无效")
        config = load_agent_config(config_path)
        models = await agent.available_models(config["provider"]) if config else []
        await websocket.send(
            json.dumps(
                {
                    "type": "agent.config",
                    "requestId": request_id,
                    "configured": config is not None,
                    "provider": config["provider"] if config else "",
                    "model": config["model"] if config else "",
                    "providers": [
                        {"id": provider, "label": label}
                        for provider, label, _ in PROVIDER_CATALOG
                    ],
                    "models": models,
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
                    "message": str(error)[:300] or "读取 Agent 配置失败",
                },
                ensure_ascii=False,
            )
        )


async def handle_agent_commands(websocket, message: dict, agent: PiRpcAgent) -> None:
    request_id = str(message.get("requestId", ""))
    try:
        if not request_id:
            raise ValueError("命令目录请求无效")
        commands = await asyncio.wait_for(agent.available_commands(), timeout=25)
        await websocket.send(
            json.dumps(
                {
                    "type": "agent.commands",
                    "requestId": request_id,
                    "commands": commands,
                },
                ensure_ascii=False,
            )
        )
    except Exception as error:
        await websocket.send(
            json.dumps(
                {
                    "type": "agent.commands.error",
                    "requestId": request_id,
                    "message": str(error)[:300] or "读取命令目录失败",
                },
                ensure_ascii=False,
            )
        )


def load_capability_state(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict) and item.get("id")] if isinstance(value, list) else []


def save_capability_state(path: Path, capabilities: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(capabilities, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


def discover_capabilities(
    capability_state: Path,
    skills_dir: Path,
    extensions_dir: Path,
) -> list[dict]:
    installed = [{**item, "managed": True, "local": False} for item in load_capability_state(capability_state)]
    managed_ids = {str(item.get("id", "")) for item in installed}

    if skills_dir.exists():
        for entry in sorted(skills_dir.iterdir(), key=lambda item: item.name.lower()):
            if entry.name.startswith(".") or not entry.is_dir() or not (entry / "SKILL.md").is_file():
                continue
            if entry.name in managed_ids:
                continue
            installed.append(
                {
                    "id": f"local-skill:{entry.name}",
                    "name": entry.name,
                    "kind": "skill",
                    "version": "",
                    "source": "",
                    "managed": False,
                    "local": True,
                }
            )

    extension_names = set()
    if extensions_dir.exists():
        for entry in sorted(extensions_dir.iterdir(), key=lambda item: item.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() in {".js", ".cjs", ".mjs", ".ts"}:
                extension_names.add(entry.stem)
            elif entry.is_dir():
                extension_names.add(entry.name)
    for name in sorted(extension_names, key=str.lower):
        if name in managed_ids:
            continue
        installed.append(
            {
                "id": f"local-extension:{name}",
                "name": name,
                "kind": "extension",
                "version": "",
                "source": "",
                "managed": False,
                "local": True,
            }
        )
    return installed


def download_skill(server: str, artifact_path: str, expected_sha256: str) -> bytes:
    url = urljoin(f"{server.rstrip('/')}/", artifact_path.lstrip("/"))
    with urllib.request.urlopen(url, timeout=45) as response:
        data = response.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("Skill 安装包超过 10 MB")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("Skill 安装包校验失败")
    return data


def install_skill(capability: dict, server: str, skills_dir: Path) -> None:
    capability_id = str(capability.get("id", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}", capability_id):
        raise ValueError("能力 ID 无效")
    artifact_path = str(capability.get("artifactPath", ""))
    expected_sha256 = str(capability.get("artifactSha256", ""))
    if not artifact_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Skill 安装信息不完整")
    data = download_skill(server, artifact_path, expected_sha256)
    skills_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=skills_dir) as temporary:
        unpacked = Path(temporary) / "unpacked"
        unpacked.mkdir()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) > 300 or sum(item.file_size for item in files) > 30 * 1024 * 1024:
                raise ValueError("Skill 安装包内容过大")
            for item in files:
                relative = Path(item.filename)
                mode = item.external_attr >> 16
                if relative.is_absolute() or ".." in relative.parts or stat.S_ISLNK(mode):
                    raise ValueError("Skill 安装包包含不安全路径")
                target = unpacked / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        manifests = list(unpacked.rglob("SKILL.md"))
        if len(manifests) != 1:
            raise ValueError("Skill 安装包必须包含一个 SKILL.md")
        staged = skills_dir / f".{capability_id}-{uuid4().hex}.new"
        shutil.copytree(manifests[0].parent, staged)
        destination = skills_dir / capability_id
        backup = skills_dir / f".{capability_id}.old"
        shutil.rmtree(backup, ignore_errors=True)
        if destination.exists():
            destination.replace(backup)
        try:
            staged.replace(destination)
        except Exception:
            if backup.exists():
                backup.replace(destination)
            raise
        shutil.rmtree(backup, ignore_errors=True)


def run_pi_package(pi_command: str, action: str, source: str) -> None:
    if not source.startswith(("npm:", "git:", "https://", "http://", "ssh://")):
        raise ValueError("能力来源只允许 npm 或 Git 地址")
    result = subprocess.run(
        [pi_command, action, source],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Pi 插件操作失败"
        raise RuntimeError(detail[-1000:])


def install_capability(
    capability: dict,
    server: str,
    pi_command: str,
    capability_state: Path,
    skills_dir: Path,
) -> list[dict]:
    kind = str(capability.get("kind", ""))
    source = str(capability.get("source", ""))
    if kind not in {"skill", "extension", "mcp"}:
        raise ValueError("能力类型无效")
    if kind == "skill" and capability.get("artifactPath"):
        install_skill(capability, server, skills_dir)
    elif source:
        run_pi_package(pi_command, "install", source)
    else:
        raise ValueError("能力缺少安装来源")
    installed = [item for item in load_capability_state(capability_state) if item["id"] != capability["id"]]
    installed.append(
        {
            "id": capability["id"],
            "name": capability.get("name") or capability["id"],
            "kind": kind,
            "version": capability.get("version", ""),
            "source": source,
            "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    save_capability_state(capability_state, installed)
    return installed


def remove_capability(
    capability_id: str,
    pi_command: str,
    capability_state: Path,
    skills_dir: Path,
) -> list[dict]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}", capability_id):
        raise ValueError("能力 ID 无效")
    installed = load_capability_state(capability_state)
    current = next((item for item in installed if item["id"] == capability_id), None)
    if not current:
        return installed
    if current.get("source"):
        run_pi_package(pi_command, "remove", str(current["source"]))
    else:
        shutil.rmtree(skills_dir / capability_id, ignore_errors=True)
    remaining = [item for item in installed if item["id"] != capability_id]
    save_capability_state(capability_state, remaining)
    return remaining


async def handle_capability(
    websocket,
    message: dict,
    server: str,
    pi_command: str,
    capability_state: Path,
    skills_dir: Path,
    extensions_dir: Path,
) -> None:
    request_id = str(message.get("requestId", ""))
    try:
        if not request_id:
            raise ValueError("能力请求无效")
        action = str(message.get("type", "")).removeprefix("capability.")
        if action == "list":
            installed = discover_capabilities(capability_state, skills_dir, extensions_dir)
        elif action == "install":
            capability = message.get("capability")
            if not isinstance(capability, dict):
                raise ValueError("能力安装信息无效")
            installed = await asyncio.to_thread(
                install_capability,
                capability,
                server,
                pi_command,
                capability_state,
                skills_dir,
            )
        elif action == "remove":
            installed = await asyncio.to_thread(
                remove_capability,
                str(message.get("capabilityId", "")),
                pi_command,
                capability_state,
                skills_dir,
            )
        else:
            raise ValueError("不支持的能力操作")
        await websocket.send(
            json.dumps(
                {"type": "capability.result", "requestId": request_id, "data": {"installed": installed}},
                ensure_ascii=False,
            )
        )
    except Exception as error:
        await websocket.send(
            json.dumps(
                {
                    "type": "capability.error",
                    "requestId": request_id,
                    "message": str(error)[:1000] or "能力操作失败",
                },
                ensure_ascii=False,
            )
        )


async def run_host(
    server: str,
    credentials: dict,
    agent: PiRpcAgent,
    agent_config_path: Path,
    capability_state: Path,
    skills_dir: Path,
    extensions_dir: Path,
    timeout: int,
) -> None:
    delay = 1
    while True:
        try:
            async with connect(
                websocket_url(server, credentials["deviceId"]),
                additional_headers={"Authorization": f"Bearer {credentials['hostToken']}"},
                max_size=12 * 1024 * 1024,
            ) as websocket:
                delay = 1
                heartbeat_task = asyncio.create_task(heartbeat(websocket))
                active_chats: dict[str, tuple[asyncio.Task, InteractionBroker]] = {}
                print(f"主机已连接：{credentials['deviceId']}", flush=True)
                try:
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        if message.get("type") == "chat.request":
                            request_id = str(message.get("requestId") or "")
                            if active_chats:
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "chat.error",
                                            "requestId": request_id,
                                            "message": "主机正在处理另一条消息",
                                        },
                                        ensure_ascii=False,
                                    )
                                )
                                continue
                            broker = InteractionBroker()

                            async def run_chat(
                                chat_message=message,
                                chat_request_id=request_id,
                                chat_broker=broker,
                            ) -> None:
                                try:
                                    await handle_chat(
                                        websocket,
                                        chat_message,
                                        agent,
                                        timeout,
                                        chat_broker,
                                    )
                                finally:
                                    chat_broker.cancel()
                                    active_chats.pop(chat_request_id, None)

                            task = asyncio.create_task(run_chat())
                            active_chats[request_id] = (task, broker)
                        elif message.get("type") == "chat.interaction.response":
                            active = active_chats.get(str(message.get("requestId") or ""))
                            if active:
                                active[1].resolve(
                                    str(message.get("interactionId") or ""),
                                    message.get("response")
                                    if isinstance(message.get("response"), dict)
                                    else {"cancelled": True},
                                )
                        elif message.get("type") == "chat.cancel":
                            active = active_chats.get(str(message.get("requestId") or ""))
                            if active:
                                active[1].cancel()
                                active[0].cancel()
                        elif message.get("type") == "agent.configure":
                            await handle_agent_configuration(
                                websocket, message, agent, agent_config_path
                            )
                        elif message.get("type") == "agent.config.get":
                            await handle_agent_config_query(
                                websocket, message, agent, agent_config_path
                            )
                        elif message.get("type") == "agent.commands.get":
                            await handle_agent_commands(websocket, message, agent)
                        elif message.get("type") == "system.status.get":
                            await handle_system_status(websocket, message)
                        elif str(message.get("type", "")).startswith("capability."):
                            await handle_capability(
                                websocket,
                                message,
                                server,
                                agent.command,
                                capability_state,
                                skills_dir,
                                extensions_dir,
                            )
                finally:
                    chats_to_cancel = list(active_chats.values())
                    for task, broker in chats_to_cancel:
                        broker.cancel()
                        task.cancel()
                    if chats_to_cancel:
                        await asyncio.gather(
                            *(task for task, _ in chats_to_cancel),
                            return_exceptions=True,
                        )
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
    parser.add_argument("--capability-state", type=Path, default=state_dir / "capabilities.json")
    parser.add_argument("--sessions", type=Path, default=state_dir / "sessions")
    parser.add_argument("--workspace", type=Path, default=state_dir / "workspace")
    parser.add_argument("--setup-host", default="0.0.0.0")
    parser.add_argument("--setup-port", type=int, default=8090)
    parser.add_argument("--setup-password", default=os.getenv("CLAWPI_SETUP_PASSWORD", "clawpi-setup"))
    parser.add_argument("--wifi-interface", default=os.getenv("CLAWPI_WIFI_INTERFACE", "wlan0"))
    parser.add_argument("--hotspot-connection", default="clawpi-setup")
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--skip-hotspot", action="store_true")
    parser.add_argument("--pi-command", default=os.getenv("CLAWPI_PI_COMMAND", "pi"))
    parser.add_argument("--pi-provider", default=os.getenv("CLAWPI_PI_PROVIDER") or None)
    parser.add_argument("--pi-model", default=os.getenv("CLAWPI_PI_MODEL") or None)
    parser.add_argument("--pi-thinking", default=os.getenv("CLAWPI_PI_THINKING") or None)
    parser.add_argument("--agent-timeout", type=int, default=600)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.credentials.parent.mkdir(parents=True, exist_ok=True)
    args.agent_config.parent.mkdir(parents=True, exist_ok=True)
    args.capability_state.parent.mkdir(parents=True, exist_ok=True)
    args.sessions.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    agent_config = load_agent_config(args.agent_config)

    credentials = None
    if args.credentials.exists():
        credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
        args.server = credentials.get("server", args.server)

    network_connected = has_network_connection()
    stop_hotspot(args.hotspot_connection)
    if credentials is None or not network_connected:
        if not args.server:
            raise SystemExit("首次启动必须配置 CLAWPI_SERVER_URL")
        if not network_connected:
            print("未检测到可用网络，进入热点配网模式", flush=True)
        else:
            print("主机已联网，等待 App 通过当前局域网完成绑定", flush=True)
        wifi_networks = None
        refresh_wifi = None
        if not network_connected and not args.skip_hotspot:
            try:
                wifi_networks = scan_wifi_networks(args.wifi_interface)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                wifi_networks = []
                print(f"热点启动前扫描 Wi-Fi 失败：{error}", flush=True)
            ssid = f"ClawPi-{args.serial[-6:]}"
            start_hotspot(args.wifi_interface, args.hotspot_connection, ssid, args.setup_password)
            refresh_wifi = lambda: refresh_hotspot_networks(
                args.wifi_interface,
                args.hotspot_connection,
                ssid,
                args.setup_password,
            )
            print(f"配网热点已启动：{ssid}", flush=True)
        setup = wait_for_setup(
            args.setup_host,
            args.setup_port,
            args.server,
            args.allow_http,
            args.wifi_interface,
            monitor_network=network_connected,
            wifi_networks=wifi_networks,
            refresh_wifi=refresh_wifi,
        )
        if setup is None:
            print("等待绑定期间网络断开，重启后进入热点配网模式", flush=True)
            return
        time.sleep(0.5)
        if not network_connected and not args.skip_hotspot:
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
        run_host(
            args.server,
            credentials,
            agent,
            args.agent_config,
            args.capability_state,
            Path(os.getenv("PI_CODING_AGENT_DIR", "/var/lib/clawpi/pi-config")) / "skills",
            Path(os.getenv("PI_CODING_AGENT_DIR", "/var/lib/clawpi/pi-config")) / "extensions",
            args.agent_timeout,
        )
    )


if __name__ == "__main__":
    main()
