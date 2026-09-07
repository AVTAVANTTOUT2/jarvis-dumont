"""Owned persistent worker, one request at a time, no network client.

Protocol v1: stdin = one UTF-8 JSON request per line (<=64 KiB), with a
positive integer id and text. stdout = >4sII (tag, request id, byte length),
then <=256 KiB payload. LOAD/RDY! use id 0 and JSON; PCM! uses S16LE mono at
the rate in RDY!; END!/ERR! finish exactly one request. Empty PCM is ignored.
Only RDY! after nonempty warmup means synthesis-ready. No implicit tail buffer.
Cancellation stops delivery, then drains that id under the same lock; a lost
boundary/timeout kills only the owned worker. The next request starts a new one.
"""

import asyncio
import contextlib
import json
import os
import struct
import sys
import tempfile
import time
import wave
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any, BinaryIO

from jarvis_office.assets import private_root
from jarvis_office.config import TTS, Config

HEADER = struct.Struct(">4sII")
MAX_FRAME = 256 * 1024
MAX_REQUEST = 64 * 1024
MAX_TEXT = 4000
TAGS = {b"LOAD", b"RDY!", b"PCM!", b"END!", b"ERR!"}


class TTSError(Exception):
    pass


def write_frame(out: BinaryIO, tag: bytes, request_id: int, payload: bytes) -> None:
    if tag not in TAGS or len(payload) > MAX_FRAME:
        raise TTSError("invalid_frame")
    out.write(HEADER.pack(tag, request_id, len(payload)))
    out.write(payload)
    out.flush()


def json_payload(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise TTSError("invalid_frame_metadata") from None


def worker_environment(cache: Path) -> dict[str, str]:
    # Allowlist: no API keys, PYTHONPATH, V1 paths or inherited model caches.
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("home", "tmp", "hf", "xdg"):
        (cache / name).mkdir(exist_ok=True, mode=0o700)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cache / "home"),
        "TMPDIR": str(cache / "tmp"),
        "HF_HOME": str(cache / "hf"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONUNBUFFERED": "1",
    }


class TTSClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        settings: TTS | None = None,
        cache: Path | None = None,
    ) -> None:
        self.command = list(command)
        self.settings = settings if settings is not None else TTS()
        self.cache = cache if cache is not None else private_root() / "cache/tts"
        self.process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: asyncio.Task[tuple[bytes, int, bytes]] | None = None
        self._stderr = bytearray()
        self.stderr_bytes = 0
        self.closed = False
        self.ready: dict[str, Any] = {}
        self.loaded: dict[str, Any] = {}
        self.last_metrics: dict[str, Any] = {}
        self.last_drain_seconds = 0.0
        self.restarts = 0
        self._request_id = 0

    @classmethod
    def for_config(cls, config_path: Path, settings: TTS) -> "TTSClient":
        if settings.python is None or not settings.python.is_file():
            raise TTSError("tts_python_not_configured")
        command = [
            str(settings.python),
            "-I",
            "-B",
            "-m",
            "jarvis_office.tts_worker",
            str(config_path),
        ]
        if sys.platform == "darwin":
            sandbox = Path("/usr/bin/sandbox-exec")
            if not sandbox.is_file():
                raise TTSError("network_sandbox_unavailable")
            command = [str(sandbox), "-p", "(version 1)(allow default)(deny network*)", *command]
        return cls(command, settings=settings)

    async def _stderr_loop(self, reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(4096):
            self.stderr_bytes += len(chunk)
            self._stderr.extend(chunk)
            del self._stderr[:-32_768]

    async def _read_frame(self) -> tuple[bytes, int, bytes]:
        assert self.process is not None and self.process.stdout is not None
        try:
            timeout = (
                self.settings.fragment_timeout if self.ready else self.settings.startup_timeout
            )
            async with asyncio.timeout(timeout):
                tag, request_id, size = HEADER.unpack(
                    await self.process.stdout.readexactly(HEADER.size)
                )
                if tag not in TAGS or size > MAX_FRAME:
                    raise TTSError("invalid_frame_header")
                return tag, request_id, await self.process.stdout.readexactly(size)
        except asyncio.IncompleteReadError:
            raise TTSError("worker_stream_truncated") from None
        except TimeoutError:
            raise TTSError("worker_fragment_timeout") from None

    async def _next_frame(self) -> tuple[bytes, int, bytes]:
        if self._pending is None:
            self._pending = asyncio.create_task(self._read_frame())
        # Preserve a partially consumed frame when the consumer is cancelled.
        task = self._pending
        result = await asyncio.shield(task)
        self._pending = None
        return result

    async def _start(self) -> None:
        if self.closed:
            raise TTSError("worker_client_closed")
        if self.process is not None and self.process.returncode is None and self.ready:
            return
        await self._stop()
        self.ready, self.loaded = {}, {}
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=worker_environment(self.cache),
            cwd=self.cache,
            limit=MAX_FRAME + HEADER.size,
        )
        assert self.process.stderr is not None
        self._stderr_task = asyncio.create_task(self._stderr_loop(self.process.stderr))
        try:
            async with asyncio.timeout(self.settings.startup_timeout):
                tag, request_id, payload = await self._next_frame()
                if tag != b"LOAD" or request_id != 0:
                    raise TTSError("worker_load_failed")
                self.loaded = json_payload(payload)
                tag, request_id, payload = await self._next_frame()
                if tag != b"RDY!" or request_id != 0:
                    raise TTSError("worker_warmup_failed")
                self.ready = json_payload(payload)
                rate = self.ready.get("sample_rate")
                warmup = self.ready.get("warmup_pcm_bytes")
                if (
                    type(rate) is not int
                    or not 8000 <= rate <= 192000
                    or self.ready.get("channels") != 1
                    or type(warmup) is not int
                    or warmup <= 0
                ):
                    raise TTSError("worker_invalid_ready")
        except TimeoutError:
            await self._stop()
            raise TTSError("worker_startup_timeout") from None
        except BaseException:
            await self._stop()
            raise

    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def _drain(self, request_id: int) -> None:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.settings.drain_timeout):
                while True:
                    tag, frame_id, _ = await self._next_frame()
                    if frame_id != request_id or tag not in {b"PCM!", b"END!", b"ERR!"}:
                        raise TTSError("cancelled_stream_lost_boundary")
                    if tag in {b"END!", b"ERR!"}:
                        if tag == b"ERR!":
                            self.restarts += 1
                            await self._stop()
                        break
        except (TTSError, TimeoutError, OSError):
            self.restarts += 1
            await self._stop()
        finally:
            self.last_drain_seconds = time.perf_counter() - started

    async def stream(
        self, text: str, *, cancel: asyncio.Event | None = None
    ) -> AsyncGenerator[bytes, None]:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise TTSError("invalid_tts_text")
        async with self._lock:
            await self._start()
            assert self.process is not None and self.process.stdin is not None
            self._request_id += 1
            request_id = self._request_id
            request = (
                json.dumps({"id": request_id, "text": text}, ensure_ascii=True).encode() + b"\n"
            )
            if len(request) > MAX_REQUEST:
                raise TTSError("request_too_large")
            complete = False
            delivered_bytes = 0
            started = time.perf_counter()
            first_delivery: float | None = None
            self.last_metrics = {}
            cancel_wait = asyncio.create_task(cancel.wait()) if cancel is not None else None
            try:
                self.process.stdin.write(request)
                await self.process.stdin.drain()
                while True:
                    if cancel is not None and cancel.is_set():
                        break
                    if cancel_wait is not None:
                        assert cancel is not None
                        if self._pending is None:
                            self._pending = asyncio.create_task(self._read_frame())
                        await asyncio.wait(
                            {self._pending, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if cancel.is_set():
                            break
                    tag, frame_id, payload = await self._next_frame()
                    if frame_id != request_id:
                        raise TTSError("request_id_mismatch")
                    if tag == b"PCM!":
                        if len(payload) % 2:
                            raise TTSError("invalid_pcm_alignment")
                        if payload:
                            if cancel is not None and cancel.is_set():
                                break
                            if first_delivery is None:
                                first_delivery = time.perf_counter() - started
                            delivered_bytes += len(payload)
                            yield payload
                    elif tag == b"END!":
                        complete = True
                        if first_delivery is None:
                            raise TTSError("empty_synthesis")
                        self.last_metrics = json_payload(payload)
                        if self.last_metrics.get("pcm_bytes") != delivered_bytes:
                            raise TTSError("pcm_byte_count_mismatch")
                        self.last_metrics["first_pcm_delivered_s"] = first_delivery
                        self.last_metrics["consumer_total_s"] = time.perf_counter() - started
                        break
                    elif tag == b"ERR!":
                        complete = True
                        raise TTSError("worker_synthesis_failed")
                    else:
                        raise TTSError("unexpected_frame")
            except (TTSError, OSError):
                await self._stop()
                complete = True
                raise
            finally:
                if cancel_wait is not None:
                    cancel_wait.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_wait
                if not complete and self.process is not None:
                    await asyncio.shield(self._drain(request_id))

    async def _stop(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pending
            self._pending = None
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await asyncio.wait_for(process.wait(), 3)
            if process.stdin is not None:
                process.stdin.close()
            if self._stderr_task is not None:
                try:
                    await asyncio.wait_for(self._stderr_task, 3)
                except TimeoutError:
                    self._stderr_task.cancel()
                    await asyncio.gather(self._stderr_task, return_exceptions=True)
                self._stderr_task = None
        self.ready = {}

    async def close(self) -> None:
        self.closed = True
        if self.process is not None and not self._lock.locked() and self.process.stdin is not None:
            self.process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 3)
        await self._stop()


async def synthesize_to_wav(
    config: Config, config_path: Path, text: str, output: Path, *, repeats: int = 1
) -> dict[str, Any]:
    if not 1 <= repeats <= 5:
        raise TTSError("invalid_repeat_count")
    output = output.expanduser().absolute()
    if output.suffix.lower() != ".wav" or output.exists() or output.is_symlink():
        raise TTSError("output_must_be_new_wav")
    for asset in (config.assets.tts_model, config.assets.voice_profile):
        if asset is not None and output.resolve().is_relative_to(asset.resolve()):
            raise TTSError("output_overlaps_assets")
    if not text.strip() or len(text) > MAX_TEXT:
        raise TTSError("invalid_tts_text")
    client = TTSClient.for_config(config_path.absolute(), config.tts)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.perf_counter()
    runs = []
    try:
        await client.start()
        startup = time.perf_counter() - started
        ready = dict(client.ready)
        loaded = dict(client.loaded)
        with tempfile.TemporaryDirectory(prefix=".tts-", dir=output.parent) as directory:
            staged = Path(directory) / "speech.wav"
            for repeat in range(repeats):
                first_output: float | None = None
                with wave.open(str(staged), "wb") as wav:
                    wav.setparams((1, 2, ready["sample_rate"], 0, "NONE", "not compressed"))
                    async with contextlib.aclosing(client.stream(text)) as stream:
                        async for pcm in stream:
                            if first_output is None:
                                first_output = time.perf_counter() - started
                            wav.writeframesraw(pcm)
                runs.append(
                    {
                        **client.last_metrics,
                        "repeat": repeat + 1,
                        "state": "post_warmup_first" if repeat == 0 else "warm_repeat",
                        "first_output_since_command_s": first_output,
                    }
                )
            staged.chmod(0o600)
            # Atomic no-overwrite publication within the owned output directory.
            # This transient link is to our temporary WAV, never to V1 or a model.
            os.link(staged, output)
        return {
            "status": "PASS",
            "exit_code": 0,
            "model_loaded": "PASS",
            "warmup": "PASS",
            "voice_identity": "NOT_RUN",
            "playback": "NOT_RUN",
            "network": "DENIED_BY_WORKER_POLICY",
            "startup_wall_s": startup,
            "loaded": loaded,
            "ready": ready,
            "runs": runs,
            "stderr_bytes": client.stderr_bytes,
            "output": "explicit_local_wav",
            "text_chars": len(text),
        }
    finally:
        await client.close()
