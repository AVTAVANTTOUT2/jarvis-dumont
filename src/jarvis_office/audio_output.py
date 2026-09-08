"""One owned output stream per response. DAC deadlines are estimates, not acoustic proof."""

import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any

import numpy as np
import soxr

from jarvis_office.audio_input import AudioError
from jarvis_office.config import Voice


def output_preflight() -> None:
    if sys.platform != "darwin":
        raise AudioError("output_requires_macos_validation")
    for label in (
        "com.jarvis.ingestion",
        "com.jarvis.macos-bridge",
        "com.jarvis.supervisor",
        "com.jarvis.t710-tunnel",
    ):
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 113:
            raise AudioError("v1_service_conflict_or_unknown")


def resolve_output(sd: Any, settings: Voice) -> tuple[int, dict[str, Any]]:
    if not settings.output_device:
        raise AudioError("explicit_output_device_required")
    matches = [
        (i, d)
        for i, d in enumerate(sd.query_devices())
        if d["name"] == settings.output_device and d["max_output_channels"] >= 1
    ]
    if len(matches) != 1:
        raise AudioError("output_device_missing_or_ambiguous")
    index, device = matches[0]
    try:
        sd.check_output_settings(
            device=index,
            channels=settings.output_channels,
            samplerate=settings.output_rate,
            dtype="float32",
        )
    except sd.PortAudioError:
        raise AudioError("output_format_unsupported") from None
    return index, {
        "name": device["name"],
        "rate": settings.output_rate,
        "channels": settings.output_channels,
        "identity": "exact_unique_name",
    }


