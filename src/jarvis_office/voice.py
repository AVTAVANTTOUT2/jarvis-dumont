"""Single semi-duplex turn owner; no engine imports in the controller."""

import asyncio
import contextlib
import re
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from jarvis_office.audio_client import AudioClient, LoopError, source_signature
from jarvis_office.config import Config
from jarvis_office.deepseek import ChatError, DeepSeek
from jarvis_office.runtime import Instance, RuntimeLog
from jarvis_office.tts import TTSClient, TTSError


def addressed(text: str) -> str | None:
    match = re.match(r"^\s*jarvis(?=$|[\s,;:.!?…])", text, re.IGNORECASE)
    if match is None:
        return None
    return text[match.end() :].lstrip(" \t\r\n,;:.!?…")


class VoiceLoop:
    def __init__(
        self,
        config: Config,
        path: Path,
        chat: DeepSeek,
        *,
        audio: Any = None,
        tts: Any = None,
        transcript_context: Any = None,
    ) -> None:
        self.config, self.path, self.chat = config, path, chat
        self.transcript_context = transcript_context
        self.on_turn_finished: Any = None
        self.archive_generation: Any = None
        self.session = uuid.uuid4().hex
        self.turn = ""
        self.audio = audio or AudioClient(config, path, self.session, self.audio_event)
        self.tts = tts or TTSClient.for_config(path, config.tts)
        self.state = "starting"
        self.ready = False
        self.armed = False
        self.closed = False
        self.shutdown_verified = False
        self.accepted = self.answer = ""
        self.error: str | None = None
        self.microphone = "closed"
        self.level = 0.0
        self.playback = "not_started"
        self.input_device: dict[str, Any] = {}
        self.output_device: dict[str, Any] = {}
        self.metrics: dict[str, Any] = {}
        self.results: list[dict[str, Any]] = []  # Expurgated, at most 20 per invocation.
        self.task: asyncio.Task[None] | None = None
        self.cancel_tts = asyncio.Event()
        self.control_lock = asyncio.Lock()
        self.completed: list[int] = []
        self.deadline = 0.0
        self.remaining = 0
        self.audio_offset = 0.0
        self.audio_clock_uncertainty = 0.0
        self.stt_deadline: float | None = None
        self.request_context_metadata: dict[str, Any] | None = None
        self.abort_turn: str | None = None
        self.abort_task: asyncio.Task[dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "turn": self.turn,
            "state": self.state,
            "engines_ready": self.ready,
            "output_verified": bool(self.output_device),
            "ready": self.ready and bool(self.output_device) and self.error is None,
            "conversation_ready": self.ready and not self.chat.closed and self.error is None,
            "remote_health": "NOT_RUN — no paid health poll",
            "qualification": "STT_QUALIFICATION_PENDING — NO_ACCEPTABLE_STT",
            "microphone": self.microphone,
            "level": self.level,
            "input": self.input_device,
            "output": self.output_device,
            "selected_input": self.config.speech.input_device,
            "selected_output": self.config.voice.output_device,
            "transcription": self.accepted,
            "response": self.answer,
            "playback": self.playback,
            "metrics": self.metrics,
            "error": self.error,
            "armed": self.armed,
            "notice": (
                ("Écoute armée. " if self.armed else "Écoute non armée. ")
                + "Pendant l'armement : STT local de toute parole. Adresse textuelle Jarvis, "
                "ni wake word acoustique ni identification du locuteur."
            ),
        }

    def audio_event(self, event: dict[str, Any]) -> None:
        if event.get("session") != self.session or not self.turn or event.get("turn") != self.turn:
            return
        data = event.get("data", {})
        if event["event"] == "level":
            self.level = min(1.0, max(0.0, float(data.get("level", 0))))
        elif event["event"] == "transcribing":
            self.state, self.microphone, self.level = "transcribing", "closed", 0
            self.stt_deadline = time.perf_counter() + self.config.voice.stt_timeout
        elif event["event"] == "listening" and self.armed:
            self.state, self.microphone = "listening", "open"
            self.input_device = data["device"]
            if self.results and "rearm_eligible" in self.results[-1]:
                opened = data.get("opened_at")
                if opened is not None:
                    self.results[-1].setdefault("rearmed", opened + self.audio_offset)
        elif event["event"] == "reconnecting":
            self.state, self.microphone = "starting", "closed"

    async def start(self) -> None:
        if self.closed:
            raise LoopError("voice_loop_closed")
        if self.ready:
            return
        self.state = "starting"
        try:
            await self.tts.start()
            if self.tts.ready.get("code_signature") != source_signature():
                raise LoopError("tts_worker_code_mismatch")
            await self.audio.start()
            before = time.perf_counter()
            clock = await self.audio.call("clock")
            after = time.perf_counter()
            self.audio_offset = (before + after) / 2 - clock["clock"]
            self.audio_clock_uncertainty = (after - before) / 2
            self.metrics = {"clock_mapping_uncertainty_s": (after - before) / 2}
            self.ready = True
            self.state = "paused"
        except BaseException:
            await asyncio.gather(self.audio.close(), self.tts.close(), return_exceptions=True)
            self.state, self.error = "error", "engine_startup_failed"
            raise

    def _progress(self, result: dict[str, Any], identifier: str, *, final: bool = False) -> None:
        if self.metrics.get("turn") != identifier or (self.turn != identifier and not final):
            return
        if result.get("error"):
            raise LoopError(str(result["error"]))
        completed = result.get("completed_segments", [])
        if completed != list(range(1, len(completed) + 1)):
            raise LoopError("invalid_playback_confirmation")
        # Concurrent progress replies can contain older, shorter snapshots.
        if len(completed) > len(self.completed):
            self.completed = list(completed)
        for key in ("first_converted", "first_driver", "first_dac_estimate", "last_dac_estimate"):
            value = result.get(key)
            if value is not None:
                self.metrics[key] = value + self.audio_offset
        for key in ("pcm_driver_bytes", "pcm_peak_bytes", "underflows"):
            if key in result:
                self.metrics[key] = result[key]
        for key in ("first_remote_send", "remote_sent_bytes"):
            if result.get(key) is not None:
                self.metrics[key] = result[key]
        if result.get("stream_format"):
            self.metrics["output_stream_format"] = result["stream_format"]
        if (
            (result.get("first_driver") is not None or result.get("playing"))
            and self.turn == identifier
            and not final
        ):
            self.state, self.playback = "speaking", "playing_estimated"

    async def _abort_audio(self, identifier: str) -> None:
        # Control and the turn finalizer share one owned abort and its last DAC prefix.
        if self.abort_task is None or self.abort_turn != identifier:
            self.abort_turn = identifier
            self.abort_task = asyncio.create_task(self.audio.abort(identifier))
        result = await asyncio.shield(self.abort_task)
        self._progress(result, identifier, final=True)

    async def _respond(
        self,
        text: str,
        identifier: str,
        *,
        no_play: bool = False,
        context: str = "",
        context_metadata: dict[str, Any] | None = None,
    ) -> None:
        question = addressed(text)
        if question is None:
            return  # No display, log, history or network of unaddressed office speech.
        self.accepted = text
        if not question:
            self.answer = "Présent. Adressez votre demande à Jarvis."
            return  # Local UI state only; not a measured LLM answer or spoken filler.
        self.answer, self.error, self.playback = "", None, "not_started"
        self.state = "responding"
        self.completed = []
        self.cancel_tts = asyncio.Event()
        segments: list[str] = []
        queue: asyncio.Queue[str | None] = asyncio.Queue(128)
        queued_chars = 0
        tasks: list[asyncio.Task[None]] = []
        pipeline: asyncio.Future[list[None]] | None = None
        complete = False
        self.metrics.update(
            session=self.session,
            turn=identifier,
            status="RUNNING",
            input_kind="explicit_text" if no_play else self.metrics.get("input_kind", "microphone"),
            clock="controller_perf_counter",
            tts=[],
            pcm_received_bytes=0,
            playback="NOT_RUN" if no_play else "PENDING",
            acoustic_verification="NOT_RUN",
            audio_clock_mapping_uncertainty_s=self.audio_clock_uncertainty,
        )
        if not no_play:
            result = await self.audio.call(
                "begin", turn=identifier, rate=self.tts.ready["sample_rate"], timeout=25
            )
            self.output_device = result["device"]
        if self.turn != identifier:
            raise asyncio.CancelledError
        async with self.chat.turn(
            question, turn_id=identifier, context=context, context_metadata=context_metadata
        ) as turn:
            self.metrics["request_started"] = turn.started

            async def receive() -> None:
                nonlocal queued_chars
                async for event in turn:
                    if self.turn != identifier or event.turn_id != identifier:
                        raise LoopError("stale_text_turn")
                    if event.kind == "delta":
                        self.answer += event.text
                    else:
                        if queued_chars + len(event.text) > self.config.voice.text_queue_chars:
                            raise LoopError("text_queue_full")
                        queued_chars += len(event.text)
                        self.metrics["text_queue_peak_chars"] = max(
                            self.metrics.get("text_queue_peak_chars", 0), queued_chars
                        )
                        queue.put_nowait(event.text)
                queue.put_nowait(None)

            async def synthesize() -> None:
                nonlocal queued_chars
                while (segment := await queue.get()) is not None:
                    if self.turn != identifier:
                        raise LoopError("stale_tts_turn")
                    segments.append(segment)
                    started = time.perf_counter()
                    first_pcm = None
                    self.metrics.setdefault("tts_started", started)
                    async with contextlib.aclosing(
                        self.tts.stream(segment, cancel=self.cancel_tts)
                    ) as stream:
                        async for pcm in stream:
                            if self.turn != identifier:
                                raise LoopError("stale_pcm_turn")
                            if first_pcm is None:
                                first_pcm = time.perf_counter()
                                self.metrics.setdefault("first_pcm_delivered", first_pcm)
                            self.metrics["pcm_received_bytes"] += len(pcm)
                            if not no_play:
                                # One bounded frame in flight, no unbounded PCM task fan-out.
                                for offset in range(0, len(pcm), 24000):
                                    chunk = pcm[offset : offset + 24000]
                                    try:
                                        async with asyncio.timeout(3):
                                            while True:
                                                if self.turn != identifier:
                                                    raise LoopError("stale_pcm_turn")
                                                credit = await self.audio.call(
                                                    "progress", turn=identifier
                                                )
                                                if self.turn != identifier:
                                                    raise LoopError("stale_pcm_turn")
                                                self._progress(credit, identifier)
                                                if credit.get(
                                                    "free_source_bytes", len(chunk)
                                                ) >= len(chunk):
                                                    break
                                                await asyncio.sleep(0.02)
                                    except TimeoutError:
                                        raise LoopError("pcm_backpressure_timeout") from None
                                    self._progress(
                                        await self.audio.write_pcm(identifier, chunk),
                                        identifier,
                                    )
                    if self.cancel_tts.is_set() or first_pcm is None:
                        raise LoopError("tts_cancelled_or_empty")
                    self.metrics["tts"].append(
                        {
                            "segment": len(segments),
                            "started": started,
                            "first_delivered": first_pcm,
                            **self.tts.last_metrics,
                            "produced_clock": (
                                "relative_to_worker_synthesis; not subtracted from controller clock"
                            ),
                        }
                    )
                    if not no_play:
                        self._progress(
                            await self.audio.call("mark", turn=identifier, segment=len(segments)),
                            identifier,
                        )
                    queued_chars -= len(segment)
                if not segments:
                    raise LoopError("empty_speakable_response")
                if not no_play:
                    self._progress(await self.audio.call("finish", turn=identifier), identifier)

            async def playback() -> None:
                if no_play:
                    return
                while True:
                    result = await self.audio.call("progress", turn=identifier)
                    self._progress(result, identifier)
                    if result.get("done"):
                        self._progress(
                            await self.audio.call("drained", turn=identifier), identifier
                        )
                        self.metrics["playback_finished"] = time.perf_counter()
                        return
                    await asyncio.sleep(0.04)

            try:
                tasks = [
                    asyncio.create_task(receive()),
                    asyncio.create_task(synthesize()),
                    asyncio.create_task(playback()),
                ]
                pipeline = asyncio.gather(*tasks)
                async with asyncio.timeout(180):
                    # The owner cancels siblings once, before joining their cleanup.
                    await asyncio.shield(pipeline)
                complete = True
                self.metrics["status"] = "PASS"
                self.playback = "NOT_RUN" if no_play else "completed_estimated"
                self.metrics["playback"] = self.playback
            except asyncio.CancelledError:
                self.metrics["status"] = "CANCELLED"
                raise
            except Exception as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, (ChatError, LoopError, TTSError))
                    else "voice_turn_failed"
                )
                self.error, self.state = reason, "error"
                self.metrics.update(status="FAIL", reason=reason)
                raise LoopError(reason) from None
            finally:
                self.cancel_tts.set()
                for task in tasks:
                    if not task.done() and not task.cancelling():
                        task.cancel()

                async def finalize() -> None:
                    # No cancellation caller may interrupt this join/confirmation boundary.
                    if not complete:
                        await asyncio.gather(
                            self._abort_audio(identifier), turn.cancel(), return_exceptions=True
                        )
                    try:
                        async with asyncio.timeout(self.config.tts.drain_timeout + 4):
                            await asyncio.gather(
                                *tasks, *([pipeline] if pipeline else []), return_exceptions=True
                            )
                    except TimeoutError:
                        await self.tts.close()
                        self.ready = False
                    cancelled = self.turn != identifier or self.metrics["status"] == "CANCELLED"
                    if cancelled:
                        self.metrics["status"] = "CANCELLED"
                        self.error = None
                    prefix = " ".join(segments[: len(self.completed)])
                    if prefix and not no_play and not turn.invalidated:
                        turn.confirm(
                            prefix,
                            channel="spoken",
                            complete=complete
                            and not cancelled
                            and len(self.completed) == len(segments),
                        )
                    self.metrics["confirmed_segments"] = len(self.completed)
                    self.metrics["llm"] = dict(turn.metrics)
                    self.metrics["tts_drain_s"] = self.tts.last_drain_seconds
                    self.metrics["tts_restarts"] = self.tts.restarts
                    speech_end, driver = (
                        self.metrics.get("speech_end_estimate"),
                        self.metrics.get("first_driver"),
                    )
                    self.metrics["speech_end_to_driver_s"] = (
                        driver - speech_end
                        if driver is not None and speech_end is not None
                        else None
                    )
                    self.results.append(dict(self.metrics))
                    del self.results[:-20]
                    if self.on_turn_finished is not None:
                        self.on_turn_finished(
                            identifier, text, self.answer, prefix, dict(self.metrics)
                        )

                finalizer = asyncio.create_task(finalize())
                interrupted = False
                while not finalizer.done():
                    try:
                        await asyncio.shield(finalizer)
                    except asyncio.CancelledError:
                        interrupted = True
                        self.metrics["status"] = "CANCELLED"
                finalizer.result()
                if interrupted:
                    raise asyncio.CancelledError

    async def _listen_loop(self) -> None:
        try:
            while self.armed and self.remaining > 0 and time.perf_counter() < self.deadline:
                self.turn = uuid.uuid4().hex
                identifier = self.turn
                self.state = "starting"
                self.stt_deadline = None
                capture = asyncio.create_task(
                    self.audio.call(
                        "listen",
                        turn=identifier,
                        seconds=max(0.01, self.deadline - time.perf_counter()),
                        timeout=max(0.01, self.deadline - time.perf_counter())
                        + self.config.voice.stt_timeout
                        + 35,
                    )
                )
                try:
                    while not capture.done():
                        await asyncio.wait({capture}, timeout=0.05)
                        if (
                            self.stt_deadline is not None
                            and time.perf_counter() > self.stt_deadline
                        ):
                            await self.audio.close()
                            self.ready = False
                            raise LoopError("stt_inference_timeout")
                    result = await capture
                finally:
                    capture.cancel()
                    await asyncio.gather(capture, return_exceptions=True)
                self.microphone, self.level = "closed", 0
                if not self.armed or self.turn != identifier or result.get("cancelled"):
                    break
                if result.get("silence"):
                    break
                if not result.get("accepted"):
                    continue
                self.request_context_metadata = None
                context = (
                    self.transcript_context(result["text"])
                    if self.transcript_context is not None
                    else ""
                )
                if addressed(result.get("text", "")) is None:
                    continue
                self.remaining -= 1
                timing = result.get("timing", {})
                self.metrics = {**timing, "input_kind": "microphone"}
                for key in (
                    "speech_start_estimate",
                    "speech_end_estimate",
                    "vad_finalized",
                    "stt_started",
                    "stt_finished",
                ):
                    value = result.get(key, timing.get(key))
                    self.metrics[key] = value + self.audio_offset if value is not None else None
                if (
                    self.metrics["vad_finalized"] is not None
                    and self.metrics["stt_started"] is not None
                ):
                    self.metrics["stt_wait_s"] = (
                        self.metrics["stt_started"] - self.metrics["vad_finalized"]
                    )
                await self._respond(
                    result["text"],
                    identifier,
                    context=context,
                    context_metadata=self.request_context_metadata,
                )
                # Remain inhibited until playback ended, then acoustic hold, new stream/VAD/SoXR.
                await asyncio.sleep(self.config.voice.acoustic_delay)
                self.metrics["rearm_eligible"] = time.perf_counter()
                if self.results and self.results[-1].get("turn") == identifier:
                    self.results[-1]["rearm_eligible"] = self.metrics["rearm_eligible"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, LoopError) else "voice_session_failed"
            self.state = "error"
        finally:
            self.armed = False
            self.microphone, self.level = "closed", 0
            self.turn = ""
            if self.state not in {"error", "stopping"}:
                self.state = "paused"

    async def control(self, action: str) -> None:
        async with self.control_lock:
            if self.closed:
                return
            if action == "resume":
                if self.microphone == "closure_unverified":
                    raise LoopError("audio_shutdown_unverified")
                if self.armed or (self.task is not None and not self.task.done()):
                    return
                if not self.ready:
                    if getattr(self.tts, "closed", False):
                        self.tts = TTSClient.for_config(self.path, self.config.tts)
                    await self.start()
                self.output_device = (await self.audio.call("check", timeout=25))["device"]
                self.error = None
                self.armed = True
                self.deadline = time.perf_counter() + self.config.voice.arm_seconds
                self.remaining = self.config.voice.arm_turns
                self.task = asyncio.create_task(self._listen_loop())
                return
            if action not in {"pause", "cancel", "clear", "stop"}:
                raise LoopError("unknown_control")
            self.armed = False  # A finally can never re-arm an intentional pause.
            old_turn, self.turn = self.turn, ""
            self.state = "stopping" if action == "stop" else "paused"
            task = self.task
            if task is not None and not task.done() and not task.cancelling():
                task.cancel()  # Before yielding to invalidated producers or their finalizer.
            self.cancel_tts.set()
            operations = [self._abort_audio(old_turn)]
            if task is None and self.chat.active is not None:
                operations.append(self.chat.active.cancel())
            aborted = await asyncio.gather(*operations, return_exceptions=True)
            audio_closed = not isinstance(aborted[0], BaseException)
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), self.config.tts.drain_timeout + 5)
                except TimeoutError:
                    # Do not abandon a still-finalizing task or let Resume start another turn.
                    await asyncio.gather(
                        self.audio.close(), self.tts.close(), return_exceptions=True
                    )
                    done, _ = await asyncio.wait({task}, timeout=4)
                    if not done:
                        self.ready = False
                        self.state, self.error = "error", "turn_shutdown_unverified"
                        raise LoopError(self.error) from None
                    with contextlib.suppress(
                        asyncio.CancelledError, LoopError, TTSError, ChatError
                    ):
                        task.result()
                except (asyncio.CancelledError, LoopError, TTSError, ChatError):
                    pass
                self.task = None
            self.microphone, self.level = "closed", 0
            if not self.audio.ready or not self.tts.ready:
                self.ready = False
            if action == "clear":
                await self.chat.reset()
                self.session = uuid.uuid4().hex
                self.audio.session = self.session
                if self.audio.ready:
                    await self.audio.call("reset")
                self.accepted = self.answer = ""
                self.metrics, self.results = {}, []
                self.completed = []
                self.error = None
            if action == "stop":
                stopped = await asyncio.gather(
                    self.chat.close(), self.audio.close(), self.tts.close(), return_exceptions=True
                )
                self.shutdown_verified = not any(
                    isinstance(value, BaseException) for value in stopped
                )
                if not self.shutdown_verified:
                    self.error = "owned_worker_shutdown_unverified"
                self.closed = True
            if not audio_closed and not self.shutdown_verified:
                self.microphone, self.state = "closure_unverified", "error"
                self.error, self.ready = "audio_shutdown_unverified", False

    async def test_text(self, text: str, *, no_play: bool) -> None:
        if addressed(text) in (None, ""):
            raise LoopError("test_requires_explicit_jarvis_request")
        self.turn = uuid.uuid4().hex
        self.metrics = {"input_kind": "explicit_synthetic_text", "microphone": "NOT_RUN"}
        try:
            await self._respond(text, self.turn, no_play=no_play)
        finally:
            self.turn = ""
            self.state = "paused" if self.error is None else "error"


