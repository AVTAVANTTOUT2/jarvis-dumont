"""One local Silero stream and continuous SoXR conversion; no audio opened on import."""

import math
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soxr
from numpy.typing import NDArray

from jarvis_office.config import Speech

Samples = NDArray[np.float32]
RATE = 16000
FRAME = 512  # Silero v6: exactly 32 ms, not 30 ms.


class AudioError(Exception):
    pass


class Normalizer:
    def __init__(self, rate: int, channels: int) -> None:
        if rate not in {16000, 24000, 44100, 48000} or channels not in {1, 2}:
            raise AudioError("unsupported_audio_format")
        self.rate, self.channels = rate, channels
        self.resampler = soxr.ResampleStream(rate, RATE, 1, dtype="float32", quality="HQ")
        self.input_samples = 0
        self.output_samples = 0

    def feed(self, data: Samples, *, last: bool = False) -> Samples:
        if data.ndim != 2 or data.shape[1] != self.channels or not np.isfinite(data).all():
            raise AudioError("invalid_audio_samples")
        mono = np.asarray(data.mean(axis=1), dtype=np.float32)
        self.input_samples += len(mono)
        result = np.asarray(self.resampler.resample_chunk(mono, last=last), dtype=np.float32)
        self.output_samples += len(result)
        return result

    @property
    def delay_samples(self) -> float:
        return float(self.resampler.delay())


def read_wav(path: Path, max_seconds: int = 60) -> Samples:
    """Bounded PCM16 WAV input; file replay has no hardware-latency claim."""
    if not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
        raise AudioError("wav_missing_or_too_large")
    try:
        with wave.open(str(path), "rb") as source:
            rate, channels, count = (
                source.getframerate(),
                source.getnchannels(),
                source.getnframes(),
            )
            if source.getsampwidth() != 2 or count == 0 or count > rate * max_seconds:
                raise AudioError("wav_format_or_duration_invalid")
            normalizer = Normalizer(rate, channels)
            chunks = []
            read = 0
            while raw := source.readframes(4096):
                if len(raw) % (2 * channels):
                    raise AudioError("truncated_wav")
                data = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels)
                read += len(data)
                chunks.append(normalizer.feed(data / np.float32(32768)))
            if read != count:
                raise AudioError("truncated_wav")
            chunks.append(normalizer.feed(np.empty((0, channels), np.float32), last=True))
            return np.concatenate(chunks)
    except (wave.Error, EOFError, ValueError):
        raise AudioError("corrupt_wav") from None


class Silero:
    """Silero v6.2.1 ONNX ABI, CPU only. Independent state and context per instance.

    New NumPy implementation of the published ABI (input/state/sr -> output/stateN),
    not a copy of V1 or the Torch wrapper. Full frames only; caller owns leftovers.
    """

    def __init__(self, model: Path) -> None:
        import onnxruntime as ort

        if not model.is_file():
            raise AudioError("silero_model_missing")
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        if {x.name for x in self.session.get_inputs()} != {"input", "state", "sr"}:
            raise AudioError("unsupported_silero_abi")
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def score(self, frame: Samples) -> float:
        if frame.shape != (FRAME,) or not np.isfinite(frame).all():
            raise AudioError("silero_requires_512_samples")
        x = np.concatenate((self.context, frame.reshape(1, -1)), axis=1)
        out, state = self.session.run(
            None, {"input": x, "state": self.state, "sr": np.array(RATE, np.int64)}
        )
        score = float(out[0, 0])
        if (
            not math.isfinite(score)
            or not 0 <= score <= 1
            or state.shape != (2, 1, 128)
            or not np.isfinite(state).all()
        ):
            raise AudioError("invalid_silero_output")
        self.state = state
        self.context = x[:, -64:].copy()
        return score


