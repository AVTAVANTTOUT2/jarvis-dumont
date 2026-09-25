"""Persisted TLS front survives launchd without inherited shell environment."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jarvis_office.config import ConfigError, load_config
from jarvis_office.local_ui import LocalUI, tailscale_front
from jarvis_office.voice import _run_owned

PUBLIC_HOST = "wrist-test.tail-test.ts.net"
ENV_HOST = "previous-front.tail-test.ts.net"


class PublicHostConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.toml"

    def test_absent_and_empty_public_host_are_optional(self):
        for source in ("", '[voice]\npublic_host = ""\n'):
            with self.subTest(source=source):
                self.path.write_text(source)
                self.assertEqual(load_config(self.path).voice.public_host, "")

    def test_valid_public_hosts_follow_existing_front_normalization(self):
        for value in (PUBLIC_HOST, "  WRIST-TEST.TAIL-TEST.TS.NET.  ", "wrist-test.ts.net"):
            with self.subTest(value=value):
                self.path.write_text(f"[voice]\npublic_host = {json.dumps(value)}\n")
                config = load_config(self.path)
                self.assertEqual(config.voice.public_host, value)
                normalized = value.strip().rstrip(".").lower()
                self.assertEqual(
                    tailscale_front(config.voice.public_host),
                    {
                        normalized: f"https://{normalized}",
                        f"{normalized}:443": f"https://{normalized}",
                    },
                )

    def test_invalid_host_or_type_is_rejected_without_echoing_input(self):
        invalid = (
            " ",
            "ts.net",
            "evil.example",
            "https://wrist-test.ts.net",
            "wrist-test.ts.net:443",
            "wrist-test.ts.net/path",
            "wrist-test.ts.net.evil.example",
            "user@wrist-test.ts.net",
            "wrist_test.ts.net",
            "-wrist-test.ts.net",
            "wrist-test-.ts.net",
            "wrist-test..ts.net",
            "a" * 64 + ".ts.net",
            "wrist-test.ts.net\nAuthorization: fake",
            False,
            443,
            4.5,
            [],
        )
        for value in invalid:
            with self.subTest(value=value):
                self.path.write_text(f"[voice]\npublic_host = {json.dumps(value)}\n")
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.path)
                self.assertEqual(str(caught.exception), "invalid_public_host")


class PublicHostRunTests(unittest.IsolatedAsyncioTestCase):
    async def run_ui(self, source, environment):
        """Run the real startup/controller handoff, replacing only external boundaries."""
        voice = SimpleNamespace(
            session="test-session",
            web_conversation=True,
            audio=SimpleNamespace(ready={}),
            tts=SimpleNamespace(ready={"sample_rate": 24000}),
            start=AsyncMock(),
            test_text=AsyncMock(),
            control=AsyncMock(),
            snapshot=lambda: {},
            error=None,
            shutdown_verified=True,
        )
        interfaces = []

        def make_ui(*args, **kwargs):
            ui = LocalUI(*args, **kwargs)
            interfaces.append(ui)
            return ui

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text(source)
            config = load_config(path)
            loop = asyncio.get_running_loop()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("jarvis_office.credentials.load_key", return_value="test-key"),
                patch("jarvis_office.voice.DeepSeek"),
                patch("jarvis_office.voice.VoiceLoop", return_value=voice),
                patch("jarvis_office.local_ui.LocalUI", side_effect=make_ui),
                patch.object(LocalUI, "start", new_callable=AsyncMock),
                patch.object(LocalUI, "close", new_callable=AsyncMock),
                patch.object(loop, "add_signal_handler"),
                patch.object(loop, "remove_signal_handler"),
                patch("builtins.print"),
            ):
                code = await _run_owned(
                    config,
                    path,
                    text="Jarvis, test.",
                    no_play=True,
                    arm=False,
                    report=None,
                    instance=Mock(),
                    log=Mock(),
                )
        self.assertEqual(code, 0)
        voice.control.assert_awaited_once_with("stop")
        self.assertEqual(len(interfaces), 1)
        return interfaces[0]

    async def test_configured_front_works_without_shell_environment(self):
        ui = await self.run_ui(f'[voice]\npublic_host = "{PUBLIC_HOST}"\n', {})
        for host in (PUBLIC_HOST, f"{PUBLIC_HOST}:443", ui.host):
            self.assertEqual(ui.route("GET", "/", {"host": host}, b"")[0], 200)
        self.assertEqual(ui.route("GET", "/", {"host": "evil.example"}, b"")[0], 403)
        headers = {
            "host": PUBLIC_HOST,
            "origin": f"https://{PUBLIC_HOST}",
            "sec-fetch-site": "same-origin",
            "x-jarvis-local": "1",
            "content-type": "application/json",
        }
        self.assertEqual(ui.route("POST", "/bootstrap", headers, b"{}")[0], 200)
        self.assertEqual(
            ui.route("POST", "/bootstrap", {**headers, "origin": ui.origin}, b"{}")[0], 403
        )

    async def test_optional_empty_host_keeps_existing_environment_front(self):
        ui = await self.run_ui(
            '[voice]\npublic_host = ""\n', {"JARVIS_LOCAL_PUBLIC_HOST": ENV_HOST}
        )
        self.assertEqual(ui.route("GET", "/", {"host": ENV_HOST}, b"")[0], 200)

    async def test_configured_front_takes_precedence_over_environment(self):
        ui = await self.run_ui(
            f'[voice]\npublic_host = "{PUBLIC_HOST}"\n',
            {"JARVIS_LOCAL_PUBLIC_HOST": ENV_HOST},
        )
        self.assertEqual(ui.route("GET", "/", {"host": PUBLIC_HOST}, b"")[0], 200)
        self.assertEqual(ui.route("GET", "/", {"host": ENV_HOST}, b"")[0], 403)


if __name__ == "__main__":
    unittest.main()
