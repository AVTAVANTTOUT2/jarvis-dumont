"""HTTPS /tv/v1 against the Office listener. No production runtime, no secrets."""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp

from jarvis_office.storage import OfficeStore
from jarvis_office.tv.hub import TvHub, TvSettings
from jarvis_office.tv.protocol import SCHEMA, is_uuid, sha256_der_from_pem


def _make_tls(root: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise unittest.SkipTest("OpenSSL unavailable")
    cert, key = root / "cert.pem", root / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=Office TV test",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return cert, key


class TvProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cert, self.key = _make_tls(root)
        self.store = OfficeStore(root / "office.sqlite3")
        await self.store.start()
        self._wait = patch("jarvis_office.tv.hub.COMMANDS_WAIT_S", 0.2)
        self._wait.start()
        self.addCleanup(self._wait.stop)
        self.hub = TvHub(
            TvSettings("127.0.0.1", 0, str(self.cert), str(self.key)),
            self.store,
        )
        await self.hub.start()
        self.tls = ssl.create_default_context(cafile=str(self.cert))
        self.client = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.tls))
        self.url = f"https://127.0.0.1:{self.hub.port}"

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.hub.close()
        await self.store.close()
        self.temp.cleanup()

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        extra = dict(headers or {})
        if token is not None:
            extra["Authorization"] = "Bearer " + token
        kwargs: dict = {"headers": extra}
        if body is not None:
            kwargs["json"] = body
        async with self.client.request(method, self.url + path, **kwargs) as response:
            raw = await response.read()
            parsed = json.loads(raw.decode()) if raw else {}
            return response.status, parsed

    async def pair_session(self, **capabilities: object) -> tuple[str, dict]:
        apps = {
            "smarttube": {
                "installed": True,
                "player_kind": "native",
                "actions": ["get_state", "play_content"],
                "targeted_control": False,
                "state_observable": False,
                "missing_permissions": [],
            }
        }
        apps.update(capabilities)
        applied = await self.hub.owner_command({"action": "enable"})
        self.assertEqual(applied["status"], "applied")
        document = (await self.hub.owner_command({"action": "pair", "confirm": True}))["document"]
        status, paired = await self.request(
            "POST",
            "/tv/v1/pair",
            body={
                "schema_version": SCHEMA,
                "pairing_id": document["pairing_id"],
                "code": document["code"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(is_uuid(paired["device_id"]))
        self.assertNotEqual(paired["device_id"], "tv-test")
        self.assertTrue(1 <= len(paired["credential"]) <= 256)
        token = paired["credential"]
        status, session = await self.request(
            "POST",
            "/tv/v1/session",
            token=token,
            body={
                "schema_version": SCHEMA,
                "device_id": paired["device_id"],
                "client_instance_id": str(uuid.uuid4()),
                "apk_version": "1.0.0",
                "capabilities": {"apps": apps},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(session["heartbeat_timeout_ms"], 45000)
        return token, session

    async def test_pair_session_command_event_dedup_and_no_completed_from_intent(self) -> None:
        token, session = await self.pair_session(
            smarttube={
                "installed": True,
                "player_kind": "native",
                "actions": ["play_content", "get_state"],
                "targeted_control": False,
                "state_observable": False,
                "missing_permissions": [],
            }
        )
        command = await self.hub.issue(
            app="smarttube",
            action="play_content",
            args={"content": {"kind": "youtube_video", "id": "aqz-KE-bpKQ"}},
        )
        status, payload = await self.request(
            "GET",
            f"/tv/v1/commands?connection_id={session['connection_id']}",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["command"]["command_id"], command["command_id"])
        event_id = str(uuid.uuid4())
        result = {
            "schema_version": SCHEMA,
            "event_id": event_id,
            "device_id": session["device_id"],
            "server_epoch": session["server_epoch"],
            "connection_id": session["connection_id"],
            "sequence": 1,
            "kind": "command_result",
            "payload": {
                "command_id": command["command_id"],
                "status": "completed",
                "error_code": None,
                "result": None,
            },
        }
        posted = await self.request("POST", "/tv/v1/events", token=token, body=result)
        self.assertEqual(posted[0], 200)
        posted = await self.request("POST", "/tv/v1/events", token=token, body=result)
        self.assertEqual(posted[0], 200)
        device = self.hub.devices[session["device_id"]]
        self.assertEqual(device.pending[command["command_id"]]["status"], "dispatched")
        self.assertEqual(device.last_result["status"], "dispatched")

    async def test_wrong_identity_url_secret_expiry_reconnect_and_late_event(self) -> None:
        token, session = await self.pair_session()
        self.assertEqual(
            (
                await self.request(
                    "POST",
                    "/tv/v1/session",
                    token="wrong-token-value",
                    body={
                        "schema_version": SCHEMA,
                        "device_id": session["device_id"],
                        "client_instance_id": str(uuid.uuid4()),
                        "apk_version": "1.0.0",
                        "capabilities": {"apps": {}},
                    },
                )
            )[0],
            401,
        )
        self.assertEqual(
            (
                await self.request(
                    "GET",
                    f"/tv/v1/commands?connection_id={session['connection_id']}&token={token}",
                    token=token,
                )
            )[0],
            401,
        )
        expired = await self.hub.issue(app="smarttube", action="get_state", args={}, ttl_ms=1)
        self.hub.now = lambda: expired["expires_at_ms"] + 1
        with patch("jarvis_office.tv.hub.COMMANDS_WAIT_S", 0.05):
            status, payload = await self.request(
                "GET",
                f"/tv/v1/commands?connection_id={session['connection_id']}",
                token=token,
            )
        self.assertEqual(status, 200)
        self.assertIsNone(payload["command"])
        pending = self.hub.devices[session["device_id"]].pending
        self.assertEqual(pending[expired["command_id"]]["status"], "expired")
        unread = await self.hub.issue(app="smarttube", action="get_state", args={})
        status, second = await self.request(
            "POST",
            "/tv/v1/session",
            token=token,
            body={
                "schema_version": SCHEMA,
                "device_id": session["device_id"],
                "client_instance_id": str(uuid.uuid4()),
                "apk_version": "1.0.0",
                "capabilities": {"apps": {}},
            },
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(second["connection_id"], session["connection_id"])
        self.assertEqual(
            self.hub.devices[session["device_id"]].pending[unread["command_id"]]["status"],
            "unknown",
        )
        with patch("jarvis_office.tv.hub.COMMANDS_WAIT_S", 0.05):
            status, empty = await self.request(
                "GET",
                f"/tv/v1/commands?connection_id={second['connection_id']}",
                token=token,
            )
        self.assertEqual(status, 200)
        self.assertIsNone(empty["command"])
        late = {
            "schema_version": SCHEMA,
            "event_id": str(uuid.uuid4()),
            "device_id": session["device_id"],
            "server_epoch": session["server_epoch"],
            "connection_id": session["connection_id"],
            "sequence": 1,
            "kind": "playback_state",
            "payload": {"app": "smarttube", "observed_at_ms": 1, "valid_for_ms": 5000},
        }
        status, body = await self.request("POST", "/tv/v1/events", token=token, body=late)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "DISCONNECTED")

    async def test_host_forwarded_disable_revoke_and_untrusted_tls(self) -> None:
        token, session = await self.pair_session()
        status, _ = await self.request(
            "GET",
            f"/tv/v1/commands?connection_id={session['connection_id']}",
            token=token,
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        self.assertEqual(status, 403)
        huge = b'{"schema_version":1,"padding":"' + b"x" * 70000 + b'"}'
        async with self.client.post(
            self.url + "/tv/v1/events",
            data=huge,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        ) as response:
            self.assertEqual(response.status, 413)
        await self.hub.owner_command({"action": "disable"})
        self.assertEqual(
            (
                await self.request(
                    "POST",
                    "/tv/v1/session",
                    token=token,
                    body={
                        "schema_version": SCHEMA,
                        "device_id": session["device_id"],
                        "client_instance_id": str(uuid.uuid4()),
                        "apk_version": "1.0.0",
                        "capabilities": {"apps": {}},
                    },
                )
            )[0],
            401,
        )
        await self.hub.owner_command({"action": "enable"})
        status, _ = await self.request(
            "POST",
            "/tv/v1/session",
            token=token,
            body={
                "schema_version": SCHEMA,
                "device_id": session["device_id"],
                "client_instance_id": str(uuid.uuid4()),
                "apk_version": "1.0.0",
                "capabilities": {"apps": {}},
            },
        )
        self.assertEqual(status, 200)
        await self.hub.owner_command(
            {"action": "revoke", "confirm": True, "device_id": session["device_id"]}
        )
        self.assertEqual(
            (
                await self.request(
                    "POST",
                    "/tv/v1/session",
                    token=token,
                    body={
                        "schema_version": SCHEMA,
                        "device_id": session["device_id"],
                        "client_instance_id": str(uuid.uuid4()),
                        "apk_version": "1.0.0",
                        "capabilities": {"apps": {}},
                    },
                )
            )[0],
            401,
        )
        other = Path(self.temp.name) / "other"
        other.mkdir()
        other_cert, _other_key = _make_tls(other)
        bad = ssl.create_default_context(cafile=str(other_cert))
        with self.assertRaises(ssl.SSLCertVerificationError):
            connector = aiohttp.TCPConnector(ssl=bad)
            async with aiohttp.ClientSession(connector=connector) as client:
                async with client.get(self.url + "/tv/v1/commands"):
                    pass
        self.assertEqual(sha256_der_from_pem(self.cert.read_text()), self.hub.cert_sha256)

    async def test_pairing_requires_enable_and_owner_confirmation(self) -> None:
        rejected = await self.hub.owner_command({"action": "pair", "confirm": True})
        self.assertEqual(rejected["error"], "TV_DISABLED")
        await self.hub.owner_command({"action": "enable"})
        pair = await self.hub.owner_command({"action": "pair"})
        self.assertEqual(pair["error"], "CONFIRMATION_REQUIRED")
        document = (await self.hub.owner_command({"action": "pair", "confirm": True}))["document"]
        self.assertTrue(document["endpoint"].startswith("https://"))
        self.assertIn("BEGIN CERTIFICATE", document["ca_pem"])
        self.assertEqual(
            (
                await self.request(
                    "POST",
                    "/tv/v1/pair",
                    body={
                        "schema_version": SCHEMA,
                        "pairing_id": document["pairing_id"],
                        "code": "wrong",
                    },
                )
            )[0],
            401,
        )


if __name__ == "__main__":
    unittest.main()
