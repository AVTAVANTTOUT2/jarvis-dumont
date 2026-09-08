"""Explicitly configured, authenticated LAN transport. No AI or local audio starts here."""

import argparse
import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import signal
import ssl
import struct
import time
import tomllib
import uuid
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .context import PassiveContextBuffer
from .protocol import VERSION, Packet, Sequence, control


@dataclass(frozen=True, repr=False)
class Settings:
    bind: str
    port: int
    devices: dict[str, str]
    security: str = "SECURE_RELEASE"
    enabled: bool = False
    test_audio: bool = False
    cert: str = ""
    key: str = ""
    context_seconds: int = 1800
    context_chars: int = 20000
    context_utterances: int = 100
    max_audio_ms: int = 200

    def validate(self) -> None:
        address = ipaddress.ip_address(self.bind)
        if not self.enabled or address.is_unspecified or address.is_multicast:
            raise ValueError("EXPLICIT_ECHO_BIND_REQUIRED")
        if not 1024 <= self.port <= 65535 or self.port == 8768:
            raise ValueError("INVALID_ECHO_PORT")
        if self.security not in {"DEV_INSECURE_LAN", "SECURE_RELEASE"}:
            raise ValueError("INVALID_SECURITY_MODE")
        if self.security == "DEV_INSECURE_LAN" and not (address.is_private or address.is_loopback):
            raise ValueError("DEV_REQUIRES_PRIVATE_BIND")
        if self.security == "SECURE_RELEASE" and not (self.cert and self.key):
            raise ValueError("TLS_CERTIFICATE_REQUIRED")
        if not self.devices or len(self.devices) > 8 or not 40 <= self.max_audio_ms <= 200:
            raise ValueError("INVALID_ECHO_LIMITS")
        for device, secret in self.devices.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device) or not re.fullmatch(
                r"[A-Za-z0-9_-]{43,128}", secret
            ):
                raise ValueError("INVALID_PAIRING")
        PassiveContextBuffer(
            seconds=self.context_seconds,
            chars=self.context_chars,
            utterances=self.context_utterances,
        )

    @classmethod
    def load(cls, path: Path) -> "Settings":
        if path.stat().st_mode & 0o077:
            raise ValueError("PRIVATE_CONFIG_PERMISSIONS_REQUIRED")
        data = tomllib.loads(path.read_text())
        result = cls(**data["echo"], devices=data["devices"])
        result.validate()
        return result


