"""Session adapter around the one existing VoiceLoop, never an independent voice engine."""

import asyncio
import contextlib
import functools
import signal
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed

from jarvis_office.assets import atomic_json, private_root
from jarvis_office.audio_client import LoopError
from jarvis_office.config import load_config
from jarvis_office.credentials import load_key, reserve_validation_request
from jarvis_office.deepseek import DeepSeek
from jarvis_office.runtime import Instance
from jarvis_office.voice import VoiceLoop, addressed

from .audio import RemoteEchoAudio
from .gateway import EchoGateway, Session, Settings
from .protocol import Packet


class LiveGateway(EchoGateway):
    def __init__(self, settings: Settings, voice: VoiceLoop, audio: RemoteEchoAudio) -> None:
        super().__init__(settings, on_frame=self.ingress)
        self.voice, self.remote = voice, audio
        self.boot_complete = voice.ready
        self.started = time.monotonic()
        self.transcriptions = self.non_addressed = self.passive_appends = 0
        self.connections = 0
        self.observed_sessions: set[str] = set()
        self.last_ui: tuple[str, str, str, int] | None = None
        self.sent_answer = ""
        self.sent_turn = ""
        voice.transcript_context = self.transcript_context

    def ingress(self, session: Session, packet: Packet, received: int) -> None:
        try:
            self.remote.feed(session, packet, received)
        except LoopError:
            raise ValueError("AUDIO_UPLINK_OVERRUN") from None

    def transcript_context(self, text: str) -> str:
        s = self.remote.bound
        if s is None or not s.alive or s.mode == "OFF" or s.down_stream:
            return ""
        self.transcriptions += 1
        if addressed(text) is None:
            self.non_addressed += 1
            if s.mode == "PASSIVE" and s.context_epoch == self.remote.listen_context_epoch:
                s.context.add(text)
                self.passive_appends += 1
            return ""
        return s.context.recent(max_chars=3500, max_utterances=20)

    async def command(self, s: Session, message: dict[str, Any], received_ns: int) -> None:
        mode = message["payload"].get("mode") if message["type"] == "set_mode" else None
        if mode in {"ACTIVE", "PASSIVE"} and not self.boot_complete:
            await s.send("error", {"code": "SERVER_NOT_READY"})
            return
        await super().command(s, message, received_ns)
        if mode in {"ACTIVE", "PASSIVE"} and s.mode == mode:
            if self.remote.bound is not s:
                await self.voice.control("clear")
                self.remote.bound = s
            await self.voice.control("resume")

    async def stop_audio(self, s: Session, *, notify: bool = True) -> None:
        s.mode = "OFF"  # Revoke the mode before awaiting worker drainage or renewal controls.
        if notify:
            await s.send("stop_audio", {})
        if self.remote.bound is s:
            await self.voice.control("pause")
        await super().stop_audio(s, notify=False)

    async def drop(self, s: Session) -> None:
        await super().drop(s)
        if self.remote.bound is s:
            self.remote.bound = None
            self.remote.listen_owner = None
            self.remote.token = ""
            await self.voice.chat.reset()

    def report(self) -> dict[str, Any]:
        return {
            "phase": "ECHO-02C",
            "uptime_s": time.monotonic() - self.started,
            "security": self.settings.security,
            "network_path": self.settings.network_path,
            "network_available": not self.network_lost.is_set(),
            "remote_stt_qualified": "NO",
            "engines_ready": self.voice.ready,
            "voice_state": self.voice.state,
            "error": self.voice.error,
            "transcriptions": self.transcriptions,
            "non_addressed": self.non_addressed,
            "passive_appends": self.passive_appends,
            "connections": self.connections,
            "turns": self.voice.results,
            "current_metrics": self.voice.metrics,
            "sessions": [
                {**s.snapshot(), "metrics": s.metrics, "timings": s.timing_summary()}
                for s in self.sessions.values()
            ],
            "audio_worker_pid": self.remote.worker.process.pid
            if self.remote.worker.process
            else None,
        }

    async def publish(self, report: Path) -> None:
        last_report = 0.0
        while True:
            for s in list(self.sessions.values()):
                if not s.alive or not s.welcomed:
                    continue
                if s.id not in self.observed_sessions:
                    self.connections += 1
                    self.observed_sessions = {s.id}  # one live device, bounded metadata
                if self.remote.bound is s and s.mode != "OFF":
                    if (
                        not self.voice.error
                        and self.voice.task
                        and self.voice.task.done()
                        and self.voice.remaining > 0
                        and time.perf_counter() >= self.voice.deadline
                    ):
                        # Renew only an already authorized connection; keep the remaining turn cap.
                        remaining = self.voice.remaining
                        epoch = (s.id, s.up_stream, s.mode)
                        try:
                            await self.voice.control("resume")
                        except LoopError:
                            if s.alive and s.mode != "OFF":
                                raise
                        if epoch == (s.id, s.up_stream, s.mode):
                            self.voice.remaining = remaining
                    if self.voice.error or (self.voice.task and self.voice.task.done()):
                        s.last_error = self.voice.error or "ARM_WINDOW_ENDED"
                        await self.stop_audio(s)
                        await s.send("state", s.snapshot())
                runtime = (
                    "OFF"
                    if s.mode == "OFF"
                    else {
                        "listening": "LISTENING",
                        "speaking": "SPEAKING",
                        "error": "ERROR",
                    }.get(self.voice.state, "PROCESSING")
                )
                view = (s.id, runtime, self.voice.state, s.context.count)
                if view != self.last_ui:
                    await s.send(
                        "live_state",
                        {
                            "runtime": runtime,
                            "server": "JARVIS LIVE" if self.voice.ready else "DÉMARRAGE",
                        },
                    )
                    await s.send("context_state", {"count": s.context.count})
                    self.last_ui = view
                if s.current_turn != self.sent_turn:
                    self.sent_turn, self.sent_answer = s.current_turn, ""
                if (
                    s.current_turn
                    and s.current_turn == self.voice.turn
                    and self.voice.answer != self.sent_answer
                ):
                    delta = self.voice.answer[len(self.sent_answer) :]
                    if delta:
                        await s.send("response_delta", {"turn_id": s.current_turn, "text": delta})
                    self.sent_answer = self.voice.answer
            if time.monotonic() - last_report >= 2:
                atomic_json(report, self.report())
                last_report = time.monotonic()
            await asyncio.sleep(0.1)


