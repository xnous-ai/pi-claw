import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import AsyncMock, patch

from daemon import (
    InteractionBroker,
    PiRpcAgent,
    ProvisioningServer,
    apply_agent_config,
    connect_wifi,
    has_network_connection,
    handle_agent_configuration,
    handle_agent_commands,
    handle_agent_config_query,
    handle_chat,
    install_capability,
    install_skill,
    load_capability_state,
    load_agent_config,
    message_text,
    refresh_hotspot_networks,
    run_nmcli,
    run_host,
    save_agent_config,
    scan_wifi_networks,
    start_hotspot,
    stop_hotspot,
    wait_for_setup,
)


class FakePiAgent(PiRpcAgent):
    def __init__(self, script: Path, state: Path):
        super().__init__(sys.executable, state / "sessions", state / "workspace")
        self.script = script
        self.sessions.mkdir()
        self.workspace.mkdir()

    def arguments(self, conversation_id: str) -> list[str]:
        return [sys.executable, "-u", str(self.script)]


class PiRpcAgentTest(unittest.TestCase):
    def test_streams_text_and_keeps_conversations_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_pi.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import sys

                    sys.stdin.reconfigure(encoding="utf-8")
                    sys.stdout.reconfigure(encoding="utf-8")
                    command = json.loads(sys.stdin.readline())
                    print(json.dumps({"type": "response", "command": "prompt", "success": True}), flush=True)
                    for text in ("received: ", command["message"]):
                        print(json.dumps({
                            "type": "message_update",
                            "assistantMessageEvent": {"type": "text_delta", "delta": text}
                        }, ensure_ascii=False), flush=True)
                    print(json.dumps({"type": "agent_settled"}), flush=True)
                    sys.stdin.read()
                    """
                ),
                encoding="utf-8",
            )
            agent = FakePiAgent(script, root)

            async def collect() -> str:
                return "".join([part async for part in agent.stream("conversation-1", "hello")])

            self.assertEqual(asyncio.run(collect()), "received: hello")
            self.assertEqual(agent.session_id("same"), agent.session_id("same"))
            self.assertNotEqual(agent.session_id("one"), agent.session_id("two"))

    def test_extracts_final_assistant_text(self) -> None:
        self.assertEqual(
            message_text(
                {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "完成"}]}
            ),
            "完成",
        )

    def test_does_not_echo_user_message_when_provider_rejects_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_pi.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import sys

                    command = json.loads(sys.stdin.readline())
                    print(json.dumps({
                        "type": "message_end",
                        "message": {"role": "user", "content": command["message"]}
                    }), flush=True)
                    print(json.dumps({
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": "API key not valid (API_KEY_INVALID)"
                        }
                    }), flush=True)
                    print(json.dumps({"type": "agent_settled"}), flush=True)
                    sys.stdin.read()
                    """
                ),
                encoding="utf-8",
            )
            agent = FakePiAgent(script, root)
            agent.provider = "google"

            async def collect() -> str:
                return "".join([part async for part in agent.stream("conversation-1", "你好")])

            with self.assertRaisesRegex(RuntimeError, "Google API Key 无效"):
                asyncio.run(collect())

    def test_logs_chat_lifecycle_without_message_text(self) -> None:
        class FakeWebsocket:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            async def send(self, value: str) -> None:
                self.messages.append(json.loads(value))

        class ReplyAgent(PiRpcAgent):
            async def stream(self, conversation_id: str, text: str, **_kwargs):
                yield "reply"

        websocket = FakeWebsocket()
        agent = ReplyAgent("pi", Path("sessions"), Path("workspace"), "openai", "gpt-test")
        with patch("builtins.print") as output:
            asyncio.run(
                handle_chat(
                    websocket,
                    {
                        "requestId": "request-1",
                        "conversationId": "conversation-1",
                        "text": "private message",
                    },
                    agent,
                    5,
                )
            )

        logs = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("收到聊天请求", logs)
        self.assertIn("聊天完成", logs)
        self.assertNotIn("private message", logs)
        self.assertEqual(websocket.messages[-1]["type"], "chat.complete")

    def test_streams_tool_status_and_resumes_after_user_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake_interactive_pi.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import sys

                    sys.stdin.reconfigure(encoding="utf-8")
                    sys.stdout.reconfigure(encoding="utf-8")
                    json.loads(sys.stdin.readline())
                    print(json.dumps({
                        "type": "tool_execution_start",
                        "toolCallId": "tool-1",
                        "toolName": "read"
                    }), flush=True)
                    print(json.dumps({
                        "type": "tool_execution_end",
                        "toolCallId": "tool-1",
                        "toolName": "read",
                        "isError": False
                    }), flush=True)
                    print(json.dumps({
                        "type": "extension_ui_request",
                        "id": "choice-1",
                        "method": "select",
                        "title": "选择方案",
                        "options": ["方案 A", "方案 B"]
                    }, ensure_ascii=False), flush=True)
                    response = json.loads(sys.stdin.readline())
                    assert response["type"] == "extension_ui_response"
                    assert response["value"] == "方案 B"
                    print(json.dumps({
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "继续完成"}
                    }, ensure_ascii=False), flush=True)
                    print(json.dumps({"type": "agent_settled"}), flush=True)
                    sys.stdin.read()
                    """
                ),
                encoding="utf-8",
            )
            agent = FakePiAgent(script, root)
            broker = InteractionBroker()
            events: list[dict] = []

            async def collect() -> str:
                async def on_event(event: dict) -> None:
                    events.append(event)
                    if event["type"] == "chat.interaction":
                        broker.resolve(event["interactionId"], {"value": "方案 B"})

                return "".join(
                    [
                        part
                        async for part in agent.stream(
                            "conversation-1",
                            "开始",
                            on_event=on_event,
                            interactions=broker,
                        )
                    ]
                )

            self.assertEqual(asyncio.run(collect()), "继续完成")
            self.assertEqual(
                [event["type"] for event in events],
                ["chat.status", "chat.status", "chat.interaction"],
            )
            self.assertEqual(events[0]["state"], "running")
            self.assertEqual(events[1]["state"], "done")

    def test_installs_builtin_interaction_extension(self) -> None:
        install_script = Path("install.sh").read_text(encoding="utf-8")
        extension = Path("clawpi-interaction.ts").read_text(encoding="utf-8")
        self.assertIn("clawpi-interaction.ts", install_script)
        self.assertIn('name: "ask_user"', extension)

    def test_lists_extension_and_skill_commands(self) -> None:
        class CommandAgent:
            async def available_commands(self) -> list[dict]:
                return [
                    {"name": "weather", "description": "查询天气", "source": "extension"},
                    {"name": "skill:writer", "description": "写作", "source": "skill"},
                ]

        class FakeWebsocket:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            async def send(self, value: str) -> None:
                self.messages.append(json.loads(value))

        websocket = FakeWebsocket()
        asyncio.run(
            handle_agent_commands(websocket, {"requestId": "commands-1"}, CommandAgent())
        )
        self.assertEqual(websocket.messages[0]["type"], "agent.commands")
        self.assertEqual(websocket.messages[0]["commands"][0]["name"], "weather")


class ProvisioningTest(unittest.TestCase):
    @staticmethod
    def get(base_url: str, path: str) -> dict:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=2) as response:
            return json.load(response)

    @staticmethod
    def post(base_url: str, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def test_completes_setup_after_receiving_network(self) -> None:
        server = ProvisioningServer(
            ("127.0.0.1", 0),
            "http://cloud.local",
            True,
            wifi_networks=[{"ssid": "Home WiFi", "signal": 84, "secured": True}],
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            scanned = self.get(base_url, "/wifi-networks")
            self.assertEqual(scanned["networks"][0]["ssid"], "Home WiFi")
            network = self.post(
                base_url,
                "/provision",
                {
                    "cloudUrl": "http://cloud.local",
                    "claimToken": "t" * 32,
                    "wifiName": "Home WiFi",
                    "wifiPassword": "password123",
                },
            )
            self.assertTrue(network["accepted"])
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            setup = server.result.get_nowait()
            self.assertEqual(setup["wifiName"], "Home WiFi")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_parses_and_deduplicates_nmcli_scan(self) -> None:
        result = CompletedProcess(
            [],
            0,
            "Home\\:Office:72:WPA2\nGuest:40:--\nHome\\:Office:91:WPA3\n:99:WPA2\n",
            "",
        )
        with patch("daemon.run_nmcli", return_value=result):
            networks = scan_wifi_networks("wlan0")
        self.assertEqual(
            networks,
            [
                {"ssid": "Home:Office", "signal": 91, "secured": True},
                {"ssid": "Guest", "signal": 40, "secured": False},
            ],
        )

    def test_detects_ipv4_or_ipv6_default_route(self) -> None:
        header = "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        ipv4_route = header + "wlp1s0 00000000 0132A8C0 0003 0 0 600 00000000 0 0 0\n"
        ipv6_route = f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000400 0 0 00000003 wlp1s0\n"
        unreachable_ipv6 = (
            f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000000 1 0 00200200 lo\n"
        )
        for routes, expected in (
            ([ipv4_route], True),
            ([header, ipv6_route], True),
            ([header, unreachable_ipv6], False),
            ([header, ""], False),
        ):
            with self.subTest(expected=expected), patch(
                "daemon.Path.read_text", side_effect=routes
            ):
                self.assertEqual(has_network_connection(), expected)

    def test_stops_existing_hotspot_without_failing(self) -> None:
        with patch("daemon.run_nmcli") as nmcli:
            stop_hotspot("clawpi-setup")
        self.assertEqual(nmcli.call_count, 2)
        self.assertTrue(all(call.kwargs["check"] is False for call in nmcli.call_args_list))

    def test_restores_hotspot_when_refresh_scan_fails(self) -> None:
        with patch("daemon.stop_hotspot") as stop, patch(
            "daemon.scan_wifi_networks", side_effect=RuntimeError("scan failed")
        ), patch("daemon.start_hotspot") as start, patch("daemon.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "scan failed"):
                refresh_hotspot_networks("wlp1s0", "clawpi-setup", "ClawPi-Test", "12345678")
        stop.assert_called_once_with("clawpi-setup")
        start.assert_called_once_with("wlp1s0", "clawpi-setup", "ClawPi-Test", "12345678")

    def test_rescans_and_retries_wifi_after_stopping_hotspot(self) -> None:
        ok = CompletedProcess([], 0, "", "")
        missing = CompletedProcess([], 10, "", "Error: No network with SSID 'Home' found.")
        with patch(
            "daemon.run_nmcli", side_effect=[ok, ok, ok, missing, ok, ok]
        ) as nmcli, patch("daemon.time.sleep"):
            connect_wifi("wlp1s0", "clawpi-setup", "Home", "secret123")

        rescans = [call for call in nmcli.call_args_list if "rescan" in call.args]
        connects = [call for call in nmcli.call_args_list if "connect" in call.args]
        self.assertEqual(len(rescans), 2)
        self.assertEqual(len(connects), 2)

    def test_install_allows_service_to_save_credentials(self) -> None:
        script = Path(__file__).with_name("install.sh").read_text(encoding="utf-8")
        self.assertIn("install -d -o root -g clawpi -m 0770 /var/lib/clawpi", script)
        self.assertIn("chown root:clawpi /var/lib/clawpi", script)
        self.assertIn("chmod 0660 /var/lib/clawpi/agent.json", script)

    def test_allows_only_one_wifi_refresh_at_a_time(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def refresh() -> list[dict]:
            started.set()
            release.wait(2)
            return []

        server = ProvisioningServer(
            ("127.0.0.1", 0), "https://cloud.local", False, refresh_wifi=refresh
        )
        try:
            self.assertTrue(server.start_wifi_refresh())
            self.assertTrue(started.wait(2))
            self.assertFalse(server.start_wifi_refresh())
            release.set()
        finally:
            release.set()
            server.server_close()

    def test_stops_waiting_for_setup_when_network_is_lost(self) -> None:
        with patch("daemon.has_network_connection", return_value=False):
            setup = wait_for_setup(
                "127.0.0.1", 0, "https://cloud.local", False, "wlp1s0", monitor_network=True
            )
        self.assertIsNone(setup)

    def test_rejects_invalid_hotspot_password(self) -> None:
        with self.assertRaisesRegex(ValueError, "8 到 63"):
            start_hotspot("wlan0", "clawpi-setup", "ClawPi-Test", "123456")

    def test_nmcli_error_includes_stderr(self) -> None:
        failed = CompletedProcess([], 1, "", "Device 'wlan0' is not available")
        with patch("daemon.subprocess.run", return_value=failed), self.assertRaisesRegex(
            RuntimeError, "wlan0.*not available"
        ):
            run_nmcli("device", "wifi", "hotspot")

    def test_host_returns_when_network_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = PiRpcAgent("pi", root / "sessions", root / "workspace")
            with patch("daemon.connect", side_effect=OSError("offline")), patch(
                "daemon.has_network_connection", return_value=False
            ):
                asyncio.run(
                    run_host(
                        "https://cloud.local",
                        {"deviceId": "device-1", "hostToken": "token"},
                        agent,
                        root / "agent.json",
                        root / "capabilities.json",
                        root / "skills",
                        1,
                    )
                )

    def test_installs_reviewed_skill_archive(self) -> None:
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("web-search/SKILL.md", "# Web search")
            archive.writestr("web-search/reference.txt", "reference")
        data = archive_buffer.getvalue()
        capability = {
            "id": "web-search",
            "name": "网页搜索",
            "kind": "skill",
            "version": "1.0.0",
            "artifactPath": "/v1/capabilities/web-search/artifact",
            "artifactSha256": hashlib.sha256(data).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "daemon.download_skill", return_value=data
        ):
            root = Path(directory)
            state = root / "capabilities.json"
            installed = install_capability(
                capability,
                "https://cloud.local",
                "pi",
                state,
                root / "skills",
            )
            self.assertEqual(installed[0]["id"], "web-search")
            self.assertTrue((root / "skills" / "web-search" / "SKILL.md").is_file())
            self.assertEqual(load_capability_state(state)[0]["version"], "1.0.0")

    def test_rejects_unsafe_skill_archive(self) -> None:
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("SKILL.md", "# Unsafe")
            archive.writestr("../outside.txt", "no")
        data = archive_buffer.getvalue()
        capability = {
            "id": "unsafe-skill",
            "artifactPath": "/unsafe.zip",
            "artifactSha256": hashlib.sha256(data).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "daemon.download_skill", return_value=data
        ), self.assertRaisesRegex(ValueError, "不安全路径"):
            install_skill(capability, "https://cloud.local", Path(directory) / "skills")

    def test_saves_and_applies_agent_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.json"
            saved = save_agent_config(
                path,
                {"provider": "anthropic", "apiKey": "sk-ant-test", "model": "claude-test"},
            )
            self.assertEqual(load_agent_config(path), saved)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o660)
            with patch.dict(os.environ, {}, clear=False):
                apply_agent_config(saved)
                self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-test")
            deepseek = save_agent_config(
                path,
                {"provider": "deepseek", "apiKey": "sk-deepseek-test", "model": ""},
            )
            with patch.dict(os.environ, {}, clear=False):
                apply_agent_config(deepseek)
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-deepseek-test")

    def test_applies_relayed_agent_configuration(self) -> None:
        class FakeWebsocket:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            async def send(self, value: str) -> None:
                self.messages.append(json.loads(value))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            websocket = FakeWebsocket()
            agent = PiRpcAgent("pi", root / "sessions", root / "workspace")
            with patch.dict(os.environ, {}, clear=False):
                asyncio.run(
                    handle_agent_configuration(
                        websocket,
                        {
                            "requestId": "config-1",
                            "provider": "openai",
                            "model": "gpt-test",
                            "apiKey": "sk-test-key",
                        },
                        agent,
                        root / "agent.json",
                    )
                )
                self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-test-key")
                asyncio.run(
                    handle_agent_configuration(
                        websocket,
                        {
                            "requestId": "config-2",
                            "provider": "openai",
                            "model": "gpt-next",
                        },
                        agent,
                        root / "agent.json",
                    )
                )
                self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-test-key")
            self.assertEqual(websocket.messages[0]["type"], "agent.configured")
            self.assertEqual(agent.provider, "openai")
            self.assertEqual(agent.model, "gpt-next")

    def test_returns_agent_config_without_api_key(self) -> None:
        class FakeWebsocket:
            def __init__(self) -> None:
                self.messages: list[dict] = []

            async def send(self, value: str) -> None:
                self.messages.append(json.loads(value))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.json"
            save_agent_config(
                path,
                {"provider": "google", "apiKey": "private-key", "model": "gemini-test"},
            )
            websocket = FakeWebsocket()
            agent = PiRpcAgent("pi", Path(directory) / "sessions", Path(directory))
            with patch.object(
                agent,
                "available_models",
                AsyncMock(return_value=[{
                    "id": "gemini-test",
                    "name": "Gemini Test",
                    "reasoning": True,
                    "contextWindow": 1000000,
                }]),
            ):
                asyncio.run(
                    handle_agent_config_query(
                        websocket,
                        {"requestId": "config-read-1"},
                        agent,
                        path,
                    )
                )
            response = websocket.messages[0]
            self.assertEqual(response["type"], "agent.config")
            self.assertEqual(response["provider"], "google")
            self.assertEqual(response["model"], "gemini-test")
            self.assertGreater(len(response["providers"]), 20)
            self.assertEqual(response["models"][0]["id"], "gemini-test")
            self.assertNotIn("apiKey", response)


if __name__ == "__main__":
    unittest.main()