@dataclass(repr=False)
class Session:
    device: str
    control: ServerConnection
    context: PassiveContextBuffer
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    audio: ServerConnection | None = None
    mode: str = "OFF"
    up_stream: int = 0
    down_stream: int = 0
    rate: int = 48000
    channels: int = 1
    down_rate: int = 48000
    down_channels: int = 1
    current_turn: str = ""
    tx: int = 0
    rx: Sequence = field(default_factory=Sequence)
    up_sequence: Sequence = field(default_factory=Sequence)
    alive: bool = True
    last_seen: float = field(default_factory=time.monotonic)
    speaking_until: float = 0
    playback_ready: asyncio.Event = field(default_factory=asyncio.Event)
    playback_drained: asyncio.Event = field(default_factory=asyncio.Event)
    turn_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    metrics: dict[str, Any] = field(default_factory=dict)
    rtts: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    first_capture: int = 0
    first_receive: int = 0
    retired_streams: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=4))

    async def send(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        async with self.lock:
            if not self.alive:
                return
            message = {
                "type": kind,
                "protocol": VERSION,
                "session_id": self.id,
                "sequence": self.tx,
                "timestamp": time.monotonic_ns(),
                "payload": payload or {},
            }
            self.tx += 1
            async with asyncio.timeout(0.5):
                await self.control.send(json.dumps(message, separators=(",", ":")))

    def snapshot(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "mode": self.mode,
            "connected": self.alive,
            "audio_uplink": "STREAMING" if self.up_stream else "STOPPED",
            "audio_downlink": "STREAMING" if self.down_stream else "STOPPED",
            "context_count": self.context.count,
            "turn_id": self.current_turn,
            "server": "TRANSPORT_ONLY",
            "rtt_ms": self.metrics.get("rtt_ms"),
            "last_seen_age_ms": (time.monotonic() - self.last_seen) * 1000,
        }


class EchoGateway:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.sessions: dict[str, Session] = {}
        self.contexts: dict[str, PassiveContextBuffer] = {}
        self.owner: str | None = None

    def authenticate(self, connection: ServerConnection, request: Request) -> Response | None:
        try:
            device = request.headers.get("X-Echo-Device", "")
            token = request.headers.get("Authorization", "")
            expected = self.settings.devices.get(device)
            valid = expected is not None and hmac.compare_digest(token, "Bearer " + expected)
            if not valid or request.path not in {"/control", "/audio"}:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "PAIRING_REQUIRED\n")
            if request.headers.get("Origin") is not None:
                return connection.respond(HTTPStatus.FORBIDDEN, "NATIVE_CLIENT_REQUIRED\n")
            if request.path == "/audio":
                session = self.sessions.get(device)
                if (
                    not session
                    or not session.alive
                    or session.audio
                    or request.headers.get("X-Echo-Session") != session.id
                ):
                    return connection.respond(HTTPStatus.FORBIDDEN, "INVALID_AUDIO_SESSION\n")
        except (KeyError, ValueError):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "PAIRING_REQUIRED\n")
        return None

    async def start(self) -> Server:
        tls = None
        if self.settings.security == "SECURE_RELEASE":
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(self.settings.cert, self.settings.key)
        # Avoid request headers, audio or exception representations in normal logs.
        logger = logging.getLogger("jarvis_office.echo.transport")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
        return await serve(
            self.handle,
            self.settings.bind,
            self.settings.port,
            ssl=tls,
            process_request=self.authenticate,
            compression=None,
            max_size=8192,
            max_queue=4,
            write_limit=4096,
            open_timeout=5,
            close_timeout=1,
            ping_interval=None,
            origins=[None],
            logger=logger,
        )

    async def handle(self, socket: ServerConnection) -> None:
        request = socket.request
        if request is None:
            return
        device = request.headers["X-Echo-Device"]
        if request.path == "/audio":
            session = self.sessions.get(device)
            if (
                session
                and session.alive
                and not session.audio
                and request.headers.get("X-Echo-Session") == session.id
            ):
                await self.audio(session, socket)
            else:
                await socket.close(1008, "STALE_SESSION")
            return
        if device in self.sessions:
            await socket.close(1008, "DEVICE_ALREADY_CONNECTED")
            return
        buffer = self.contexts.setdefault(
            device,
            PassiveContextBuffer(
                seconds=self.settings.context_seconds,
                chars=self.settings.context_chars,
                utterances=self.settings.context_utterances,
            ),
        )
        session = Session(device, socket, buffer)
        self.sessions[device] = session
        try:
            async with asyncio.timeout(5):
                hello = control(await socket.recv(), "", session.rx)
            p = hello["payload"]
            if hello["type"] != "hello" or VERSION not in p.get("supported_protocol_versions", []):
                raise ValueError("PROTOCOL_INCOMPATIBLE")
            session.rate, session.channels = p.get("uplink_rate"), p.get("uplink_channels")
            session.down_rate, session.down_channels = (
                p.get("downlink_rate"),
                p.get("downlink_channels"),
            )
            if (
                session.rate not in (16000, 48000)
                or session.channels != 1
                or session.down_rate not in (16000, 48000)
                or session.down_channels not in (1, 2)
            ):
                raise ValueError("AUDIO_FORMAT_UNSUPPORTED")
            await session.send(
                "welcome",
                {
                    "echo_protocol_version": VERSION,
                    "uplink_rate": session.rate,
                    "downlink_rate": session.down_rate,
                    "frame_ms": 20,
                    "max_audio_ms": self.settings.max_audio_ms,
                    "security": self.settings.security,
                    "pipeline": "NEXT_PHASE",
                },
            )
            await session.send("state", session.snapshot())
            while session.alive:
                async with asyncio.timeout(10):
                    incoming = await socket.recv()
                received_ns = time.monotonic_ns()
                message = control(incoming, session.id, session.rx)
                await self.command(session, message, received_ns)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            code = (
                str(exc)
                if isinstance(exc, ValueError) and re.fullmatch("[A-Z_]+", str(exc))
                else "CONTROL_INVALID"
            )
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                await session.send("error", {"code": code})
        except (ConnectionClosed, TimeoutError):
            pass
        finally:
            await self.drop(session)

    async def command(self, s: Session, message: dict[str, Any], received_ns: int) -> None:
        kind, p = message["type"], message["payload"]
        if time.monotonic() - s.last_seen > 10:
            raise ValueError("HEARTBEAT_TIMEOUT")
        if kind == "ping":
            sent = p.get("client_send_ns")
            if type(sent) is not int or sent < 0:
                raise ValueError("INVALID_PING")
            s.last_seen = time.monotonic()
            await s.send(
                "pong",
                {
                    "client_send_ns": sent,
                    "server_receive_ns": received_ns,
                    "server_send_ns": time.monotonic_ns(),
                },
            )
        elif kind == "set_mode":
            mode = p.get("mode")
            if mode not in {"OFF", "ACTIVE", "PASSIVE"}:
                raise ValueError("INVALID_MODE")
            if mode != "OFF" and s.current_turn:
                await s.send("error", {"code": "TURN_BUSY"})
                return
            if mode != "OFF" and (not s.audio or self.owner not in (None, s.id)):
                await s.send("error", {"code": "AUDIO_SESSION_BUSY"})
                return
            await self.stop_audio(s, notify=mode == "OFF")
            s.mode = mode
            if mode != "OFF":
                self.owner = s.id
            await s.send("state", s.snapshot())
            if mode != "OFF":
                s.up_stream = secrets.randbits(63) or 1
                s.up_sequence = Sequence()
                s.first_capture = s.first_receive = 0
                await s.send("audio_start", {"stream_id": s.up_stream})
        elif kind == "clear_context":
            s.context.clear()
            await s.send("context_state", {"count": 0})
        elif kind == "client_metrics":
            # Fixed numeric allowlist; never retain arbitrary text supplied by the client.
            for key in (
                "rtt_ms",
                "jitter_ms",
                "a1_a2_ms",
                "read_to_enqueue_ms",
                "a3_a4_ms",
                "a4_write_ms",
                "queue_depth",
                "underruns",
                "clock_uncertainty_ms",
            ):
                value = p.get(key)
                if type(value) in (float, int) and math.isfinite(value) and abs(value) < 1e9:
                    s.metrics[key] = value
                    if key == "rtt_ms":
                        s.rtts.append(float(value))
        elif kind in {"playback_ready", "playback_drained"}:
            if p.get("stream_id") != s.down_stream or not s.down_stream:
                raise ValueError("STALE_PLAYBACK_ACK")
            (s.playback_ready if kind == "playback_ready" else s.playback_drained).set()
        elif kind == "test_tone":
            if not self.settings.test_audio:
                await s.send("error", {"code": "TEST_AUDIO_DISABLED"})
            elif self.owner not in (None, s.id) or (s.turn_task and not s.turn_task.done()):
                await s.send("error", {"code": "TURN_BUSY"})
            else:
                s.turn_task = asyncio.create_task(self.tone(s))
        elif kind == "audio_stop":
            await self.stop_audio(s)
            await s.send("state", s.snapshot())
        else:
            raise ValueError("UNKNOWN_CONTROL_MESSAGE")

    async def audio(self, s: Session, socket: ServerConnection) -> None:
        s.audio = socket
        try:
            await s.send("audio_ready", {})
            async for message in socket:
                m1 = time.monotonic_ns()
                if not s.alive or s.audio is not socket:
                    break
                if not isinstance(message, bytes):
                    raise ValueError("BINARY_AUDIO_REQUIRED")
                frame = Packet.decode(message)
                if s.mode == "OFF" or not s.up_stream:
                    # Frames already in flight after STOP are discarded, never routed.
                    continue
                if any(
                    frame.stream == old and time.monotonic() < deadline
                    for old, deadline in s.retired_streams
                ):
                    continue
                if (
                    frame.stream != s.up_stream
                    or frame.rate != s.rate
                    or frame.channels != s.channels
                    or (frame.flags and not self.settings.test_audio)
                ):
                    raise ValueError("STALE_AUDIO_STREAM")
                s.up_sequence.accept(frame.sequence)
                if time.monotonic() < s.speaking_until or s.down_stream:
                    continue
                if not s.first_receive:
                    s.first_receive, s.first_capture = m1, frame.captured_ns
                # Compare elapsed durations within each clock, never subtract absolute clocks.
                backlog = (m1 - s.first_receive) - (frame.captured_ns - s.first_capture)
                if (
                    backlog > self.settings.max_audio_ms * 1_000_000
                    or frame.enqueued_ns - frame.captured_ns
                    > self.settings.max_audio_ms * 1_000_000
                ):
                    raise ValueError("AUDIO_UPLINK_OVERRUN")
                s.metrics["uplink_frames"] = s.metrics.get("uplink_frames", 0) + 1
                s.metrics["uplink_bytes"] = s.metrics.get("uplink_bytes", 0) + len(frame.pcm)
                s.metrics["uplink_sequence"] = frame.sequence
                s.metrics["uplink_drops"] = 0
                s.metrics["m1_m2_ms"] = (time.monotonic_ns() - m1) / 1e6
                # Transport qualification sink only. PCM is released here; no STT/model/file.
        except ValueError as exc:
            s.metrics["uplink_drops"] = s.metrics.get("uplink_drops", 0) + 1
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                await s.send("error", {"code": str(exc)})
        except (ConnectionClosed, TimeoutError):
            pass
        finally:
            await self.drop(s)

    async def stop_audio(self, s: Session, *, notify: bool = True) -> None:
        if s.up_stream:
            s.retired_streams.append((s.up_stream, time.monotonic() + 0.2))
        s.up_stream = 0
        s.mode = "OFF"
        if self.owner == s.id:
            self.owner = None
        if s.turn_task and s.turn_task is not asyncio.current_task() and not s.turn_task.done():
            s.turn_task.cancel()
            await asyncio.gather(s.turn_task, return_exceptions=True)
        s.down_stream = 0
        s.current_turn = ""
        if notify:
            await s.send("stop_audio", {})

    async def tone(self, s: Session) -> None:
        try:
            await self.stop_audio(s)
            if not s.audio or not s.alive:
                return
            s.down_stream = secrets.randbits(63) or 1
            stream, session_id = s.down_stream, s.id
            s.current_turn = uuid.uuid4().hex
            s.playback_ready.clear()
            s.playback_drained.clear()
            await s.send(
                "speaking_started",
                {
                    "stream_id": stream,
                    "turn_id": s.current_turn,
                    "rate": s.down_rate,
                    "channels": s.down_channels,
                },
            )
            async with asyncio.timeout(3):
                await s.playback_ready.wait()
            start = time.monotonic()
            count = s.down_rate // 50
            for sequence in range(20):
                await asyncio.sleep(max(0, start + sequence * 0.02 - time.monotonic()))
                if not s.alive or s.id != session_id or s.down_stream != stream:
                    return
                # 440 Hz, -30 dBFS, 400 ms including 10 ms ramps; never changes device volume.
                values = []
                for i in range(count):
                    t = (sequence * count + i) / s.down_rate
                    gain = min(1.0, t / 0.01, max(0, (0.4 - t) / 0.01))
                    values.extend(
                        [int(1000 * gain * math.sin(2 * math.pi * 440 * t))] * s.down_channels
                    )
                pcm = struct.pack(f"<{len(values)}h", *values)
                m3 = time.monotonic_ns()
                m4 = time.monotonic_ns()
                packet = Packet(stream, sequence, m3, m4, s.down_rate, s.down_channels, pcm)
                async with asyncio.timeout(self.settings.max_audio_ms / 1000):
                    await s.audio.send(packet.encode())
                s.metrics["m3_m4_ms"] = (m4 - m3) / 1e6
                s.metrics["downlink_frames"] = sequence + 1
            await s.send("audio_end", {"stream_id": stream, "last_sequence": 19})
            async with asyncio.timeout(3):
                await s.playback_drained.wait()
            s.speaking_until = time.monotonic() + 0.2
            await s.send("speaking_finished", {"stream_id": stream})
        except (ConnectionClosed, TimeoutError):
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                await s.send("error", {"code": "AUDIO_DOWNLINK_OVERRUN"})
        finally:
            s.down_stream = 0
            s.current_turn = ""
            s.mode = "OFF"
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                await s.send("state", s.snapshot())

    async def drop(self, s: Session) -> None:
        if not s.alive:
            return
        s.alive = False
        await self.stop_audio(s)
        if s.audio:
            await s.audio.close(1000, "SESSION_ENDED")
        await s.control.close(1000, "SESSION_ENDED")
        if self.sessions.get(s.device) is s:
            self.sessions.pop(s.device, None)

    async def close(self) -> None:
        await asyncio.gather(*(self.drop(s) for s in list(self.sessions.values())))
        for buffer in self.contexts.values():
            buffer.clear()


