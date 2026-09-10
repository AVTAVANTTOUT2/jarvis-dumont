import json
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from jarvis_office.echo.gateway import EchoGateway, Settings


class PrivateTLSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("OpenSSL unavailable")
        self.cert, self.key = self.root / "cert.pem", self.root / "key.pem"
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
                "/CN=Office test",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-keyout",
                str(self.key),
                "-out",
                str(self.cert),
            ],
            check=True,
            capture_output=True,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.secret = secrets.token_urlsafe(32)
        settings = Settings(
            "127.0.0.1",
            self.port,
            {"echo": self.secret},
            enabled=True,
            cert=str(self.cert),
            key=str(self.key),
        )
        self.gateway = EchoGateway(settings)
        self.server = await self.gateway.start()
        self.url = f"wss://127.0.0.1:{self.port}/control"
        self.trusted = ssl.create_default_context(cafile=str(self.cert))
        self.headers = {"X-Echo-Device": "echo", "Authorization": "Bearer " + self.secret}

    async def asyncTearDown(self):
        await self.gateway.close()
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

    async def test_trusted_tls_authenticates_and_starts_off(self):
        async with connect(self.url, ssl=self.trusted, additional_headers=self.headers) as ws:
            await ws.send(
                json.dumps(
                    {
                        "protocol": 1,
                        "type": "hello",
                        "sequence": 0,
                        "session_id": "",
                        "timestamp": time.monotonic_ns(),
                        "payload": {
                            "supported_protocol_versions": [1],
                            "uplink_rate": 16000,
                            "uplink_channels": 1,
                            "downlink_rate": 48000,
                            "downlink_channels": 1,
                        },
                    }
                )
            )
            self.assertEqual(json.loads(await ws.recv())["type"], "welcome")
            state = json.loads(await ws.recv())["payload"]
            self.assertEqual(state["mode"], "OFF")

    async def test_unapproved_certificate_is_refused(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            async with connect(self.url, additional_headers=self.headers):
                self.fail("untrusted TLS accepted")

    async def test_wrong_hostname_is_refused(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            async with connect(
                self.url,
                ssl=self.trusted,
                server_hostname="wrong.invalid",
                additional_headers=self.headers,
            ):
                self.fail("wrong TLS hostname accepted")

    async def test_revoked_device_and_browser_origin_are_refused(self):
        for headers, status in [
            ({**self.headers, "Authorization": "Bearer " + secrets.token_urlsafe(32)}, 401),
            ({**self.headers, "Origin": "http://127.0.0.1:8768"}, 403),
        ]:
            with self.assertRaises(InvalidStatus) as caught:
                async with connect(self.url, ssl=self.trusted, additional_headers=headers):
                    self.fail("invalid principal accepted")
            self.assertEqual(caught.exception.response.status_code, status)


if __name__ == "__main__":
    unittest.main()
