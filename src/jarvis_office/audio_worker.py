"""Offline persistent STT/capture/output child; no DeepSeek credentials or HTTP client."""

import asyncio
import base64
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from jarvis_office.audio_client import WIRE_LIMIT, source_signature
from jarvis_office.config import Config, load_config


class AudioEngine:
    def __init__(self, config: Config, emit: Callable[[str, dict[str, Any]], None]) -> None:
        import sounddevice as sd

        from jarvis_office.audio_input import AudioError
        from jarvis_office.stt import Recognizer

        if config.assets.stt_model is None or config.assets.vad_model is None:
            raise AudioError("selected_stt_assets_required")
        self.config, self.sd, self.emit = config, sd, emit
        self.recognizer = Recognizer(
            config.assets.stt_model, config.assets.vad_model, config.speech
        )
        self.lock = threading.Lock()
        self.input: Any = None
        self.playback: Any = None
        self.cancelled = threading.Event()
        self.turn = ""
        self.busy = False
        self.inference_busy = False

    def stop_input(self) -> None:
        with self.lock:
            stream, self.input = self.input, None
            if stream is not None:
                try:
                    stream.abort(ignore_errors=False)
                finally:
                    stream.close(ignore_errors=False)

    def abort(self) -> dict[str, Any]:
        self.cancelled.set()
        self.stop_input()
        result = self.playback.close(abort=True) if self.playback else {}
        self.playback = None
        self.turn = ""
        return {**result, "inference_busy": self.inference_busy, "microphone": "closed"}

    def listen(self, turn: str, seconds: float) -> dict[str, Any]:
        import numpy as np

        from jarvis_office.audio_input import AudioError, Normalizer, Segmenter
        from jarvis_office.capture import CaptureQueue, microphone_preflight, resolve_input

        if self.playback is not None or not 0 < seconds <= 300:
            raise AudioError("audio_capture_busy_or_invalid")
        settings = self.config.speech
        deadline = time.perf_counter() + seconds
        try:
            # Any failed attempt discards all frames and resets every stream state.
            for attempt in range(settings.reconnect_attempts + 1):
                try:
                    check = microphone_preflight()
                    if check["status"] != "PASS":
                        raise AudioError(str(check["reason"]))
                    if self.cancelled.is_set():
                        return {"cancelled": True}
                    index, device = resolve_input(
                        self.sd, settings.input_device, settings.input_rate
                    )
                    channel = CaptureQueue(settings.queue_blocks)
                    normalizer = Normalizer(settings.input_rate, 1)
                    segmenter = Segmenter(settings, realtime=True)
                    self.recognizer.vad.reset()
                    sequence = expected = 0
                    energy = 0.0
                    adc_previous: float | None = None
                    origin: float | None = None
                    last_block = level_at = time.perf_counter()
                    with self.lock:
                        if self.cancelled.is_set():
                            return {"cancelled": True}
                        self.input = self.sd.InputStream(
                            device=index,
                            samplerate=settings.input_rate,
                            channels=1,
                            dtype="float32",
                            blocksize=round(settings.input_rate * 0.02),
                            callback=channel.callback,
                            extra_settings=self.sd.CoreAudioSettings(
                                change_device_parameters=False
                            ),
                        )
                        self.input.start()
                        device.update(
                            stream_rate=getattr(self.input, "samplerate", None),
                            stream_channels=getattr(self.input, "channels", None),
                        )
                    self.emit(
                        "listening",
                        {"device": device, "microphone": "open", "opened_at": time.perf_counter()},
                    )
                    while time.perf_counter() < deadline and not self.cancelled.is_set():
                        if channel.lost:
                            raise AudioError("capture_discontinuity")
                        try:
                            block = channel.blocks.get(timeout=0.1)
                        except queue.Empty:
                            if time.perf_counter() - last_block > 2:
                                raise AudioError("capture_disconnected") from None
                            continue
                        last_block = time.perf_counter()
                        if (
                            block.sequence != sequence + 1
                            or block.start_sample != expected
                            or (
                                adc_previous is not None
                                and abs(block.adc_time - adc_previous) > 0.01
                            )
                        ):
                            raise AudioError("capture_discontinuity")
                        sequence = block.sequence
                        expected += len(block.data)
                        energy += float(np.sum(block.data**2, dtype=np.float64))
                        adc_previous = block.adc_time + len(block.data) / settings.input_rate
                        if sequence == 1 and block.callback_current_time is not None:
                            lag = block.callback_current_time - block.adc_time
                            if 0 <= lag <= 2:
                                origin = block.received_wall - lag
                        if last_block - level_at >= 0.25:
                            self.emit("level", {"level": float(np.sqrt(np.mean(block.data**2)))})
                            level_at = last_block
                        utterances = segmenter.feed(
                            normalizer.feed(block.data), self.recognizer.vad.score
                        )
                        if utterances:
                            utterance = utterances[0]  # Close immediately; no backlog during STT.
                            self.stop_input()
                            if channel.lost:
                                raise AudioError("capture_discontinuity")
                            if self.cancelled.is_set():
                                return {"cancelled": True}
                            speech_end = (
                                origin + utterance.speech_end / 16000
                                if origin is not None
                                else None
                            )
                            timing = {
                                "speech_start_estimate": origin + utterance.speech_start / 16000
                                if origin is not None
                                else None,
                                "speech_end_estimate": speech_end,
                                "vad_finalized": utterance.finalized_wall,
                                "vad_delay_s": (utterance.finalized - utterance.speech_end) / 16000,
                                "input_clock": "ADC_mapped_to_perf_counter_estimate"
                                if origin is not None
                                else "UNAVAILABLE",
                                "reconnects": attempt,
                                "pre_roll_s": (utterance.speech_start - utterance.audio_start)
                                / 16000,
                                "terminal_silence_ms": settings.terminal_silence_ms,
                                "normalized_rate": 16000,
                                "vad_frame_samples": 512,
                                "input_samples": expected,
                                "callback_dropped": channel.dropped,
                                "rms_capture": (energy / expected) ** 0.5,
                            }
                            self.emit("transcribing", {"microphone": "closed", "timing": timing})
                            with self.lock:
                                if self.cancelled.is_set():
                                    return {"cancelled": True}
                                self.inference_busy = True
                            try:
                                result = self.recognizer.transcribe_utterance(utterance)
                            finally:
                                self.inference_busy = False
                            if self.cancelled.is_set():
                                return {"cancelled": True}
                            return {
                                **result,
                                "timing": timing,
                                "device": device,
                                "microphone": "closed",
                            }
                    return {"silence": True, "microphone": "closed"}
                except AudioError as exc:
                    self.stop_input()
                    if (
                        str(exc) not in {"capture_discontinuity", "capture_disconnected"}
                        or attempt == settings.reconnect_attempts
                        or self.cancelled.is_set()
                    ):
                        raise
                    self.emit("reconnecting", {"reason": str(exc), "attempt": attempt + 1})
            raise AudioError("capture_reconnection_exhausted")
        finally:
            self.stop_input()
            self.busy = False

    def command(self, op: str, turn: str, args: dict[str, Any]) -> dict[str, Any]:
        from jarvis_office.audio_input import AudioError
        from jarvis_office.audio_output import Playback, output_preflight, resolve_output

        if op == "clock":
            return {"clock": time.perf_counter()}
        if op == "abort":
            return self.abort()
        if op == "preflight":
            from jarvis_office.capture import microphone_preflight, resolve_input

            output_preflight()
            _, output_device = resolve_output(self.sd, self.config.voice)
            _, input_device = resolve_input(
                self.sd, self.config.speech.input_device, self.config.speech.input_rate
            )
            check = microphone_preflight()
            if check["status"] != "PASS":
                raise AudioError("microphone_preflight_blocked")
            return {"input": input_device, "output": output_device, "capture": "NOT_RUN"}
        if op == "check":
            output_preflight()
            _, device = resolve_output(self.sd, self.config.voice)
            return {"device": device, "status": "PASS"}
        if op == "begin":
            if self.busy or self.playback is not None:
                raise AudioError("audio_busy")
            output_preflight()
            self.turn = turn
            self.playback = Playback(self.sd, self.config.voice, args["rate"])
            return {"device": self.playback.device}
        if not turn or self.turn != turn or self.playback is None:
            raise AudioError("stale_audio_turn")
        if op == "pcm":
            pcm = base64.b64decode(args["pcm"], validate=True)
            self.playback.feed(pcm)
        elif op == "mark":
            self.playback.mark(args["segment"])
        elif op == "finish":
            self.playback.finish()
        elif op == "drained":
            if not self.playback.progress()["done"]:
                raise AudioError("output_not_drained")
            result = self.playback.close(abort=False)
            self.playback = None
            return dict(result)
        elif op != "progress":
            raise AudioError("unknown_audio_command")
        return dict(self.playback.progress())