async def run_command(
    config: Config, path: Path, *, text: str | None, no_play: bool, arm: bool, report: Path | None
) -> int:
    instance = Instance()  # Before credentials, port, workers or capture.
    log: RuntimeLog | None = None
    try:
        log = RuntimeLog()
        return await _run_owned(
            config,
            path,
            text=text,
            no_play=no_play,
            arm=arm,
            report=report,
            instance=instance,
            log=log,
        )
    finally:
        if log is not None:
            log.close()
        instance.close()


async def _run_owned(
    config: Config,
    path: Path,
    *,
    text: str | None,
    no_play: bool,
    arm: bool,
    report: Path | None,
    instance: Instance,
    log: RuntimeLog,
) -> int:
    from jarvis_office.assets import atomic_json, private_root
    from jarvis_office.config import ConfigError
    from jarvis_office.credentials import load_key
    from jarvis_office.local_ui import LocalUI

    if (no_play and text is None) or (arm and text is not None):
        raise ConfigError("invalid_voice_test_arguments")
    if report is not None and (
        report.exists()
        or report.is_symlink()
        or not report.resolve().is_relative_to(private_root().resolve() / "reports")
    ):
        raise ConfigError("voice_report_requires_new_private_path")
    chat = DeepSeek(load_key(), config.chat)
    voice = VoiceLoop(config, path, chat)
    instance.publish(voice.session, config.voice.port)
    ui = LocalUI(voice, config.voice.port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    code = 0
    try:
        # Bind before loading engines: never kill another owner or warm on collision.
        await ui.start()
        print("Jarvis Office — démarrage en pause : " + ui.origin, flush=True)
        boot = asyncio.create_task(voice.start())
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({boot, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                boot.cancel()
                await asyncio.gather(boot, return_exceptions=True)
                code = 130
                return 130
            await boot
            devices = await voice.audio.call("preflight", timeout=25)
            voice.input_device = devices.get("input", {})
            voice.output_device = devices.get("output", {})
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
        if text is not None:
            trial = asyncio.create_task(voice.test_text(text, no_play=no_play))
            voice.task = trial
            stopping = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({trial, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    await voice.control("pause")
                    code = 130
                else:
                    await trial
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
        else:
            if arm:
                print(
                    "Essai micro armé explicitement ; STT local, durée et tours bornés.", flush=True
                )
                await voice.control("resume")
            while not stop.is_set() and not voice.closed:
                if instance.record["session"] != voice.session:
                    instance.publish(voice.session, config.voice.port)
                log.update(voice.snapshot())
                if arm and voice.task is not None and voice.task.done():
                    break
                await asyncio.sleep(0.1)
            if voice.error:
                code = 1
    except Exception as exc:
        voice.error = (
            str(exc) if isinstance(exc, (LoopError, TTSError, ChatError)) else "voice_run_failed"
        )
        code = 1
    finally:
        audio_runtime = dict(voice.audio.ready)
        tts_runtime = dict(voice.tts.ready)
        await voice.control("stop")
        await ui.close()
        log.update(voice.snapshot())
        if not voice.shutdown_verified:
            code = 1
        if report is not None:
            await asyncio.to_thread(
                atomic_json,
                report,
                {
                    "phase": 6,
                    "status": "PASS" if code == 0 else "FAIL",
                    "error": voice.error,
                    "conditions": "explicit_synthetic_text"
                    if text is not None
                    else "bounded_microphone_if_armed",
                    "turns": voice.results,
                    "input_device": voice.input_device,
                    "output_device": voice.output_device,
                    "audio_worker": audio_runtime,
                    "tts_worker": tts_runtime,
                    "qualification": "PROVISIONAL_NO_ACCEPTABLE_STT",
                    "voice_identity": "NOT_RUN",
                    "microphone_closed": voice.microphone == "closed",
                    "owned_workers_stopped": voice.shutdown_verified,
                },
            )
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    return code