async def run(settings: Settings) -> None:
    gateway = EchoGateway(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with await gateway.start():
        print("ECHO_GATEWAY_RUNNING " + settings.security + " TRANSPORT_ONLY", flush=True)
        await stop.wait()
        await gateway.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated Echo gateway; no AI pipeline starts")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--pair", metavar="DEVICE")
    parser.add_argument("--bind", help="Required explicit private IP when creating pairing")
    args = parser.parse_args()
    try:
        if args.pair:
            secret = secrets.token_urlsafe(32)
            settings = Settings(
                bind=args.bind or "",
                port=8771,
                devices={args.pair: secret},
                enabled=True,
                security="DEV_INSECURE_LAN",
            )
            settings.validate()
            args.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # O_EXCL prevents silently overwriting a paired device or a symlink.
            fd = os.open(args.config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(
                    '[echo]\nenabled = true\nsecurity = "DEV_INSECURE_LAN"\n'
                    f'bind = "{settings.bind}"\nport = 8771\ntest_audio = false\n\n[devices]\n'
                    f'"{args.pair}" = "{secret}"\n'
                )
            print("PAIRING_CREATED_PRIVATE_FILE — provision through Android Settings")
        else:
            asyncio.run(run(Settings.load(args.config)))
        return 0
    except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
        print("ECHO_CONFIGURATION_ERROR — check private config, bind, pairing and TLS")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