async def serve(config: Config, out: BinaryIO) -> int:
    from jarvis_office.assets import AssetError
    from jarvis_office.audio_input import AudioError
    from jarvis_office.speech_cli import deny_network

    # The asyncio loop exists before the socket audit guard (its own wakeup socket).
    deny_network()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=WIRE_LIMIT + 1)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    output: asyncio.Queue[bytes] = asyncio.Queue(64)
    engine: AudioEngine | None = None
    session = ""
    capture: asyncio.Task[None] | None = None

    def send(item: dict[str, Any]) -> None:
        raw = json.dumps(item, allow_nan=False).encode() + b"\n"
        if len(raw) > 16384:
            raise AudioError("audio_reply_too_large")
        output.put_nowait(raw)

    async def writer() -> None:
        while True:
            raw = await output.get()
            await asyncio.to_thread(out.write, raw)

    async def handle(request: dict[str, Any]) -> None:
        nonlocal engine, session
        envelope = {k: request[k] for k in ("id", "session", "turn")}
        try:
            op, args = request["op"], request["args"]
            if op == "start" and engine is None:
                session = request["session"]

                def emit(event: str, data: dict[str, Any]) -> None:
                    assert engine is not None
                    item = {"event": event, "session": session, "turn": engine.turn, "data": data}
                    loop.call_soon_threadsafe(send, item)

                engine = await asyncio.to_thread(AudioEngine, config, emit)
                result = {
                    "code_signature": source_signature(),
                    "load_s": engine.recognizer.load_s,
                    "warmup_s": engine.recognizer.warmup_s,
                    "backend": engine.recognizer.backend,
                    "qualification": "PROVISIONAL_NO_ACCEPTABLE_STT",
                    "microphone": "closed",
                }
            elif op == "reset" and engine is not None and not engine.busy:
                await asyncio.to_thread(engine.abort)
                engine.recognizer.vad.reset()
                session = request["session"]
                result = {"microphone": "closed"}
            elif engine is None or request["session"] != session:
                raise AudioError("stale_audio_session")
            elif op == "listen":
                if engine.busy:
                    raise AudioError("capture_busy")
                # Establish cancellation before scheduling the native thread. A late
                # thread must not clear a Pause/Stop that arrived in the meantime.
                engine.busy = True
                engine.turn = request["turn"]
                engine.cancelled = threading.Event()
                result = await asyncio.to_thread(engine.listen, request["turn"], args["seconds"])
            else:
                result = await asyncio.to_thread(engine.command, op, request["turn"], args)
                if op == "abort" and capture is not None and not capture.done():
                    done, _ = await asyncio.wait({capture}, timeout=1)
                    result["capture_busy"] = not bool(done)
            send({**envelope, "result": result})
        except Exception as exc:
            reason = (
                str(exc) if isinstance(exc, (AudioError, AssetError)) else "audio_operation_failed"
            )
            send({**envelope, "error": reason})

    writing = asyncio.create_task(writer())
    try:
        while raw := await reader.readline():
            if len(raw) > WIRE_LIMIT or not raw.endswith(b"\n"):
                return 1
            request = json.loads(raw)
            if (
                not isinstance(request, dict)
                or set(request) != {"id", "session", "turn", "op", "args"}
                or type(request["id"]) is not int
                or not isinstance(request["args"], dict)
                or any(
                    not isinstance(request[k], str) or len(request[k]) > 64
                    for k in ("session", "turn", "op")
                )
            ):
                return 1
            if request["op"] == "listen":
                if capture is not None and not capture.done():
                    send(
                        {k: request[k] for k in ("id", "session", "turn")}
                        | {"error": "capture_busy"}
                    )
                else:
                    capture = asyncio.create_task(handle(request))
            else:
                await handle(request)
        return 0
    finally:
        transport.close()
        if engine is not None:
            await asyncio.to_thread(engine.abort)
        if capture is not None:
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
        if engine is not None and not engine.inference_busy:
            await asyncio.to_thread(engine.recognizer.close)
        writing.cancel()
        await asyncio.gather(writing, return_exceptions=True)
        # Parent bounds process termination even if a native inference thread is stuck.


def main() -> int:
    with os.fdopen(os.dup(1), "wb", buffering=0) as out:
        sys.stdout.flush()
        os.dup2(2, 1)
        try:
            config = load_config(Path(sys.argv[1]))
            return asyncio.run(serve(config, out))
        except Exception:
            return 1  # No raw exception, transcript or asset path on stderr.


if __name__ == "__main__":
    raise SystemExit(main())
