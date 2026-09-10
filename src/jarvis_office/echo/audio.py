"""Remote ingress/egress adapter for VoiceLoop's existing audio contract."""

import asyncio
import secrets
import time
from pathlib import Path
from typing import Any

from jarvis_office.audio_client import AudioClient, LoopError
from jarvis_office.config import Config

from .gateway import Session, Settings
from .protocol import Packet, Sequence


class RemoteEchoEgress:
    def __init__(
        self, session: Session, settings: Settings, turn: str, rate: int, *, audition: bool = False
    ) -> None:
        import soxr

        self.session, self.settings, self.turn = session, settings, turn
        self.audition = audition
        self.identity = session.id
        self.uplink = session.up_stream
        self.stream = secrets.randbits(63) or 1
        self.rate = rate
        self.resampler = soxr.ResampleStream(rate, 48000, 1, dtype="float32", quality="HQ")
        self.pending = bytearray()
        self.sequence = self.segments = self.sent_bytes = 0
        self.start_at: float | None = None
        self.first_send: float | None = None
        self.ended = self.closed = False
        self.input_ended = False
        self.source_bytes = 0
        self.source: asyncio.Queue[tuple[bytes, float] | None] = asyncio.Queue(25)
        self.sender: asyncio.Task[None] | None = None

    def check(self, turn: str) -> None:
        s = self.session
        if (
            self.closed
            or turn != self.turn
            or not s.alive
            or (s.mode == "OFF" and not self.audition)
            or s.id != self.identity
            or s.current_turn != turn
            or s.down_stream != self.stream
            or s.up_stream != self.uplink
            or s.audio is None
        ):
            raise LoopError("stale_remote_turn")

    async def start(self) -> dict[str, Any]:
        s = self.session
        if not s.alive or not s.audio or (s.mode == "OFF" and not self.audition) or s.down_stream:
            raise LoopError("remote_output_unavailable")
        s.current_turn, s.down_stream = self.turn, self.stream
        s.playback_ready.clear()
        s.playback_drained.clear()
        await s.send(
            "speaking_started",
            {
                "stream_id": self.stream,
                "turn_id": self.turn,
                "rate": 48000,
                "channels": 1,
                "prefill_ms": self.settings.playback_prefill_ms,
            },
        )
        async with asyncio.timeout(3):
            await s.playback_ready.wait()
        self.check(self.turn)
        self.sender = asyncio.create_task(self._pump())
        return {"device": {"name": "Echo", "origin": f"echo:{s.device}:{s.id}"}}

    async def _output(self, samples: Any, ready: float) -> None:
        import numpy as np

        pcm = np.rint(np.clip(samples, -1, 32767 / 32768) * 32768).astype("<i2").tobytes()
        # Only a fractional frame is retained; each completed frame is sent with backpressure.
        for offset in range(0, len(pcm), 1920):
            self.pending.extend(pcm[offset : offset + 1920])
            while len(self.pending) >= 1920:
                payload = bytes(self.pending[:1920])
                del self.pending[:1920]
                if "playback_envelope_v1" in self.session.capabilities:
                    # Side-channel only: bounded work, no await or influence on PCM scheduling.
                    values = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768
                    energy = min(1.0, float(np.sqrt(np.mean(values * values))) * 6)
                    self.session.envelope_queue.append(
                        (self.stream, self.turn, self.sequence * 960, round(energy, 4))
                    )
                self.check(self.turn)
                now = time.perf_counter()
                if self.start_at is None:
                    self.start_at = now
                elif now - (self.start_at + self.sequence * 0.02) > 0.02:
                    # A producer gap is elapsed silence, never a debt to repay in a PCM burst.
                    self.start_at = now - self.sequence * 0.02
                    self.session.metrics["pacing_rebases"] = (
                        self.session.metrics.get("pacing_rebases", 0) + 1
                    )
                await asyncio.sleep(
                    max(0, self.start_at + self.sequence * 0.02 - time.perf_counter())
                )
                self.check(self.turn)
                sent = time.perf_counter()
                m4 = time.monotonic_ns()
                # Ready and send are both measured on this Mac, in the same monotonic domain.
                m3 = m4 - max(0, round((sent - ready) * 1e9))
                packet = Packet(self.stream, self.sequence, m3, m4, 48000, 1, payload)
                assert self.session.audio is not None
                async with asyncio.timeout(self.settings.max_audio_ms / 1000):
                    await self.session.audio.send(packet.encode())
                self.first_send = sent if self.first_send is None else self.first_send
                self.session.observe("pcm_ready_to_remote_send_ms", (sent - ready) * 1000)
                self.session.observe("remote_send_call_ms", (time.perf_counter() - sent) * 1000)
                self.sequence += 1
                self.sent_bytes += len(payload)
                self.session.metrics["downlink_frames"] = self.sequence

    async def feed(self, turn: str, pcm: bytes) -> dict[str, Any]:
        self.check(turn)
        if self.input_ended or len(pcm) > 24000 or len(pcm) % 2:
            raise LoopError("invalid_remote_pcm")
        if len(pcm) > self.progress(turn)["free_source_bytes"]:
            raise LoopError("remote_pcm_backpressure")
        if pcm:
            self.source_bytes += len(pcm)
            self.source.put_nowait((pcm, time.perf_counter()))
            self.session.metrics["source_queue_peak_bytes"] = max(
                self.session.metrics.get("source_queue_peak_bytes", 0), self.source_bytes
            )
        return self.progress(turn)

    async def _pump(self) -> None:
        import numpy as np

        # The existing 24 kB credit includes in-flight source PCM (500 ms at Qwen's 24 kHz).
        # Generation can start the next segment while this bounded sender drains the previous one.
        step = self.rate * 2 // 50
        while (item := await self.source.get()) is not None:
            pcm, ready = item
            for offset in range(0, len(pcm), step):
                self.check(self.turn)
                chunk = pcm[offset : offset + step]
                mono = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768
                await self._output(self.resampler.resample_chunk(mono), ready)
                self.source_bytes -= len(chunk)
        await self._output(
            self.resampler.resample_chunk(np.empty(0, np.float32), last=True), time.perf_counter()
        )
        if self.pending:
            padding = (1920 - len(self.pending)) // 2
            await self._output(np.zeros(padding, np.float32), time.perf_counter())
        if self.sequence == 0:
            raise LoopError("empty_remote_pcm")
        self.ended = True
        await self.session.send(
            "audio_end", {"stream_id": self.stream, "last_sequence": self.sequence - 1}
        )

    async def finish(self, turn: str) -> dict[str, Any]:
        self.check(turn)
        self.input_ended = True
        await self.source.put(None)
        assert self.sender is not None
        await self.sender
        return self.progress(turn)

    def progress(self, turn: str) -> dict[str, Any]:
        self.check(turn)
        if self.sender is not None and self.sender.done():
            self.sender.result()
        self.session.metrics["source_queue_bytes"] = self.source_bytes
        done = self.ended and self.session.playback_drained.is_set()
        return {
            "completed_segments": list(range(1, self.segments + 1)) if done else [],
            "done": done,
            "playing": self.sequence > 0,
            "free_source_bytes": 0 if self.source.full() else 24000 - self.source_bytes,
            "first_remote_send": self.first_send,
            "remote_sent_bytes": self.sent_bytes,
            "stream_format": {"rate": 48000, "channels": 1, "format": "PCM16"},
        }

    async def drained(self, turn: str) -> dict[str, Any]:
        result = self.progress(turn)
        if not result["done"]:
            raise LoopError("remote_output_not_drained")
        await self.session.send("speaking_finished", {"stream_id": self.stream})
        self.session.metrics["playback_completed_streams"] = (
            self.session.metrics.get("playback_completed_streams", 0) + 1
        )
        self.session.down_stream = 0
        self.closed = True
        return result

    async def abort(self) -> None:
        self.closed = True
        if self.sender is not None:
            self.sender.cancel()
            await asyncio.gather(self.sender, return_exceptions=True)
        while not self.source.empty():
            self.source.get_nowait()
        self.source_bytes = 0
        self.pending.clear()
        if self.session.id == self.identity and self.session.down_stream == self.stream:
            self.session.down_stream = 0
        self.session.envelope_queue.clear()


