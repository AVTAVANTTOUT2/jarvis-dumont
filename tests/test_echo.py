import asyncio
import json
import secrets
import socket
import time
import unittest
from dataclasses import replace

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from jarvis_office.echo.context import PassiveContextBuffer, PassivePolicy
from jarvis_office.echo.gateway import EchoGateway, Settings
from jarvis_office.echo.protocol import HEADER, Packet, Sequence
from jarvis_office.voice import addressed


class EchoUnitTests(unittest.TestCase):
    def test_packet_contract_and_corruption(self):
        p = Packet(7, 0, 10, 12, 48000, 1, bytes(1920))
        self.assertEqual(HEADER.size, 48)
        self.assertEqual(Packet.decode(p.encode()), p)
        for offset in (0, 4, 7, 37, 39, 47):
            data = bytearray(p.encode())
            data[offset] += 1
            with self.assertRaises(ValueError):
                Packet.decode(bytes(data))
        with self.assertRaises(ValueError):
            replace(p, pcm=bytes(1922)).encode()
        with self.assertRaises(ValueError):
            replace(p, captured_ns=13).encode()
        sequence = Sequence()
        sequence.accept(0)
        with self.assertRaises(ValueError):
            sequence.accept(2)

    def test_passive_synthetic_text_zero_cloud_requests_until_addressed(self):
        clock = [0.0]
        buffer = PassiveContextBuffer(
            clock=lambda: clock[0], seconds=1800, chars=20000, utterances=100
        )
        policy = PassivePolicy(buffer, addressed)
        cloud_requests = []
        for text in ("On va commander à manger.", "Prenons des pizzas."):
            request = policy.accept(text, mode="PASSIVE")
            if request:
                cloud_requests.append(request)
        self.assertEqual(cloud_requests, [])
        self.assertEqual(buffer.count, 2)
        request = policy.accept("Jarvis, qu'est-ce qu'on vient de dire ?", mode="PASSIVE")
        self.assertIsNotNone(request)
        self.assertIn("Contexte passivement entendu", request[1])
        self.assertIn("pizzas", request[1])
        policy.accept("Ma propre réponse", mode="PASSIVE", speaking=True)
        policy.accept("Ignoré", mode="ACTIVE")
        policy.accept("Ignoré", mode="OFF")
        self.assertEqual(buffer.count, 2)
        buffer.clear()
        self.assertEqual(buffer.count, 0)
        buffer.add("expiration")
        clock[0] = 1800
        self.assertEqual(buffer.count, 0)

    def test_all_context_bounds(self):
        buffer = PassiveContextBuffer(chars=10, utterances=2)
        for text in ("1111", "2222", "3333"):
            buffer.add(text)
        self.assertEqual(buffer.count, 2)
        self.assertNotIn("1111", buffer.recent())
        buffer.add("x" * 100)
        self.assertEqual(buffer.count, 1)
        self.assertTrue(buffer.recent().endswith("x" * 10))

    def test_explicit_bind_auth_and_tls_required(self):
        valid = Settings(
            "127.0.0.1", 8771, {"synthetic": "x" * 43}, enabled=True, security="DEV_INSECURE_LAN"
        )
        valid.validate()
        for bad in (
            replace(valid, bind="0.0.0.0"),
            replace(valid, enabled=False),
            replace(valid, devices={}),
            replace(valid, security="SECURE_RELEASE"),
            replace(valid, bind="8.8.8.8"),
            replace(valid, port=8768),
        ):
            with self.assertRaises(ValueError):
                bad.validate()


class EchoTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.secret = secrets.token_urlsafe(32)
        self.gateway = EchoGateway(
            Settings(
                "127.0.0.1",
                port,
                {"synthetic": self.secret},
                enabled=True,
                security="DEV_INSECURE_LAN",
                test_audio=True,
            )
        )
        self.server = await self.gateway.start()
        self.url = f"ws://127.0.0.1:{port}"
        self.headers = {"Authorization": "Bearer " + self.secret, "X-Echo-Device": "synthetic"}
        self.control = self.audio = None
        self.sequence = 0
        self.session = ""

    async def asyncTearDown(self):
        if self.control:
            await self.control.close()
        if self.audio:
            await self.audio.close()
        await self.gateway.close()
        self.server.close()
        await self.server.wait_closed()

    async def send(self, kind, payload=None):
        message = {
            "type": kind,
            "protocol": 1,
            "session_id": self.session,
            "sequence": self.sequence,
            "timestamp": time.monotonic_ns(),
            "payload": payload or {},
        }
        self.sequence += 1
        await self.control.send(json.dumps(message))

    async def receive(self, kind):
        async with asyncio.timeout(3):
            for _ in range(10):
                m = json.loads(await self.control.recv())
                if m["type"] == kind:
                    return m
        self.fail("missing " + kind)

    async def handshake(self):
        self.control = await connect(
            self.url + "/control", additional_headers=self.headers, proxy=None
        )
        await self.send(
            "hello",
            {
                "supported_protocol_versions": [1],
                "uplink_rate": 48000,
                "uplink_channels": 1,
                "downlink_rate": 48000,
                "downlink_channels": 1,
            },
        )
        welcome = await self.receive("welcome")
        self.session = welcome["session_id"]
        self.audio = await connect(
            self.url + "/audio",
            proxy=None,
            additional_headers={**self.headers, "X-Echo-Session": self.session},
        )
        await self.receive("audio_ready")

    async def test_authentication_precedes_upgrade_and_audio(self):
        with self.assertRaises(InvalidStatus) as rejected:
            await connect(self.url + "/audio", proxy=None)
        self.assertEqual(rejected.exception.response.status_code, 401)
        with self.assertRaises(InvalidStatus):
            await connect(self.url + "/audio", additional_headers=self.headers, proxy=None)
        self.assertEqual(self.gateway.sessions, {})

    async def test_synthetic_duplex_heartbeat_clear_reconnect_and_no_replay(self):
        await self.handshake()
        original = self.session
        now = time.monotonic_ns()
        await self.send("ping", {"client_send_ns": now})
        pong = await self.receive("pong")
        self.assertEqual(pong["payload"]["client_send_ns"], now)
        await self.send("set_mode", {"mode": "PASSIVE"})
        start = await self.receive("audio_start")
        stream = start["payload"]["stream_id"]
        for n in range(10):
            now = time.monotonic_ns()
            await self.audio.send(Packet(stream, n, now, now, 48000, 1, bytes(1920), 1).encode())
            await asyncio.sleep(0.02)
        s = self.gateway.sessions["synthetic"]
        self.assertEqual(s.metrics["uplink_frames"], 10)
        self.assertEqual(s.metrics["uplink_bytes"], 19200)
        s.context.add("synthetic only")
        await self.send("clear_context")
        self.assertEqual((await self.receive("context_state"))["payload"]["count"], 0)
        await self.send("test_tone")
        speaking = (await self.receive("speaking_started"))["payload"]
        down_stream = speaking["stream_id"]
        await self.send("playback_ready", {"stream_id": down_stream})
        frames = []
        async with asyncio.timeout(3):
            for n in range(20):
                frame = Packet.decode(await self.audio.recv())
                self.assertEqual(frame.stream, down_stream)
                self.assertEqual(frame.sequence, n)
                frames.append(frame)
        self.assertTrue(any(any(frame.pcm) for frame in frames))
        await self.receive("audio_end")
        await self.send("playback_drained", {"stream_id": down_stream})
        await self.receive("speaking_finished")
        await self.control.close()
        async with asyncio.timeout(2):
            await self.audio.wait_closed()
            while self.gateway.sessions:
                await asyncio.sleep(0.01)
        self.sequence = 0
        self.session = ""
        await self.handshake()
        self.assertNotEqual(original, self.session)
        s = self.gateway.sessions["synthetic"]
        self.assertEqual(s.mode, "OFF")
        self.assertEqual(s.metrics.get("uplink_frames", 0), 0)
        await self.send("set_mode", {"mode": "ACTIVE"})
        await self.receive("audio_start")
        now = time.monotonic_ns()
        await self.audio.send(Packet(stream, 10, now, now, 48000, 1, bytes(1920)).encode())
        self.assertEqual((await self.receive("error"))["payload"]["code"], "STALE_AUDIO_STREAM")

    async def test_sequence_loss_closes_audio_and_control(self):
        await self.handshake()
        await self.send("set_mode", {"mode": "ACTIVE"})
        stream = (await self.receive("audio_start"))["payload"]["stream_id"]
        now = time.monotonic_ns()
        await self.audio.send(Packet(stream, 3, now, now, 48000, 1, bytes(1920)).encode())
        self.assertEqual((await self.receive("error"))["payload"]["code"], "SEQUENCE_GAP")
        with self.assertRaises(ConnectionClosed):
            async with asyncio.timeout(2):
                while True:
                    await self.control.recv()

    async def test_incompatible_version_is_explicit(self):
        self.control = await connect(
            self.url + "/control", additional_headers=self.headers, proxy=None
        )
        await self.send("hello", {"supported_protocol_versions": [99]})
        self.assertEqual((await self.receive("error"))["payload"]["code"], "PROTOCOL_INCOMPATIBLE")

    async def test_uplink_deadline_overrun_fails_session(self):
        await self.handshake()
        await self.send("set_mode", {"mode": "ACTIVE"})
        stream = (await self.receive("audio_start"))["payload"]["stream_id"]
        now = time.monotonic_ns()
        await self.audio.send(
            Packet(stream, 0, now - 201_000_000, now, 48000, 1, bytes(1920)).encode()
        )
        self.assertEqual((await self.receive("error"))["payload"]["code"], "AUDIO_UPLINK_OVERRUN")

    async def test_off_never_processes_audio_and_transition_retires_stream(self):
        await self.handshake()
        s = self.gateway.sessions["synthetic"]
        now = time.monotonic_ns()
        await self.audio.send(Packet(123, 0, now, now, 48000, 1, bytes(1920)).encode())
        await asyncio.sleep(0.02)
        self.assertEqual(s.metrics.get("uplink_frames", 0), 0)
        await self.send("set_mode", {"mode": "ACTIVE"})
        old = (await self.receive("audio_start"))["payload"]["stream_id"]
        await self.send("set_mode", {"mode": "PASSIVE"})
        new = (await self.receive("audio_start"))["payload"]["stream_id"]
        self.assertNotEqual(old, new)
        now = time.monotonic_ns()
        await self.audio.send(Packet(old, 0, now, now, 48000, 1, bytes(1920)).encode())
        await self.audio.send(Packet(new, 0, now, now, 48000, 1, bytes(1920)).encode())
        await asyncio.sleep(0.02)
        self.assertEqual(s.metrics["uplink_frames"], 1)


if __name__ == "__main__":
    unittest.main()
