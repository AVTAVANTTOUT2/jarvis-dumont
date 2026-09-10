"""Owned audio worker RPC. Bounded JSON lines; PCM is <=24 KiB before base64.

    request: id, session, turn, op, args
    reply: id, session, turn, result OR error (constant reason)
    event: event, session, turn, data

Identifiers are checked before resolving futures. Only one capture/inference and
one output exist. The parent closes the owned process on an uncertain boundary.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis_office.assets import private_root
from jarvis_office.config import Config
from jarvis_office.tts import worker_environment

WIRE_LIMIT = 96 * 1024


class LoopError(Exception):
    pass


def source_signature() -> str:
    digest = hashlib.sha256()
    for file in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


class AudioClient:
    def __init__(
        self,
        config: Config,
        path: Path,
        session: str,
        event: Callable[[dict[str, Any]], None],
        *,
        remote_ingress: bool = False,
    ) -> None:
        self.config, self.path, self.session, self.event = config, path, session, event
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, tuple[str, str, asyncio.Future[dict[str, Any]]]] = {}
        self.sequence = 0
        self.reader: asyncio.Task[None] | None = None
        self.errors: asyncio.Task[None] | None = None
        self.write_lock = asyncio.Lock()
        self.stderr = bytearray()
        self.stderr_bytes = 0
        self.ready: dict[str, Any] = {}
        self.owns_group = False
        self.remote_ingress = remote_ingress
        self.pcm_writer: int | None = None

    async def start(self) -> None:
        if self.process is not None:
            return
        python = self.config.speech.python
        if python is None or not python.is_file():
            raise LoopError("speech_python_not_configured")
        command = [str(python), "-I", "-B", "-m", "jarvis_office.audio_worker", str(self.path)]
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise LoopError("audio_network_sandbox_unavailable")
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)(deny network*)",
            *command,
        ]
        cache = private_root() / "cache/voice-audio"
        env = worker_environment(cache)
        env["JARVIS_OFFICE_DATA"] = str(private_root())
        read_fd = None
        if self.remote_ingress:
            read_fd, self.pcm_writer = os.pipe()
            os.set_blocking(self.pcm_writer, False)
            command.append(str(read_fd))
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cache,
                limit=WIRE_LIMIT + 1,
                start_new_session=True,
                pass_fds=() if read_fd is None else (read_fd,),
            )
        except BaseException:
            if self.pcm_writer is not None:
                os.close(self.pcm_writer)
                self.pcm_writer = None
            raise
        finally:
            if read_fd is not None:
                os.close(read_fd)
        self.owns_group = True
        self.reader = asyncio.create_task(self._read())
        self.errors = asyncio.create_task(self._stderr())
        try:
            self.ready = await self.call("start", timeout=180)
            if self.ready.get("code_signature") != source_signature():
                raise LoopError("audio_worker_code_mismatch")
        except BaseException:
            await self.close()
            raise

    async def _stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while data := await self.process.stderr.read(4096):
            self.stderr_bytes += len(data)
            self.stderr.extend(data)
            del self.stderr[:-32768]

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                raw = await self.process.stdout.readline()
                if not raw or len(raw) > WIRE_LIMIT or not raw.endswith(b"\n"):
                    raise LoopError("audio_worker_truncated")
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise LoopError("audio_worker_protocol")
                if "event" in item:
                    if item.get("session") == self.session:
                        self.event(item)
                    continue
                identifier = item.get("id")
                if type(identifier) is not int:
                    raise LoopError("audio_worker_protocol")
                request = self.pending.get(identifier)
                if request is None:
                    continue  # Explicitly abandoned old request, never a new turn's result.
                session, turn, future = request
                if (item.get("session"), item.get("turn")) != (session, turn):
                    raise LoopError("audio_worker_id_mismatch")
                if not future.done():
                    if "error" in item:
                        reason = item["error"]
                        # Worker exception text must never reach UI/logs.
                        if not isinstance(reason, str) or not reason.replace("_", "").isalnum():
                            reason = "audio_worker_failed"
                        future.set_exception(LoopError(reason[:80]))
                    elif isinstance(item.get("result"), dict):
                        future.set_result(item["result"])
                    else:
                        raise LoopError("audio_worker_protocol")
        except asyncio.CancelledError:
            raise
        except Exception:
            for _, _, future in self.pending.values():
                if not future.done():
                    future.set_exception(LoopError("audio_worker_dead_or_invalid"))

    async def call(
        self, op: str, *, turn: str = "", timeout: float = 5, **arguments: Any
    ) -> dict[str, Any]:
        if self.process is None or self.process.returncode is not None:
            raise LoopError("audio_worker_not_running")
        if len(self.pending) >= 8:
            raise LoopError("audio_command_queue_full")
        self.sequence += 1
        identifier = self.sequence
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identifier] = (self.session, turn, future)
        raw = (
            json.dumps(
                {
                    "id": identifier,
                    "session": self.session,
                    "turn": turn,
                    "op": op,
                    "args": arguments,
                },
                allow_nan=False,
            ).encode()
            + b"\n"
        )
        try:
            if len(raw) > WIRE_LIMIT:
                raise LoopError("audio_command_too_large")
            async with asyncio.timeout(timeout):
                async with self.write_lock:
                    assert self.process.stdin is not None
                    self.process.stdin.write(raw)
                    await self.process.stdin.drain()
                return await future
        except TimeoutError:
            await self.close()
            raise LoopError("audio_worker_timeout") from None
        finally:
            self.pending.pop(identifier, None)

    async def abort(self, turn: str) -> dict[str, Any]:
        if self.process is None:
            return {}
        try:
            result = await self.call("abort", turn=turn, timeout=3)
            if result.get("inference_busy") or result.get("capture_busy"):
                await self.close()  # Cannot interrupt CT2 safely; recovery is a new worker.
            return result
        except LoopError:
            await self.close()
            return {}

    def feed_remote(self, token: str, pcm: bytes, received: float) -> None:
        from jarvis_office.audio_ingress import PIPE_HEADER, REMOTE_RATE

        if self.pcm_writer is None or len(pcm) != REMOTE_RATE * 2 // 50:
            raise LoopError("remote_ingress_unavailable")
        try:
            data = PIPE_HEADER.pack(bytes.fromhex(token), received, len(pcm)) + pcm
            if os.write(self.pcm_writer, data) != len(data):
                raise OSError("partial_pipe_write")
        except (OSError, ValueError):
            os.close(self.pcm_writer)
            self.pcm_writer = None
            raise LoopError("remote_ingress_overrun") from None

    async def write_pcm(self, turn: str, pcm: bytes) -> dict[str, Any]:
        return await self.call("pcm", turn=turn, pcm=base64.b64encode(pcm).decode())

    async def close(self) -> None:
        if self.pcm_writer is not None:
            os.close(self.pcm_writer)
            self.pcm_writer = None
        process, self.process = self.process, None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if self.owns_group:
                # A fresh session belongs solely to this worker and its preflight
                # helpers. Never signal the controller's or any V1 process group.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        if self.owns_group:
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    await asyncio.wait_for(process.wait(), 2)
            if self.owns_group:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                self.owns_group = False
        for task in (self.reader, self.errors):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (self.reader, self.errors) if t), return_exceptions=True)
        for _, _, future in self.pending.values():
            if not future.done():
                future.set_exception(LoopError("audio_worker_closed"))
        self.ready = {}
