"""Tiny loopback control page, same-origin bootstrap and HttpOnly local session cookie.

No action or transcript on GET. POST APIs require exact Host + Origin, same-origin
Fetch Metadata, a custom header and a session cookie (except bootstrap). No CORS.
The static unauthenticated page contains no personal text, secret or control token.
"""

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from jarvis_office.audio_client import LoopError
from jarvis_office.config import tailscale_front
from jarvis_office.talk import SEATS, TALK_BATCH_BYTES, TalkBridge, TalkError
from jarvis_office.voice import VoiceLoop
from jarvis_office.web_audio import CAPTURE_BATCH_BYTES, FRAME_BYTES, WebPhoneAudio

HERE = Path(__file__).resolve().parent
PAGE = (HERE / "control.html").read_text(encoding="utf-8")


def _talk_http(exc: TalkError) -> tuple[int, bytes, dict[str, str]]:
    code = str(exc)
    if code == "session_limit":
        return 429, b"{}", {}
    if code in {
        "seat_taken",
        "call_busy",
        "peer_offline",
        "remote_pcm_backpressure",
        "stale_call",
        "not_in_call",
        "audio_stalled",
    }:
        return 409, b"{}", {}
    if code == "unknown_session":
        return 403, b"{}", {}
    return 400, b"{}", {}


SHELL = {
    "/manifest.webmanifest": (
        (HERE / "manifest.webmanifest").read_bytes(),
        "application/manifest+json",
    ),
    "/sw.js": ((HERE / "sw.js").read_bytes(), "application/javascript"),
    "/wrist.js": ((HERE / "wrist.js").read_bytes(), "text/javascript"),
    # Same vendored MIT engine as the dashboard: one copy, no runtime download.
    "/thinking-orbs.js": (
        (HERE / "static" / "dashboard" / "thinking-orbs.js").read_bytes(),
        "text/javascript",
    ),
    "/icon.svg": ((HERE / "icon.svg").read_bytes(), "image/svg+xml"),
    "/icon.png": ((HERE / "icon.png").read_bytes(), "image/png"),
}


