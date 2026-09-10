"""Tiny loopback control page, same-origin bootstrap and HttpOnly local session cookie.

No action or transcript on GET. POST APIs require exact Host + Origin, same-origin
Fetch Metadata, a custom header and a session cookie (except bootstrap). No CORS.
The static unauthenticated page contains no personal text, secret or control token.
"""

import asyncio
import contextlib
import hmac
import json
import secrets
from pathlib import Path

from jarvis_office.audio_client import LoopError
from jarvis_office.voice import VoiceLoop

PAGE = Path(__file__).with_name("control.html").read_text(encoding="utf-8")


class LocalUI:
    def __init__(self, voice: VoiceLoop, port: int) -> None:
        self.voice, self.port = voice, port
        self.host = f"127.0.0.1:{port}"
        self.origin = "http://" + self.host
        self.cookie_name = "jarvis_office_" + str(port)
        self.token = secrets.token_urlsafe(32)
        self.server: asyncio.Server | None = None
        self.connections = 0
        self.tasks: set[asyncio.Task[None]] = set()
        self.controls: asyncio.Queue[str] = asyncio.Queue(4)
        self.control_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            self.server = await asyncio.start_server(
                self.handle, "127.0.0.1", self.port, limit=8192
            )
        except OSError:
            raise LoopError("loopback_port_unavailable") from None
        self.control_task = asyncio.create_task(self._control())

    async def _control(self) -> None:
        while True:
            action = await self.controls.get()
            try:
                await self.voice.control(action)
            except Exception as exc:
                self.voice.error = str(exc) if isinstance(exc, LoopError) else "control_failed"
                self.voice.state = "error"

    def route(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes, dict[str, str]]:
        if headers.get("host") != self.host:
            return 403, b"{}", {}
        if method == "GET" and path == "/":
            nonce = secrets.token_urlsafe(24)
            return (
                200,
                PAGE.replace("NONCE", nonce).encode(),
                {
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Security-Policy": (
                        f"default-src 'none'; script-src 'nonce-{nonce}'; "
                        f"style-src 'nonce-{nonce}'; connect-src 'self'; "
                        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                    ),
                },
            )
        if (
            method != "POST"
            or headers.get("origin") != self.origin
            or headers.get("sec-fetch-site") != "same-origin"
            or headers.get("x-jarvis-local") != "1"
            or headers.get("content-type") != "application/json"
        ):
            return 403, b"{}", {}
        if path == "/bootstrap":
            return (
                200,
                b"{}",
                {
                    "Set-Cookie": (
                        f"{self.cookie_name}={self.token}; HttpOnly; SameSite=Strict; Path=/"
                    )
                },
            )
        cookies = [c.strip() for c in headers.get("cookie", "").split(";")]
        if not any(hmac.compare_digest(c, self.cookie_name + "=" + self.token) for c in cookies):
            return 403, b"{}", {}
        if path == "/snapshot":
            payload = json.dumps(
                self.voice.snapshot(), ensure_ascii=False, allow_nan=False
            ).encode()
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
                self.controls.put_nowait(value["action"])
                return 202, b"{}", {}
            except (ValueError, TypeError, asyncio.QueueFull):
                return 400, b"{}", {}
        return 404, b"{}", {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        self.connections += 1
        try:
            async with asyncio.timeout(3):
                if self.connections > 8:
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
                if not 0 <= size <= 1024:
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
        pending = [*self.tasks, *([self.control_task] if self.control_task else [])]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.token = ""