class RemoteEchoAudio:
    """Reuse the sandboxed AudioClient/STT worker and route only playback to this Echo."""

    def __init__(self, config: Config, path: Path, settings: Settings) -> None:
        self.settings = settings
        self.worker = AudioClient(config, path, "", self.event, remote_ingress=True)
        self.voice: Any = None
        self.bound: Session | None = None
        self.token = ""
        self.egress: RemoteEchoEgress | None = None
        self.listen_owner: tuple[str, int, str] | None = None
        self.listen_context_epoch = 0
        self.listen_storage_generation = 0

    @property
    def session(self) -> str:
        return self.worker.session

    @session.setter
    def session(self, value: str) -> None:
        self.worker.session = value

    @property
    def ready(self) -> dict[str, Any]:
        return self.worker.ready

    async def start(self) -> None:
        await self.worker.start()

    def event(self, item: dict[str, Any]) -> None:
        if (
            self.voice is None
            or item.get("session") != self.voice.session
            or item.get("turn") != self.voice.turn
        ):
            return
        if item.get("event") == "listening" and self.bound is not None:
            self.token = item.get("data", {}).get("device", {}).get("ingress_token", "")
        elif item.get("event") == "transcribing":
            self.token = ""
        self.voice.audio_event(item)

    def feed(self, session: Session, packet: Packet, received: int) -> None:
        if (
            session is not self.bound
            or not session.alive
            or session.mode == "OFF"
            or not self.token
        ):
            session.metrics["suppressed_frames"] = session.metrics.get("suppressed_frames", 0) + 1
            return
        if self.listen_owner != (session.id, packet.stream, self.voice.turn):
            return
        self.worker.feed_remote(self.token, packet.pcm, time.perf_counter())

    async def call(
        self, op: str, *, turn: str = "", timeout: float = 5, **args: Any
    ) -> dict[str, Any]:
        if op in {"clock", "reset"}:
            return await self.worker.call(op, turn=turn, timeout=timeout, **args)
        s = self.bound
        if s is None or not s.alive or s.mode == "OFF" or not s.audio:
            raise LoopError("remote_session_unavailable")
        if op == "check":
            return {"device": {"name": "Echo", "origin": f"echo:{s.device}:{s.id}"}}
        if op == "listen":
            self.token = ""
            if s.up_stream:
                s.retired_streams.append((s.up_stream, time.monotonic() + 0.2))
            s.up_stream = secrets.randbits(63) or 1
            s.up_sequence = Sequence()
            s.first_capture = s.first_receive = 0
            self.listen_owner = (s.id, s.up_stream, turn)
            self.listen_context_epoch = s.context_epoch
            self.listen_storage_generation = (
                self.voice.archive_generation() if self.voice.archive_generation else 0
            )
            s.current_turn = ""
            await s.send("audio_start", {"stream_id": s.up_stream})
            try:
                return await self.worker.call(op, turn=turn, timeout=timeout, **args)
            finally:
                self.token = ""
        if self.listen_owner != (s.id, s.up_stream, turn):
            raise LoopError("stale_remote_turn")
        if op == "begin":
            self.token = ""
            self.egress = RemoteEchoEgress(s, self.settings, turn, args["rate"])
            await s.send("turn_started", {"turn_id": turn})
            await s.send("transcript", {"turn_id": turn, "text": self.voice.accepted})
            return await self.egress.start()
        output = self.egress
        if output is None:
            raise LoopError("remote_output_not_started")
        if op == "progress":
            return output.progress(turn)
        if op == "mark":
            output.check(turn)
            if args["segment"] != output.segments + 1:
                raise LoopError("remote_segment_sequence")
            output.segments += 1
            return output.progress(turn)
        if op == "finish":
            return await output.finish(turn)
        if op == "drained":
            return await output.drained(turn)
        raise LoopError("unknown_remote_audio_command")

    async def write_pcm(self, turn: str, pcm: bytes) -> dict[str, Any]:
        if self.egress is None:
            raise LoopError("remote_output_not_started")
        return await self.egress.feed(turn, pcm)

    async def abort(self, turn: str) -> dict[str, Any]:
        self.token = ""
        self.listen_owner = None
        if self.egress is not None:
            await self.egress.abort()
        return await self.worker.abort(turn)

    async def close(self) -> None:
        self.token = ""
        if self.egress is not None:
            await self.egress.abort()
        await self.worker.close()