class LocalUI:
    def __init__(self, voice: VoiceLoop, port: int, public_host: str | None = None) -> None:
        self.voice, self.port = voice, port
        self.host = f"127.0.0.1:{port}"
        self.origin = "http://" + self.host
        extra = (
            public_host
            if public_host is not None
            else os.environ.get("JARVIS_LOCAL_PUBLIC_HOST", "")
        )
        self.by_host = {self.host: self.origin, **tailscale_front(extra)}
        self.cookie_name = "jarvis_office_" + str(port)
        self.talk = TalkBridge()
        self.server: asyncio.Server | None = None
        self.connections = 0
        self.tasks: set[asyncio.Task[None]] = set()
        self.controls: asyncio.Queue[str] = asyncio.Queue(4)
        self.says: asyncio.Queue[str] = asyncio.Queue(4)
        self.control_task: asyncio.Task[None] | None = None
        self.say_task: asyncio.Task[None] | None = None
        self.phone_owner: tuple[str, str] | None = None
        self.control_running = False
        self.say_running = False

    async def start(self) -> None:
        try:
            self.server = await asyncio.start_server(
                self.handle, "127.0.0.1", self.port, limit=8192
            )
        except OSError:
            raise LoopError("loopback_port_unavailable") from None
        self.control_task = asyncio.create_task(self._control())
        self.say_task = asyncio.create_task(self._say())

    async def _control(self) -> None:
        while True:
            action = await self.controls.get()
            self.control_running = True
            try:
                await self.voice.control(action)
            except Exception as exc:
                self.voice.error = str(exc) if isinstance(exc, LoopError) else "control_failed"
                self.voice.state = "error"
            finally:
                self.control_running = False

    async def _say(self) -> None:
        while True:
            text = await self.says.get()
            self.say_running = True
            try:
                await self.voice.ask(text)
            except Exception as exc:
                self.voice.error = str(exc) if isinstance(exc, LoopError) else "control_failed"
                self.voice.state = "error"
            finally:
                self.say_running = False

    def _phone_owned(self, token: str, headers: dict[str, str]) -> bool:
        return self.phone_owner == (token, headers.get("x-jarvis-client", ""))

    def _phone_available(self, token: str, headers: dict[str, str]) -> bool:
        if not isinstance(self.voice.audio, WebPhoneAudio):
            return True
        client = headers.get("x-jarvis-client", "")
        if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", client):
            return False
        if self.talk.sessions[token].profile is None:
            return False
        if self._phone_owned(token, headers):
            return True
        return (
            self.voice.snapshot().get("state") in {"paused", "error"}
            and self.controls.empty()
            and self.says.empty()
            and not self.control_running
            and not self.say_running
        )

    def _forget_phone(self) -> None:
        self.phone_owner = None
        if isinstance(self.voice.audio, WebPhoneAudio):
            self.voice.audio.token = ""
            self.voice.audio.sink.abort()

    def _pause_phone(self) -> None:
        self._forget_phone()
        # A stop supersedes queued starts/questions from the expired acquisition.
        for queue in (self.controls, self.says):
            while not queue.empty():
                queue.get_nowait()
        self.controls.put_nowait("pause")

    def route(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes, dict[str, str]]:
        host = headers.get("host", "").lower()
        if host not in self.by_host:
            return 403, b"{}", {}
        path = path.split("?", 1)[0]
        if method == "GET" and path == "/":
            nonce = secrets.token_urlsafe(24)
            return (
                200,
                PAGE.replace("NONCE", nonce).encode(),
                {
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Security-Policy": (
                        f"default-src 'none'; script-src 'nonce-{nonce}' 'self'; "
                        f"style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self'; "
                        "manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
                        "base-uri 'none'; form-action 'none'"
                    ),
                },
            )
        if method == "GET" and path in SHELL:
            payload, content_type = SHELL[path]
            return 200, payload, {"Content-Type": content_type}
        if (
            method != "POST"
            or headers.get("origin") != self.by_host[host]
            or headers.get("sec-fetch-site") != "same-origin"
            or headers.get("x-jarvis-local") != "1"
        ):
            return 403, b"{}", {}
        content_type = headers.get("content-type", "")
        if path == "/bootstrap":
            if content_type != "application/json":
                return 403, b"{}", {}
            if self._cookie(headers) is not None:
                return 200, b"{}", {}
            try:
                issued = self.talk.register()
            except TalkError as exc:
                return _talk_http(exc)
            return (
                200,
                b"{}",
                {"Set-Cookie": (f"{self.cookie_name}={issued}; HttpOnly; SameSite=Strict; Path=/")},
            )
        token = self._cookie(headers)
        if token is None:
            return 403, b"{}", {}
        if path == "/uplink":
            if (
                content_type != "application/octet-stream"
                or not 0 < len(body) <= CAPTURE_BATCH_BYTES
                or len(body) % FRAME_BYTES
            ):
                return 400, b"{}", {}
            if self.talk.call is not None:
                return 200, b"{}", {}
            phone = self.voice.audio
            if isinstance(phone, WebPhoneAudio):
                if not self._phone_owned(token, headers):
                    return 409, b"{}", {}
                sequence = headers.get("x-jarvis-sequence", "")
                if not re.fullmatch(r"[0-9]{1,10}", sequence):
                    self._pause_phone()
                    return 400, b"{}", {}
                try:
                    phone.feed_uplink(
                        body,
                        capture=headers.get("x-jarvis-capture", ""),
                        sequence=int(sequence),
                    )
                except LoopError as exc:
                    if str(exc) == "stale_phone_capture":
                        return 409, b'{"error":"stale_phone_capture"}', {}
                    self._pause_phone()
                    return 409, b"{}", {}
            return 200, b"{}", {}
        if path == "/talk/uplink":
            if content_type != "application/octet-stream":
                return 400, b"{}", {}
            try:
                self.talk.feed(token, body, headers.get("x-jarvis-call", ""))
            except TalkError as exc:
                return _talk_http(exc)
            return 200, b"{}", {}
        if content_type != "application/json":
            return 403, b"{}", {}
        if path == "/crew/claim":
            try:
                value = json.loads(body)
                seat = value.get("id") if isinstance(value, dict) else None
                if (
                    not isinstance(value, dict)
                    or set(value) != {"id"}
                    or not isinstance(seat, str)
                    or seat not in SEATS
                ):
                    return 400, b"{}", {}
                if (
                    self.phone_owner is not None
                    and self.phone_owner[0] == token
                    and not self._phone_owned(token, headers)
                ):
                    return 409, b"{}", {}
                self.talk.claim(token, seat)
                return 200, b"{}", {}
            except (ValueError, TypeError):
                return 400, b"{}", {}
            except TalkError as exc:
                return _talk_http(exc)
        if path == "/crew/release":
            try:
                value = json.loads(body)
                if not isinstance(value, dict) or value:
                    return 400, b"{}", {}
                if (
                    self.phone_owner is not None
                    and self.phone_owner[0] == token
                    and not self._phone_owned(token, headers)
                ):
                    return 409, b"{}", {}
                self.talk.release(token)
                if self.phone_owner is not None and self.phone_owner[0] == token:
                    self._pause_phone()
                return 200, b"{}", {}
            except (ValueError, TypeError):
                return 400, b"{}", {}
            except TalkError as exc:
                return _talk_http(exc)
        if path == "/talk/call":
            try:
                value = json.loads(body)
                peer = value.get("peer") if isinstance(value, dict) else None
                if (
                    not isinstance(value, dict)
                    or set(value) != {"peer"}
                    or not isinstance(peer, str)
                    or peer not in SEATS
                ):
                    return 400, b"{}", {}
                self.talk.start_call(token, peer)
            except (ValueError, TypeError):
                return 400, b"{}", {}
            except TalkError as exc:
                return _talk_http(exc)
            self._pause_phone()
            return 200, b"{}", {}
        if path == "/talk/hangup":
            try:
                value = json.loads(body)
                if not isinstance(value, dict) or value:
                    return 400, b"{}", {}
                self.talk.hangup(token, headers.get("x-jarvis-call", ""))
                return 200, b"{}", {}
            except (ValueError, TypeError):
                return 400, b"{}", {}
            except TalkError as exc:
                return _talk_http(exc)
        if path == "/talk/pcm":
            try:
                raw = json.dumps(
                    self.talk.pull(token, headers.get("x-jarvis-call", "")),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode()
            except TalkError as exc:
                return _talk_http(exc)
            if len(raw) > 65536:
                return 503, b"{}", {}
            return 200, raw, {}
        if path == "/pcm":
            phone = self.voice.audio
            if isinstance(phone, WebPhoneAudio) and not self._phone_owned(token, headers):
                return 409, b"{}", {}
            pcm = (
                phone.pull_pcm()
                if isinstance(phone, WebPhoneAudio)
                else {"pcm": "", "rate": 0, "done": True}
            )
            raw = json.dumps(
                pcm, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            if len(raw) > 65536:
                return 503, b"{}", {}
            return 200, raw, {}
        if path == "/heard":
            try:
                value = json.loads(body)
                amount = value.get("bytes") if isinstance(value, dict) else None
                done = value.get("done") if isinstance(value, dict) else None
                if (
                    not isinstance(value, dict)
                    or set(value) != {"bytes", "done"}
                    or type(amount) is not int
                    or amount < 0
                    or type(done) is not bool
                ):
                    return 400, b"{}", {}
                phone = self.voice.audio
                if isinstance(phone, WebPhoneAudio):
                    if not self._phone_owned(token, headers):
                        return 409, b"{}", {}
                    phone.hear(amount, done)
                return 200, b"{}", {}
            except (ValueError, TypeError, LoopError):
                return 400, b"{}", {}
        if path == "/snapshot":
            data = self.voice.snapshot()
            data["crew"] = self.talk.view(token)
            phone = self.voice.audio
            if isinstance(phone, WebPhoneAudio):
                owned = self._phone_owned(token, headers)
                if isinstance(data.get("input"), dict):
                    data["input"] = {
                        key: value for key, value in data["input"].items() if key != "ingress_token"
                    }
                data["phone_audio"] = {
                    "owned": owned,
                    "capture": phone.token if owned and data.get("state") == "listening" else "",
                    "playback": phone.sink.turn if owned and not phone.sink.closed else "",
                }
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            if len(payload) > 65536:
                return 503, b"{}", {}
            return 200, payload, {}
        if path == "/control":
            try:
                value = json.loads(body)
                if (
                    not isinstance(value, dict)
                    or set(value) != {"action"}
                    or value["action"] not in {"resume", "pause", "cancel", "clear", "stop"}
                ):
                    return 400, b"{}", {}
                if value["action"] == "resume" and self.talk.call is not None:
                    return 409, b"{}", {}
                phone = isinstance(self.voice.audio, WebPhoneAudio)
                if phone and value["action"] == "resume":
                    if not self._phone_available(token, headers):
                        return 409, b"{}", {}
                elif (
                    phone and self.phone_owner is not None and not self._phone_owned(token, headers)
                ):
                    return 409, b"{}", {}
                self.controls.put_nowait(value["action"])
                if phone:
                    if value["action"] == "resume":
                        self.phone_owner = (token, headers["x-jarvis-client"])
                    else:
                        self._forget_phone()
                return 202, b"{}", {}
            except (ValueError, TypeError, asyncio.QueueFull):
                return 400, b"{}", {}
        if path == "/say":
            try:
                value = json.loads(body)
                text = value.get("text") if isinstance(value, dict) else None
                if (
                    not isinstance(value, dict)
                    or set(value) != {"text"}
                    or not isinstance(text, str)
                    or not text.strip()
                    or len(text) > 2000
                    or any(ord(c) < 32 and c not in "\t\n" for c in text)
                ):
                    return 400, b"{}", {}
                if self.talk.call is not None or not self._phone_available(token, headers):
                    return 409, b"{}", {}
                self.says.put_nowait(text.strip())
                if isinstance(self.voice.audio, WebPhoneAudio):
                    self.phone_owner = (token, headers["x-jarvis-client"])
                return 202, b"{}", {}
            except (ValueError, TypeError, asyncio.QueueFull):
                return 400, b"{}", {}
        return 404, b"{}", {}

    def _cookie(self, headers: dict[str, str]) -> str | None:
        self.talk.expire()
        if self.phone_owner is not None and self.phone_owner[0] not in self.talk.sessions:
            self._pause_phone()
        prefix = self.cookie_name + "="
        offered = [
            part.strip()[len(prefix) :]
            for part in headers.get("cookie", "").split(";")
            if part.strip().startswith(prefix)
        ]
        found = ""
        for token in self.talk.sessions:
            for value in offered:
                if hmac.compare_digest(value, token):
                    found = token
        if found:
            self.talk.touch(found)
        return found or None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        self.connections += 1
        try:
            async with asyncio.timeout(3):
                if self.connections > 32:
                    return
                raw = await reader.readuntil(b"\r\n\r\n")
                if len(raw) > 8192:
                    return
                lines = raw.decode("ascii").split("\r\n")
                method, path, version = lines[0].split(" ")
                if version != "HTTP/1.1":
                    return
                headers: dict[str, str] = {}
                for line in lines[1:-2]:
                    name, value = line.split(":", 1)
                    name = name.lower()
                    if name in headers or name != name.strip():
                        return
                    headers[name] = value.strip()
                if "transfer-encoding" in headers:
                    return
                size = int(headers.get("content-length", "0"))
                octet = headers.get("content-type") == "application/octet-stream"
                limit = (
                    (
                        TALK_BATCH_BYTES
                        if path.split("?", 1)[0] == "/talk/uplink"
                        else CAPTURE_BATCH_BYTES
                    )
                    if octet
                    else 1024
                )
                if not 0 <= size <= limit:
                    return
                body = await reader.readexactly(size)
                code, payload, extra = self.route(method, path, headers, body)
                common = {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Connection": "close",
                    "Content-Length": str(len(payload)),
                    **extra,
                }
                response = f"HTTP/1.1 {code} Response\r\n" + "".join(
                    f"{k}: {v}\r\n" for k, v in common.items()
                )
                writer.write(response.encode() + b"\r\n" + payload)
                await writer.drain()
        except (
            TimeoutError,
            ValueError,
            UnicodeError,
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass  # Deliberately no access log or request/exception dump.
        finally:
            self.connections -= 1
            self.tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 0.5)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        pending = [
            *self.tasks,
            *([self.control_task] if self.control_task else []),
            *([self.say_task] if self.say_task else []),
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.talk.clear()
