import asyncio
import json
import os
import sys
import tempfile
import textwrap
import threading
import unittest
import urllib.request
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from daemon import (
    PiRpcAgent,
    ProvisioningServer,
    apply_agent_config,
    has_network_connection,
    handle_agent_configuration,
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
                        1,
                    )
                )

    def test_saves_and_applies_agent_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.json"
            saved = save_agent_config(
                path,
                {"provider": "anthropic", "apiKey": "sk-ant-test", "model": "claude-test"},
            )
            self.assertEqual(load_agent_config(path), saved)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch.dict(os.environ, {}, clear=False):
                apply_agent_config(saved)
                self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-test")

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


if __name__ == "__main__":
    unittest.main()