@dataclass
class Utterance:
    audio: Samples
    audio_start: int
    speech_start: int
    speech_end: int
    finalized: int
    reason: str
    finalized_wall: float | None = None

    def timing(self) -> dict[str, Any]:
        return {
            "audio_start_s": self.audio_start / RATE,
            "speech_start_s": self.speech_start / RATE,
            "speech_end_s": self.speech_end / RATE,
            "finalized_s": self.finalized / RATE,
            "finalized_wall_s": self.finalized_wall,
            "wall_clock": "perf_counter" if self.finalized_wall is not None else None,
            "endpoint_delay_s": (self.finalized - self.speech_end) / RATE,
            "reason": self.reason,
            "clock": "capture_samples" if self.finalized_wall is not None else "file_samples",
            "hardware_latency": "NOT_RUN"
            if self.finalized_wall is None
            else "see_capture_timestamps",
        }


class Segmenter:
    def __init__(self, settings: Speech, *, realtime: bool = False) -> None:
        self.settings, self.realtime = settings, realtime
        self.reset()

    def reset(self) -> None:
        self.pending = np.empty(0, dtype=np.float32)
        self.roll: deque[Samples] = deque(maxlen=math.ceil(self.settings.pre_roll_ms * 16 / FRAME))
        self.chunks: list[Samples] = []
        self.seen = self.audio_start = self.speech_start = self.speech_end = 0
        self.silence = self.voiced = 0

    def _frame(self, frame: Samples, probability: float) -> Utterance | None:
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise AudioError("invalid_vad_probability")
        start = self.seen
        self.seen += len(frame)
        speech = probability >= self.settings.vad_threshold
        if not self.chunks:
            if not speech:
                self.roll.append(frame.copy())
                return None
            self.chunks = list(self.roll)
            self.audio_start = start - sum(map(len, self.chunks))
            self.speech_start = start
            self.roll.clear()
        self.chunks.append(frame.copy())
        if speech:
            self.speech_end = self.seen
            self.voiced += len(frame)
            self.silence = 0
        else:
            self.silence += len(frame)
        if self.seen - self.speech_start >= self.settings.max_utterance_s * RATE:
            return self._finish("max_duration")
        if self.silence >= self.settings.terminal_silence_ms * 16:
            return self._finish("terminal_silence")
        return None

    def _finish(self, reason: str) -> Utterance | None:
        utterance = None
        if self.voiced >= self.settings.min_speech_ms * 16:
            utterance = Utterance(
                np.concatenate(self.chunks),
                self.audio_start,
                self.speech_start,
                self.speech_end,
                self.seen,
                reason,
                time.perf_counter() if self.realtime else None,
            )
        self.chunks = []
        self.voiced = self.silence = 0
        self.roll.clear()
        return utterance

    def feed(self, samples: Samples, score: Callable[[Samples], float]) -> list[Utterance]:
        if samples.ndim != 1 or not np.isfinite(samples).all():
            raise AudioError("invalid_vad_audio")
        data = np.concatenate((self.pending, samples))
        complete = len(data) // FRAME * FRAME
        results = []
        for start in range(0, complete, FRAME):
            frame = data[start : start + FRAME]
            utterance = self._frame(frame, score(frame))
            if utterance is not None:
                results.append(utterance)
        self.pending = data[complete:].copy()
        return results

    def finish(self, score: Callable[[Samples], float]) -> list[Utterance]:
        results = []
        if len(self.pending):
            probability = score(np.pad(self.pending, (0, FRAME - len(self.pending))))
            item = self._frame(self.pending, probability)
            if item is not None:
                results.append(item)
            self.pending = np.empty(0, dtype=np.float32)
        if self.chunks:
            item = self._finish("end_of_file")
            if item is not None:
                results.append(item)
        return results


def segment_audio(audio: Samples, vad: Silero, settings: Speech) -> list[Utterance]:
    vad.reset()
    stream = Segmenter(settings)
    # Feed bounded blocks even for fast replay; metrics use sample counts only.
    results = []
    for offset in range(0, len(audio), 4096):
        results.extend(stream.feed(audio[offset : offset + 4096], vad.score))
    results.extend(stream.finish(vad.score))
    return results
