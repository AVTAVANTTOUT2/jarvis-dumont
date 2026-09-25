"""Phone-only PCM for the local web HUD. STT/TTS stay on the Mac; PortAudio does not."""

from __future__ import annotations

import base64
import time
from typing import Any

from jarvis_office.audio_client import AudioClient, LoopError
from jarvis_office.audio_ingress import REMOTE_RATE

FRAME_BYTES = REMOTE_RATE * 2 // 50
CAPTURE_BATCH_BYTES = FRAME_BYTES * 10
SOURCE_CREDIT = 24000
SOURCE_CAPACITY = 48000
PHONE_DEVICE = {
    "name": "Téléphone",
    "sample_rate": REMOTE_RATE,
    "channels": 1,
    "clock_basis": "phone_audio_context",
}


class WebSink:
    """Bounded RAM playback queue. The phone pulls PCM; the Mac never opens a DAC."""

    def __init__(self) -> None:
        self.turn = ""
        self.rate = 0
        self.pending = bytearray()
        self.produced = 0
        self.pulled = 0
        self.acked = 0
        self.segments = 0
        self.ended = False
        self.closed = False
        self.first_pull: float | None = None

    def abort(self) -> None:
        self.pending.clear()
        self.turn = ""
        self.rate = 0
        self.produced = self.pulled = self.acked = self.segments = 0
        self.ended = False
        self.closed = True
        self.first_pull = None

    def _check(self, turn: str) -> None:
        if self.closed or not self.turn or turn != self.turn:
            raise LoopError("stale_phone_turn")

    def begin(self, turn: str, rate: int) -> dict[str, Any]:
        if not turn or type(rate) is not int or not 8000 <= rate <= 48000:
            raise LoopError("invalid_phone_rate")
        self.abort()
        self.closed = False
        self.ended = False
        self.turn = turn
        self.rate = rate
        return {"device": {**PHONE_DEVICE, "sample_rate": rate}}

    def feed(self, turn: str, pcm: bytes) -> dict[str, Any]:
        self._check(turn)
        if self.ended or len(pcm) > SOURCE_CREDIT or len(pcm) % 2:
            raise LoopError("invalid_remote_pcm")
        if len(pcm) > SOURCE_CAPACITY - len(self.pending):
            raise LoopError("remote_pcm_backpressure")
        if pcm:
            self.pending.extend(pcm)
            self.produced += len(pcm)
        return self.progress(turn)

    def mark(self, turn: str, segment: int) -> dict[str, Any]:
        self._check(turn)
        if segment != self.segments + 1:
            raise LoopError("remote_segment_sequence")
        self.segments += 1
        return self.progress(turn)

    def finish(self, turn: str) -> dict[str, Any]:
        self._check(turn)
        self.ended = True
        return self.progress(turn)

    def pull(self, limit: int = SOURCE_CREDIT) -> dict[str, Any]:
        if limit < 2 or limit > SOURCE_CREDIT or limit % 2:
            limit = SOURCE_CREDIT
        if not self.turn or self.closed:
            return {"pcm": "", "rate": self.rate or 0, "done": True}
        chunk = bytes(self.pending[:limit])
        del self.pending[: len(chunk)]
        if chunk and self.first_pull is None:
            self.first_pull = time.perf_counter()
        self.pulled += len(chunk)
        return {
            "pcm": base64.b64encode(chunk).decode("ascii") if chunk else "",
            "rate": self.rate,
            "done": self.ended and not self.pending,
        }

    def hear(self, amount: int, done: bool) -> None:
        if type(amount) is not int or amount < 0 or amount > self.produced:
            raise LoopError("invalid_phone_ack")
        if amount > self.acked:
            self.acked = amount
        if done and self.ended:
            self.acked = max(self.acked, self.produced)

    def progress(self, turn: str) -> dict[str, Any]:
        self._check(turn)
        duration = self.produced / (self.rate * 2) if self.rate else 0.0
        estimated = (
            self.first_pull is not None
            and self.ended
            and time.perf_counter() >= self.first_pull + duration
        )
        # A byte ack only proves the phone received the PCM. Playback still runs
        # for `duration` after the first pull; finishing here would start the next TTS.
        done = estimated or (self.ended and self.produced == 0)
        return {
            "completed_segments": list(range(1, self.segments + 1)) if done else [],
            "done": done,
            "playing": self.pulled > 0 and not done,
            "free_source_bytes": max(0, SOURCE_CAPACITY - len(self.pending)),
            "first_driver": self.first_pull,
            "first_dac_estimate": self.first_pull,
            "last_dac_estimate": (self.first_pull + duration) if self.first_pull else None,
            "pcm_driver_bytes": self.pulled,
            "underflows": 0,
            "stream_format": {"rate": self.rate, "channels": 1, "format": "PCM16"},
            "first_remote_send": self.first_pull,
            "remote_sent_bytes": self.pulled,
        }

    async def drained(self, turn: str) -> dict[str, Any]:
        result = self.progress(turn)
        if not result["done"]:
            raise LoopError("remote_output_not_drained")
        self.closed = True
        return result