class Playback:
    """Preallocated two-second (configurable) ring, measured in device-format bytes.

    The callback never waits for the producer's lock. Contention, device underflow or
    starvation fail explicitly; no sample is discarded and then called confirmed.
    Markers count resampled samples, including SoXR's delayed tail across segments.
    """

    def __init__(self, sd: Any, settings: Voice, source_rate: int) -> None:
        self.sd, self.settings = sd, settings
        self.index, self.device = resolve_output(sd, settings)
        if source_rate != 24000:
            raise AudioError("unexpected_qwen_pcm_format")
        self.source_rate = source_rate
        self.resampler = soxr.ResampleStream(
            source_rate, settings.output_rate, 1, dtype="float32", quality="HQ"
        )
        self.capacity = round(settings.output_rate * settings.pcm_seconds)
        self.ring = np.zeros((self.capacity, settings.output_channels), dtype=np.float32)
        self.lock = threading.Lock()
        self.read = self.write = self.queued = self.produced = self.delivered = self.source = 0
        self.peak_bytes = 0
        self.stream: Any = None
        self.ended = self.aborted = False
        self.error: str | None = None
        self.first_driver: float | None = None
        self.first_converted: float | None = None
        self.stream_format: dict[str, Any] = {}
        self.first_dac: float | None = None
        self.last_dac: float | None = None
        self.markers: deque[tuple[int, int]] = deque()
        self.deadlines: deque[tuple[int, float | None]] = deque()
        self.completed: list[int] = []
        self.underflows = 0
        self.marked = 0

    def _append(self, mono: Any) -> None:
        count = len(mono)
        if count and self.first_converted is None:
            self.first_converted = time.perf_counter()
        with self.lock:
            if self.aborted or self.error:
                raise AudioError(self.error or "playback_aborted")
            if count > self.capacity - self.queued:
                raise AudioError("pcm_queue_full")
            first = min(count, self.capacity - self.write)
            self.ring[self.write : self.write + first] = mono[:first, None]
            self.ring[: count - first] = mono[first:, None]
            self.write = (self.write + count) % self.capacity
            self.queued += count
            self.produced += count
            self.peak_bytes = max(self.peak_bytes, self.queued * self.settings.output_channels * 4)
        if self.stream is None and self.queued >= round(
            self.settings.prefill_seconds * self.settings.output_rate
        ):
            self._start()

    def _start(self) -> None:
        if self.aborted:
            raise AudioError("playback_aborted")
        self.stream = self.sd.OutputStream(
            device=self.index,
            samplerate=self.settings.output_rate,
            channels=self.settings.output_channels,
            dtype="float32",
            blocksize=round(self.settings.output_rate * 0.02),
            callback=self.callback,
            extra_settings=self.sd.CoreAudioSettings(change_device_parameters=False)
            if sys.platform == "darwin"
            else None,
        )
        self.stream.start()
        self.stream_format = {
            "rate": getattr(self.stream, "samplerate", None),
            "channels": getattr(self.stream, "channels", None),
            "latency_s": getattr(self.stream, "latency", None),
        }

    def feed(self, pcm: bytes) -> None:
        if self.ended or len(pcm) > 48_000 or len(pcm) % 2:
            raise AudioError("invalid_playback_pcm")
        if not pcm:
            return
        mono = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / np.float32(32768)
        self.source += len(mono)
        self._append(self.resampler.resample_chunk(mono))

    def mark(self, segment: int) -> None:
        if segment != self.marked + 1:
            raise AudioError("playback_segment_order")
        if segment > 128:
            raise AudioError("playback_segment_limit")
        target = round(self.source * self.settings.output_rate / self.source_rate)
        # Conversion may emit a segment boundary before its marker arrives. The last
        # DAC deadline is later, hence conservative, never inferred from byte percent.
        with self.lock:
            self.marked = segment
            if target <= self.delivered:
                self.deadlines.append((segment, self.last_dac))
            else:
                self.markers.append((segment, target))

    def finish(self) -> None:
        self._append(self.resampler.resample_chunk(np.empty(0, np.float32), last=True))
        self.ended = True
        if not self.produced:
            raise AudioError("empty_playback")
        if self.stream is None:
            self._start()

    def callback(self, out: Any, frames: int, times: Any, status: Any) -> None:
        out.fill(0)
        if self.aborted:
            raise self.sd.CallbackAbort
        if status or frames > self.capacity or not self.lock.acquire(blocking=False):
            self.error = "output_device_underflow_or_contention"
            self.underflows += 1
            raise self.sd.CallbackAbort
        try:
            count = min(frames, self.queued)
            first = min(count, self.capacity - self.read)
            out[:first] = self.ring[self.read : self.read + first]
            out[first:count] = self.ring[: count - first]
            self.read = (self.read + count) % self.capacity
            self.queued -= count
            now = time.perf_counter()
            dac, current = float(times.outputBufferDacTime), float(times.currentTime)
            mapped = (
                now + dac - current
                if (math.isfinite(dac) and math.isfinite(current) and 0 <= dac - current <= 2)
                else None
            )
            if count:
                if self.first_driver is None:
                    self.first_driver, self.first_dac = now, mapped
                self.delivered += count
                self.last_dac = (
                    mapped + count / self.settings.output_rate if mapped is not None else None
                )
                while self.markers and self.markers[0][1] <= self.delivered:
                    segment, _ = self.markers.popleft()
                    self.deadlines.append((segment, self.last_dac))
            if count < frames and not self.ended:
                self.error = "output_starvation"
                self.underflows += 1
                raise self.sd.CallbackAbort
            if self.ended and not self.queued:
                raise self.sd.CallbackStop
        finally:
            self.lock.release()

    def progress(self) -> dict[str, Any]:
        now = time.perf_counter()
        if not self.error:
            while self.deadlines and self.deadlines[0][1] is not None:
                deadline = self.deadlines[0][1]
                assert deadline is not None
                if now < deadline:
                    break
                self.completed.append(self.deadlines.popleft()[0])
        done = bool(
            self.ended
            and self.stream is not None
            and not self.stream.active
            and not self.queued
            and self.last_dac is not None
            and now >= self.last_dac
        )
        return {
            "completed_segments": list(self.completed),
            "done": done,
            "error": self.error,
            "pcm_driver_bytes": self.delivered * self.settings.output_channels * 4,
            "pcm_peak_bytes": self.peak_bytes,
            "pcm_capacity_bytes": self.ring.nbytes,
            "free_source_bytes": max(
                0,
                int(
                    (self.capacity - self.queued - self.resampler.delay() - 8)
                    * self.source_rate
                    / self.settings.output_rate
                ),
            )
            * 2,
            "underflows": self.underflows,
            "first_driver": self.first_driver,
            "first_converted": self.first_converted,
            "stream_format": dict(self.stream_format),
            "first_dac_estimate": self.first_dac,
            "last_dac_estimate": self.last_dac,
            "playback_clock": "perf_counter_mapped_from_PortAudio_callback_estimate",
            "acoustic_verification": "NOT_RUN",
        }

    def close(self, *, abort: bool) -> dict[str, Any]:
        # Refresh completed prefix before discarding pending samples on cancellation.
        result = self.progress()
        self.aborted = abort
        if self.stream is not None:
            try:
                if abort:
                    self.stream.abort(ignore_errors=False)
                else:
                    self.stream.stop(ignore_errors=False)
            finally:
                self.stream.close(ignore_errors=False)
                self.stream = None
        with self.lock:
            self.queued = 0
        return result