async def run_live(settings: Settings) -> None:
    config_path, budget_path, report_path = map(
        Path,
        (
            settings.voice_config,
            settings.api_budget_file,
            settings.report_file,
        ),
    )
    for path in (config_path, budget_path, report_path):
        if not path.resolve().is_relative_to(private_root().resolve()) or path.is_symlink():
            raise ValueError("PRIVATE_LIVE_PATH_REQUIRED")
    config = load_config(config_path)
    if config.speech.input_rate != 16000:
        raise ValueError("FROZEN_AUDIO_PROFILE_REQUIRED")
    config = replace(
        config,
        voice=replace(
            config.voice,
            acoustic_delay=settings.acoustic_tail_ms / 1000,
        ),
    )
    reserve = functools.partial(
        reserve_validation_request, path=budget_path, phase="ECHO-02C", limit=8
    )
    # Existing Office instance lock also excludes a simultaneous local microphone conversation.
    instance = Instance()
    chat = DeepSeek(load_key(), config.chat, reserve_request=reserve)
    remote = RemoteEchoAudio(config, config_path, settings)
    voice = VoiceLoop(config, config_path, chat, audio=remote)
    remote.voice, remote.session = voice, voice.session
    gateway = LiveGateway(settings, voice, remote)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks: list[asyncio.Task[Any]] = []
    boot: asyncio.Task[None] | None = None
    try:
        async with await gateway.start():
            tasks = [
                asyncio.create_task(stop.wait()),
                asyncio.create_task(gateway.network_lost.wait()),
                asyncio.create_task(gateway.publish(report_path)),
            ]
            instance.publish(voice.session, None)
            boot = asyncio.create_task(voice.start())
            await asyncio.wait([boot, *tasks], return_when=asyncio.FIRST_COMPLETED)
            if not boot.done():
                for task in tasks:
                    if task.done() and not task.cancelled():
                        task.result()
                return
            await boot
            gateway.boot_complete = True
            print("ECHO_LIVE_READY DEV_INSECURE_LAN REMOTE_STT_QUALIFIED=NO", flush=True)
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                if task.done() and not task.cancelled():
                    task.result()
    finally:
        if boot is not None and not boot.done():
            boot.cancel()
            await asyncio.gather(boot, return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(ConnectionClosed):
            await gateway.close()
        try:
            await voice.control("stop")
            atomic_json(
                report_path,
                {**gateway.report(), "stopped": True, "shutdown_verified": voice.shutdown_verified},
            )
        finally:
            instance.close()