class WebPhoneAudio:
    """AudioClient listen/STT + WebSink playback. Never calls PortAudio check/begin."""

    def __init__(self, worker: AudioClient) -> None:
        self.worker = worker
        self.sink = WebSink()
        self.token = ""
        self.sequence = 0
        self._event = getattr(worker, "event", None)
        if callable(self._event):
            worker.event = self._on_event

    @property
    def session(self) -> str:
        return self.worker.session

    @session.setter
    def session(self, value: str) -> None:
        self.worker.session = value

    @property
    def ready(self) -> dict[str, Any]:
        return self.worker.ready

    @property
    def closed(self) -> bool:
        return bool(getattr(self.worker, "closed", False))

    def _on_event(self, item: dict[str, Any]) -> None:
        kind = item.get("event")
        if kind == "listening":
            device = item.get("data", {}).get("device", {})
            token = device.get("ingress_token", "")
            self.token = token if isinstance(token, str) else ""
            self.sequence = 0
        elif kind in {"transcribing", "reconnecting"}:
            self.token = ""
        if callable(self._event):
            self._event(item)

    def feed_uplink(self, pcm: bytes, *, capture: str, sequence: int) -> None:
        if not self.token or capture != self.token:
            raise LoopError("stale_phone_capture")
        if not 0 < len(pcm) <= CAPTURE_BATCH_BYTES or len(pcm) % FRAME_BYTES:
            raise LoopError("invalid_remote_pcm")
        if type(sequence) is not int or sequence != self.sequence:
            self.token = ""
            raise LoopError("phone_capture_discontinuity")
        try:
            for offset in range(0, len(pcm), FRAME_BYTES):
                self.worker.feed_remote(
                    capture, pcm[offset : offset + FRAME_BYTES], time.perf_counter()
                )
        except LoopError:
            self.token = ""
            raise
        self.sequence += len(pcm) // FRAME_BYTES

    def pull_pcm(self) -> dict[str, Any]:
        return self.sink.pull()

    def hear(self, amount: int, done: bool) -> None:
        if not self.sink.turn or self.sink.closed:
            return
        self.sink.hear(amount, done)

    async def start(self) -> None:
        await self.worker.start()

    async def close(self) -> None:
        self.token = ""
        self.sink.abort()
        await self.worker.close()

    async def abort(self, turn: str) -> dict[str, Any]:
        self.token = ""
        self.sink.abort()
        return await self.worker.abort(turn)

    async def write_pcm(self, turn: str, pcm: bytes) -> dict[str, Any]:
        return self.sink.feed(turn, pcm)

    async def call(
        self, op: str, *, turn: str = "", timeout: float = 5, **arguments: Any
    ) -> dict[str, Any]:
        if op == "preflight":
            return {"input": dict(PHONE_DEVICE), "output": dict(PHONE_DEVICE), "capture": "NOT_RUN"}
        if op == "check":
            return {"device": dict(PHONE_DEVICE), "status": "PASS"}
        if op == "begin":
            return self.sink.begin(turn, int(arguments["rate"]))
        if op == "progress":
            return self.sink.progress(turn)
        if op == "mark":
            return self.sink.mark(turn, int(arguments["segment"]))
        if op == "finish":
            return self.sink.finish(turn)
        if op == "drained":
            return await self.sink.drained(turn)
        if op == "pcm":
            raw = arguments.get("pcm")
            if not isinstance(raw, str):
                raise LoopError("invalid_remote_pcm")
            return self.sink.feed(turn, base64.b64decode(raw, validate=True))
        return await self.worker.call(op, turn=turn, timeout=timeout, **arguments)
