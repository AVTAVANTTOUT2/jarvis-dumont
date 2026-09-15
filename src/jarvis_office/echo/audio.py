"""Remote ingress/egress adapter for VoiceLoop's existing audio contract."""

import asyncio
import secrets
import time
from collections import deque
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
        self.capture_diagnostics: deque[dict[str, Any]] = deque(maxlen=20)
        self.capture_diagnostics_overwritten = 0
        self.capture_diagnostics_stale_events = 0
        self.capture: dict[str, Any] | None = None
        self.server_epoch = ""

    @staticmethod
    def count(capture: dict[str, Any], key: str, amount: int = 1) -> None:
        value = capture[key] + amount
        if value > 2**63 - 1:
            capture["counter_saturated"] = True
        capture[key] = min(value, 2**63 - 1)

    def worker_diagnostic(self, capture: dict[str, Any], timing: object) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if not isinstance(timing, dict):
            capture["diagnostic_incomplete"] = True
            return clean
        worker = capture.setdefault("worker", {})
        for key in (
            "capture_opened",
            "first_block_consumed",
            "last_block_consumed",
            "input_closed",
            "vad_finalized",
            "input_samples",
            "input_blocks",
            "input_audio_s",
            "normalized_samples",
            "utterance_samples",
            "callback_dropped",
            "reconnects",
            "stt_started",
            "stt_finished",
            "speech_start_estimate",
            "speech_end_estimate",
            "vad_delay_s",
            "pre_roll_s",
            "terminal_silence_ms",
            "normalized_rate",
            "vad_frame_samples",
            "rms_capture",
        ):
            if key not in timing:
                continue
            value = timing[key]
            # Compare JSON numbers before any float conversion; the bounds also reject NaN/inf.
            if (type(value) in (int, float) and 0 <= value < 2**63) or (
                value is None
                and key in ("first_block_consumed", "speech_start_estimate", "speech_end_estimate")
            ):
                clean[key] = worker[key] = value
            else:
                worker.pop(key, None)
                capture["diagnostic_incomplete"] = True
        reason = timing.get("segmentation_reason")
        if reason in ("terminal_silence", "max_duration"):
            clean["segmentation_reason"] = capture["segmentation_reason"] = reason
        elif "segmentation_reason" in timing:
            capture["segmentation_reason"] = "UNKNOWN"
        clock = timing.get("input_clock")
        if clock in (
            "server_receive_estimate",
            "ADC_mapped_to_perf_counter_estimate",
            "UNAVAILABLE",
        ):
            clean["input_clock"] = worker["input_clock"] = clock
        elif "input_clock" in timing:
            worker.pop("input_clock", None)
        if len(clean) != len(timing):
            capture["diagnostic_incomplete"] = True
        return clean

    def context_observed(self, outcome: str, chars: int, *, entry_id: str | None = None) -> None:
        capture = self.capture
        if (
            capture is None
            or self.voice is None
            or self.bound is None
            or capture["turn_id"] != self.voice.turn
            or capture["conversation_session"] != self.voice.session
            or capture["echo_connection_session"] != self.bound.id
        ):
            self.capture_diagnostics_stale_events = min(
                2**63 - 1, self.capture_diagnostics_stale_events + 1
            )
            return
        capture.update(
            context_delivery_s=time.perf_counter(),
            context_outcome=outcome,
            context_chars=chars,
            context_entry_id=entry_id,
        )

    def close_ingress_diagnostic(self, capture: dict[str, Any] | None = None) -> None:
        capture = self.capture if capture is None else capture
        if (
            capture is not None
            and capture["ingress_opened_s"] is not None
            and capture["ingress_closed_s"] is None
        ):
            capture["ingress_closed_s"] = time.perf_counter()

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
            self.capture_diagnostics_stale_events = min(
                2**63 - 1, self.capture_diagnostics_stale_events + 1
            )
            return
        if item.get("event") == "listening":
            if self.bound is not None:
                self.token = item.get("data", {}).get("device", {}).get("ingress_token", "")
            data = item.get("data", {})
            capture = (
                self.capture if self.capture and self.capture["turn_id"] == item.get("turn") else {}
            )
            opened_metadata = self.worker_diagnostic(
                capture, {"capture_opened": data.get("opened_at")}
            )
            worker_opened = opened_metadata.get("capture_opened")
            if worker_opened is None:
                item = {
                    **item,
                    "data": {key: value for key, value in data.items() if key != "opened_at"},
                }
            if (
                self.bound is not None
                and self.capture is not None
                and self.capture["turn_id"] == item.get("turn")
            ):
                self.count(self.capture, "worker_listen_events")
                opened = time.perf_counter()
                if self.capture["ingress_opened_s"] is None:
                    self.capture["ingress_opened_s"] = opened
                self.capture["last_ingress_opened_s"] = opened
                self.capture["status"] = "LISTENING"
                self.capture["worker_opened_s"] = worker_opened
                if len(self.capture_diagnostics) > 1 and self.capture["worker_listen_events"] == 1:
                    previous = self.capture_diagnostics[-2]
                    if (
                        all(
                            previous[key] == self.capture[key]
                            for key in (
                                "echo_connection_session",
                                "conversation_session",
                                "generation",
                                "context_generation",
                                "archive_generation",
                            )
                        )
                        and previous["ingress_closed_s"] is not None
                    ):
                        previous["rearmed_s"] = self.capture["ingress_opened_s"]
                        previous["input_closed_interval_s"] = (
                            previous["rearmed_s"] - previous["ingress_closed_s"]
                        )
        elif item.get("event") == "transcribing":
            capture = (
                self.capture if self.capture and self.capture["turn_id"] == item.get("turn") else {}
            )
            data = item.get("data", {})
            timing = self.worker_diagnostic(capture, data.get("timing", {}))
            item = {**item, "data": {**data, "timing": timing}}
            if self.capture is not None and self.capture["turn_id"] == item.get("turn"):
                self.close_ingress_diagnostic()
                self.capture["transcribing_event_s"] = time.perf_counter()
            self.token = ""
        self.voice.audio_event(item)

    def feed(self, session: Session, packet: Packet, received: int) -> None:
        capture = self.capture
        owned = capture is not None and (
            capture["echo_connection_session"],
            capture["stream_id"],
        ) == (session.id, packet.stream)
        if owned:
            assert capture is not None
            self.count(capture, "received_frames")
            self.count(capture, "received_samples", len(packet.pcm) // (2 * packet.channels))
            capture["received_audio_s"] = min(
                2**63 - 1,
                capture["received_audio_s"] + len(packet.pcm) / (2 * packet.channels * packet.rate),
            )
            if capture["first_frame_received_ns"] is None:
                capture["first_frame_received_ns"] = received
            capture["last_frame_received_ns"] = received
        if (
            session is not self.bound
            or not session.alive
            or session.mode == "OFF"
            or not self.token
        ):
            if owned:
                assert capture is not None
                self.count(capture, "ignored_frames")
                if capture["ingress_closed_s"] is not None and not self.token:
                    self.count(capture, "ignored_while_closed_frames")
            session.metrics["suppressed_frames"] = session.metrics.get("suppressed_frames", 0) + 1
            return
        if self.listen_owner != (session.id, packet.stream, self.voice.turn):
            if owned:
                assert capture is not None
                self.count(capture, "ignored_frames")
            return
        try:
            self.worker.feed_remote(self.token, packet.pcm, time.perf_counter())
        except LoopError:
            if owned:
                assert capture is not None
                self.count(capture, "ingress_failed_frames")
            raise
        if owned:
            assert capture is not None
            self.count(capture, "forwarded_frames")
            self.count(capture, "forwarded_samples", len(packet.pcm) // (2 * packet.channels))

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
            capture = {
                "turn_id": turn,
                "echo_connection_session": s.id,
                "server_epoch": self.server_epoch,
                "conversation_session": self.voice.session,
                "generation": s.generation,
                "context_generation": s.context_epoch,
                "archive_generation": self.listen_storage_generation,
                "stream_id": s.up_stream,
                "clock": "controller_perf_counter_absolute_seconds",
                "frame_clock": "server_monotonic_ns",
                "worker_clock": "audio_worker_perf_counter",
                "audio_worker_pid": self.worker.process.pid if self.worker.process else None,
                "listen_requested_s": time.perf_counter(),
                "ingress_opened_s": None,
                "worker_opened_s": None,
                "ingress_closed_s": None,
                "first_frame_received_ns": None,
                "last_frame_received_ns": None,
                "received_frames": 0,
                "worker_listen_events": 0,
                "counter_saturated": False,
                "received_samples": 0,
                "received_audio_s": 0.0,
                "forwarded_frames": 0,
                "forwarded_samples": 0,
                "ignored_frames": 0,
                "ingress_failed_frames": 0,
                "ignored_while_closed_frames": 0,
                "segmentation_reason": "UNKNOWN",
                "rearmed_s": None,
                "status": "REQUESTED",
            }
            self.capture = capture
            if len(self.capture_diagnostics) == self.capture_diagnostics.maxlen:
                self.capture_diagnostics_overwritten = min(
                    2**63 - 1, self.capture_diagnostics_overwritten + 1
                )
            self.capture_diagnostics.append(capture)
            s.current_turn = ""
            try:
                await s.send("audio_start", {"stream_id": s.up_stream})
                result = await self.worker.call(op, turn=turn, timeout=timeout, **args)
                timing = self.worker_diagnostic(capture, result.get("timing", {}))
                clocks = {
                    key: result[key]
                    for key in (
                        "speech_start_estimate",
                        "speech_end_estimate",
                        "vad_finalized",
                        "stt_started",
                        "stt_finished",
                    )
                    if key in result
                }
                clean_clocks = self.worker_diagnostic(capture, clocks)
                result = {
                    **result,
                    "timing": timing,
                    **{key: clean_clocks.get(key) for key in clocks},
                }
                capture.update(
                    result_received_s=time.perf_counter(),
                    produced_chars=len(result.get("text", "")),
                    accepted=bool(result.get("accepted")),
                    status="CANCELLED"
                    if result.get("cancelled")
                    else "SILENCE"
                    if result.get("silence")
                    else "RESULT",
                )
                return result
            except asyncio.CancelledError:
                capture["status"] = "CANCELLED"
                raise
            except Exception:
                capture["status"] = "FAILED"
                raise
            finally:
                self.close_ingress_diagnostic(capture)
                capture["listen_finished_s"] = time.perf_counter()
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
        self.close_ingress_diagnostic()
        self.token = ""
        self.listen_owner = None
        if self.egress is not None:
            await self.egress.abort()
        return await self.worker.abort(turn)

    async def close(self) -> None:
        self.close_ingress_diagnostic()
        self.token = ""
        if self.egress is not None:
            await self.egress.abort()
        await self.worker.close()
