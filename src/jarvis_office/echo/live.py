"""Session adapter around the one existing VoiceLoop, never an independent voice engine."""

import asyncio
import contextlib
import functools
import json
import re
import signal
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed

from jarvis_office.assets import atomic_json, fingerprint, private_root
from jarvis_office.audio_client import LoopError
from jarvis_office.config import ConfigError, load_config
from jarvis_office.credentials import load_key, reserve_validation_request
from jarvis_office.deepseek import ChatError, DeepSeek
from jarvis_office.runtime import Instance
from jarvis_office.storage import StorageError
from jarvis_office.voice import VoiceLoop, addressed

from .audio import RemoteEchoAudio, RemoteEchoEgress
from .gateway import EchoGateway, Session, Settings
from .protocol import CAPTURE_MODES, ECHO_MODES, Packet


def historical_reports() -> list[dict[str, Any]]:
    """Fixed Office catalogue. No path parameter, transcripts, artifact links or arbitrary disk."""
    catalogue = []
    for name, relative, keys in (
        (
            "ECHO-02C — dialogues live",
            "echo-02c/qualification-final.json",
            (
                "ECHO_02C_FINAL",
                "REMOTE_STT_QUALIFIED",
                "SECURITY_STATUS",
                "RELEASE_STATUS",
                "PASSIVE_BEHAVIOR_OK",
                "DEEPSEEK_BUDGET_FINAL",
                "SERVER_HEAD",
                "ANDROID_HEAD",
            ),
        ),
        (
            "ECHO-02D — limites de lecture",
            "echo-02d/playback-jitter-report.json",
            ("phase", "updated_utc", "verdict", "latency_qualified", "remote_stt_qualified"),
        ),
    ):
        path = private_root() / relative
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text())
            catalogue.append(
                {
                    "name": name,
                    "sha256": fingerprint(path)["sha256"],
                    "restricted": True,
                    "summary": {
                        key: data[key]
                        for key in keys
                        if key in data and isinstance(data[key], (str, int, bool))
                    },
                }
            )
        except (OSError, ValueError):
            catalogue.append({"name": name, "error": "REPORT_UNAVAILABLE"})
    return catalogue


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
        self.store: Any = None
        self.server_epoch = uuid.uuid4().hex
        self.remote.server_epoch = self.server_epoch
        self.event_id = 0
        self.last_product: dict[str, str] = {}
        self.command_lock = asyncio.Lock()
        self.record_owner: tuple[str, str, str, int] | None = None
        self.diagnostic_session: str | None = None
        self.memory_task: asyncio.Task[None] | None = None
        self.tv: Any = None
        self.memory_status: dict[str, Any] = {
            "state": "disabled",
            "pending_turns": 0,
            "summary_present": False,
            "commits": 0,
            "last_error": None,
            "updated_at": None,
        }
        voice.transcript_context = self.transcript_context
        voice.on_turn_finished = self.record_turn

    def record_turn(
        self, turn: str, user: str, answer: str, delivered: str, metrics: dict[str, Any]
    ) -> None:
        owner = self.record_owner
        if self.store is None or owner is None or owner[2] != turn:
            return
        if metrics.get("llm", {}).get("path") == "tv_dispatcher":
            self.record_owner = None
            return
        status = {"PASS": "complete", "CANCELLED": "interrupted"}.get(
            metrics.get("status", ""), "error"
        )
        # Remote playback acknowledges a whole stream; a partial stream is never "heard".
        try:
            recorded = self.store.record_turn(
                owner[0],
                owner[1],
                turn,
                generation=owner[3],
                user_text=user,
                assistant_text=answer,
                delivered_text=delivered,
                status=status,
                metrics=metrics,
            )
            proof = metrics.get("llm", {}).get("request_context", {})
            if (
                recorded
                and status in {"complete", "interrupted"}
                and delivered
                and not proof.get("payload_includes_passive_context", False)
                and hasattr(self.store, "memory_context")
            ):
                self.schedule_memory_rollup(owner[0])
        except (StorageError, TypeError, ValueError):
            # Persistence failure is visible and prevents new arming, never breaks audio cleanup.
            self.store.error = "STORAGE_WRITE_FAILED"
        self.record_owner = None

    async def load_memory(self, device: str) -> None:
        """Restore bounded persistent context for the selected device."""
        if self.store is None or not hasattr(self.store, "memory_context"):
            return
        await self.store.flush()
        memory = await self.store.memory_context(device)
        history = [(item["user_text"], item["delivered_text"]) for item in memory["pending"]]
        self.voice.chat.restore_memory(memory["summary"], history)
        self.memory_status.update(
            state="idle" if memory["enabled"] else "disabled",
            pending_turns=memory["pending_count"],
            summary_present=bool(memory["summary"]),
            last_error=None,
            updated_at=memory["updated_at"],
        )

    def schedule_memory_rollup(self, device: str) -> None:
        """Replace an obsolete background rollup with one fresh bounded attempt."""
        previous = self.memory_task
        if previous is not None and not previous.done():
            previous.cancel()

        async def run() -> None:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            await self.rollup_memory(device)

        self.memory_task = asyncio.create_task(run(), name="office-memory-rollup")

    async def rollup_memory(self, device: str) -> None:
        """Summarize eligible persisted turns once; stale commits are rejected by storage."""
        try:
            await self.store.flush()
            memory = await self.store.memory_context(device)
            self.memory_status.update(
                state="summarizing" if memory["should_summarize"] else "idle",
                pending_turns=memory["pending_count"],
                summary_present=bool(memory["summary"]),
                last_error=None,
                updated_at=memory["updated_at"],
            )
            if not memory["should_summarize"] or not memory["rollup"]:
                return
            summary = await self.voice.chat.summarize_memory(memory["summary"], memory["rollup"])
            cursor = memory["rollup"][-1]
            committed = await self.store.commit_memory(
                device,
                generation=memory["generation"],
                revision=memory["revision"],
                through_created_at=cursor["created_at"],
                through_turn_id=cursor["turn_id"],
                summary=summary,
            )
            if committed:
                current = await self.store.memory_context(device)
                self.memory_status.update(
                    state="idle",
                    pending_turns=current["pending_count"],
                    summary_present=True,
                    commits=self.memory_status["commits"] + 1,
                    updated_at=current["updated_at"],
                )
                if self.remote.bound is not None and self.remote.bound.device == device:
                    self.voice.chat.restore_memory(
                        current["summary"],
                        [
                            (item["user_text"], item["delivered_text"])
                            for item in current["pending"]
                        ],
                    )
        except asyncio.CancelledError:
            self.memory_status["state"] = "idle"
            raise
        except (ChatError, StorageError):
            self.memory_status.update(state="error", last_error="MEMORY_ROLLUP_FAILED")

    async def cancel_memory_rollup(self) -> None:
        """Cancel and drain the owned rollup task."""
        task = self.memory_task
        if task is not None and not task.done():
            task.cancel()
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    def product_state(self, s: Session) -> dict[str, Any]:
        fresh = s.alive and time.monotonic() - s.physical_at < 3
        activity = {
            "listening": "LISTENING",
            "transcribing": "TRANSCRIBING",
            "responding": "GENERATING",
            "speaking": "GENERATING",
            "error": "ERROR",
        }.get(self.voice.state, "IDLE")
        if s.mode == "OFF":
            activity = "IDLE"
        if fresh and s.playing:
            activity = "PLAYING"
        error = s.last_error or self.voice.error
        if error:
            activity = "ERROR"
        return {
            "schema_version": 1,
            "server_epoch": self.server_epoch,
            "event_id": self.event_id,
            "device_id": s.device,
            "session_id": s.id,
            "turn_id": s.current_turn,
            "stream_id": s.down_stream,
            "generation": s.generation,
            "command_id": s.command_id,
            "connection": "DISCONNECTED"
            if not s.alive
            else ("CONNECTED" if s.audio and fresh else "DEGRADED"),
            "requested_mode": s.requested_mode,
            "confirmed_mode": s.mode,
            "activity": activity,
            "orb_style": getattr(self.store, "orb_style", "auto"),
            "physical": {
                "microphone": s.microphone if fresh else None,
                "playing": s.playing if fresh else None,
                "playback_frames": s.playback_frames if fresh else None,
            },
            "error": self.safe_error(error),
            "capabilities": sorted(s.capabilities),
            "rtt_ms": s.metrics.get("rtt_ms"),
            "acoustic_latency_ms": None,
            "context_count": s.context.count,
        }

    @staticmethod
    def safe_error(error: object) -> str | None:
        if not error:
            return None
        return str(error) if re.fullmatch(r"[A-Za-z_]{1,80}", str(error)) else "PRODUCT_ERROR"

    async def publish_state(self, s: Session, *, force: bool = False) -> dict[str, Any]:
        value = self.product_state(s)
        signature = json.dumps({k: v for k, v in value.items() if k != "event_id"}, sort_keys=True)
        if force or self.last_product.get(s.id) != signature:
            self.event_id += 1
            value["event_id"] = self.event_id
            self.last_product = {s.id: signature}
            if "private_state_v1" in s.capabilities:
                await s.send("product_state", value)
        return value

    async def dashboard_state(self) -> dict[str, Any]:
        preferences = await self.store.settings()
        try:
            raw_budget = json.loads(Path(self.settings.api_budget_file).read_text())
            diagnostic = {
                "used": raw_budget["attempts"],
                "limit": raw_budget["limit"],
                "source": "ECHO-02C diagnostic",
            }
        except (OSError, KeyError, ValueError):
            diagnostic = {"used": None, "limit": None, "source": "Non mesuré"}
        return {
            "schema_version": 1,
            "server_epoch": self.server_epoch,
            "event_id": self.event_id,
            "devices": [self.product_state(s) for s in self.sessions.values()],
            "session": self.voice.session,
            "state": self.voice.state,
            "armed": self.voice.armed,
            "microphone": self.voice.microphone,
            "conversation_ready": self.voice.ready,
            "output_verified": any(s.audio is not None for s in self.sessions.values()),
            "error": self.safe_error(self.voice.error),
            "budget": {"nominal": preferences.get("budget", {}), "diagnostic": diagnostic},
            "memory": {
                **self.memory_status,
                "enabled": preferences.get("memory_enabled", False),
                "budget_source": "nominal",
            },
            "storage": self.store.status(),
            "settings": preferences,
            "versions": {
                "server": "0.3.0-private",
                "protocol": 1,
                "schema": 1,
                "security": self.settings.security,
            },
            "alerts": ["KNOWN_STT_LIMITATIONS", "KNOWN_PLAYBACK_LIMITATIONS"],
            "engines_ready": self.voice.ready,
            "metrics": self.voice.metrics,
            "last_turn": self.voice.results[-1] if self.voice.results else None,
            "tv": self.tv.snapshot()
            if self.tv is not None
            else {"configured": False, "enabled": False, "devices": []},
        }

    async def dashboard_context(self, device: str) -> dict[str, Any]:
        buffer = self.contexts.get(device)
        settings = await self.store.settings()
        return {
            "entries": buffer.snapshot(device) if buffer else [],
            "limits": {
                "seconds": self.settings.context_seconds,
                "chars": self.settings.context_chars,
                "utterances": self.settings.context_utterances,
            },
            "archive_enabled": settings.get("archive_passive", False),
            "next_turn_max_chars": 3500,
        }

    async def dashboard_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        s = self.sessions.get(payload.get("device_id", ""))
        if s is None or not s.alive:
            return {"status": "rejected", "error": "DEVICE_DISCONNECTED"}
        action = payload.get("action")
        if action == "passive_smoke":
            if s.mode != "OFF" or s.down_stream:
                return {"status": "rejected", "error": "AUDIO_SESSION_BUSY"}
            try:
                diagnostic = json.loads(Path(self.settings.api_budget_file).read_text())
                if diagnostic["attempts"] >= diagnostic["limit"]:
                    return {"status": "rejected", "error": "DIAGNOSTIC_BUDGET_EXHAUSTED"}
            except (OSError, KeyError, ValueError):
                return {"status": "rejected", "error": "DIAGNOSTIC_BUDGET_UNAVAILABLE"}
            async with self.command_lock:
                await self.product_command(
                    s,
                    {
                        "type": "set_mode",
                        "payload": {
                            "mode": "PASSIVE",
                            "command_id": payload.get("command_id"),
                        },
                    },
                    time.monotonic_ns(),
                    diagnostic=True,
                    surface="dashboard",
                )
                self.voice.remaining = 1
            return s.command_acks[payload["command_id"]]
        if action == "preview_voice":
            if s.mode != "OFF" or s.down_stream or (s.turn_task and not s.turn_task.done()):
                return {"status": "rejected", "error": "AUDIO_SESSION_BUSY"}
            if not self.voice.ready or self.voice.armed:
                return {"status": "rejected", "error": "SERVER_NOT_READY"}
            s.turn_task = asyncio.create_task(self.preview_voice(s))
            return {"status": "applied", "error": None, "command_id": payload.get("command_id")}
        if action not in {"set_mode", "interrupt", "clear_context", "clear_memory"}:
            return {"status": "rejected", "error": "INVALID_COMMAND"}
        command_id = payload.get("command_id", "")
        await self.command(
            s,
            {
                "type": action,
                "payload": {
                    "mode": payload.get("mode"),
                    "command_id": command_id,
                },
            },
            time.monotonic_ns(),
            surface="dashboard",
        )
        return s.command_acks.get(command_id, {"status": "rejected", "error": "INVALID_COMMAND"})

    async def preview_voice(self, s: Session) -> None:
        """Fixed owner-requested local audition. Capture stays OFF, no cloud request."""
        cancel = asyncio.Event()
        output = RemoteEchoEgress(
            s, self.settings, uuid.uuid4().hex, self.voice.tts.ready["sample_rate"], audition=True
        )
        try:
            async with asyncio.timeout(60):
                await output.start()
                async with contextlib.aclosing(
                    self.voice.tts.stream(
                        "Bonjour. Je suis Jarvis. Le microphone est coupé. "
                        "Cette voix est produite localement.",
                        cancel=cancel,
                    )
                ) as stream:
                    async for pcm in stream:
                        for offset in range(0, len(pcm), 24000):
                            chunk = pcm[offset : offset + 24000]
                            while output.progress(output.turn)["free_source_bytes"] < len(chunk):
                                await asyncio.sleep(0.02)
                            await output.feed(output.turn, chunk)
                await output.finish(output.turn)
                await s.playback_drained.wait()
                await output.drained(output.turn)
        except Exception:
            s.last_error = "LOCAL_VOICE_PREVIEW_FAILED"
        finally:
            cancel.set()
            await output.abort()
            s.current_turn = ""
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                await s.send("stop_audio", {})
                await self.publish_state(s, force=True)

    def ingress(self, session: Session, packet: Packet, received: int) -> None:
        try:
            self.remote.feed(session, packet, received)
        except LoopError:
            raise ValueError("AUDIO_UPLINK_OVERRUN") from None

    def transcript_context(self, text: str) -> str:
        s = self.remote.bound
        if s is None or not s.alive or s.mode == "OFF" or s.down_stream:
            self.voice.request_context_metadata = {"selection_reason": "INACTIVE_OR_PLAYING"}
            self.remote.context_observed("INACTIVE_OR_PLAYING", len(text))
            return ""
        self.transcriptions += 1
        if addressed(text) is None and (s.mode not in {"ACTIVE", "COMMAND"} or not text.strip()):
            self.non_addressed += 1
            if s.mode == "PASSIVE" and s.context_epoch == self.remote.listen_context_epoch:
                entry_id = s.context.add(text)
                self.remote.context_observed("APPENDED", len(text), entry_id=entry_id)
                self.passive_appends += 1
                if self.store is not None:
                    self.store.record_passive(
                        s.device,
                        s.id,
                        generation=self.remote.listen_storage_generation,
                        text=text,
                        source="echo",
                    )
            else:
                self.remote.context_observed("MODE_OR_GENERATION_EXCLUDED", len(text))
            return ""
        if self.memory_task is not None and not self.memory_task.done():
            self.memory_task.cancel()
        command = s.mode == "COMMAND"
        if self.store is not None and not command:
            self.record_owner = (
                s.device,
                s.id,
                self.voice.turn,
                self.remote.listen_storage_generation,
            )
            self.store.record_turn(
                s.device,
                s.id,
                self.voice.turn,
                generation=self.record_owner[3],
                user_text=text,
                status="partial",
            )
        context, selection = (
            ("", {"selection_reason": "EMPTY"})
            if command
            else s.context.select(max_chars=3500, max_utterances=20)
        )
        self.voice.request_context_metadata = {
            **selection,
            "server_epoch": self.server_epoch,
            "echo_connection_session": s.id,
            "generation": s.generation,
            "context_generation": s.context_epoch,
            "archive_generation": self.remote.listen_storage_generation,
        }
        if command:
            self.voice.request_context_metadata["command_session"] = self.voice.session
        else:
            self.voice.request_context_metadata["conversation_session"] = self.voice.session
        self.remote.context_observed("ADDRESSED", len(text))
        return context

    async def command(
        self, s: Session, message: dict[str, Any], received_ns: int, *, surface: str = "echo"
    ) -> None:
        kind, payload = message["type"], message["payload"]
        if kind == "set_orb_style":
            command_id = payload.get("command_id")
            if (
                not isinstance(command_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", command_id)
                or payload.keys() != {"command_id", "orb_style"}
            ):
                raise ValueError("INVALID_APPEARANCE_COMMAND")
            async with self.command_lock:
                error = None
                try:
                    if self.store is None:
                        raise StorageError("STORAGE_UNAVAILABLE")
                    await self.store.update_settings({"orb_style": payload["orb_style"]})
                except StorageError:
                    error = "APPEARANCE_NOT_SAVED"
                await s.send(
                    "orb_style_ack",
                    {
                        "command_id": command_id,
                        "status": "rejected" if error else "applied",
                        "error": error,
                    },
                )
                await self.publish_state(s, force=True)
            return
        if kind == "client_state":
            microphone, playing = payload.get("microphone"), payload.get("playing")
            frames, stream = payload.get("playback_frames"), payload.get("stream_id", 0)
            if type(microphone) is not bool or type(playing) is not bool:
                raise ValueError("INVALID_PHYSICAL_STATE")
            if microphone and (s.mode == "OFF" or stream not in {s.up_stream, s.down_stream}):
                await s.send("stop_audio", {})
                return
            if playing and (not s.down_stream or stream != s.down_stream):
                return
            if frames is not None and (type(frames) is not int or not 0 <= frames < 2**53):
                raise ValueError("INVALID_PLAYBACK_POSITION")
            s.microphone, s.playing = microphone, playing
            s.playback_frames = frames if playing else None
            s.physical_stream, s.physical_at = stream, time.monotonic()
            return
        if kind in {"set_mode", "interrupt", "clear_context", "clear_memory"} and (
            self.settings.private_product or "command_id" in payload
        ):
            async with self.command_lock:
                await self.product_command(s, message, received_ns, surface=surface)
            return
        await self.legacy_command(s, message, received_ns)

    async def product_command(
        self,
        s: Session,
        message: dict[str, Any],
        received_ns: int,
        *,
        diagnostic: bool = False,
        surface: str = "internal",
    ) -> None:
        kind, payload = message["type"], message["payload"]
        command_id = payload.get("command_id")
        if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", command_id):
            raise ValueError("INVALID_COMMAND_ID")
        audit = {
            "surface": surface,
            "command_id": command_id,
            "action": kind,
            "server_epoch": self.server_epoch,
            "echo_connection_session": s.id,
            "conversation_session": self.voice.session,
            "turn_id": self.voice.turn if self.remote.bound is s and self.voice.turn else None,
            "generation_before": s.generation,
            "context_generation_before": s.context_epoch,
            "clock": "server_monotonic_ns",
            "received_ns": received_ns,
            "processing_ns": time.monotonic_ns(),
            "mode": payload.get("mode")
            if kind == "set_mode" and payload.get("mode") in ECHO_MODES
            else "OFF"
            if kind != "set_mode"
            else "INVALID",
        }
        if command_id in s.command_acks:
            await self.command_ack(s, s.command_acks[command_id], audit, deduplicated=True)
            return
        error = None
        mode = payload.get("mode") if kind == "set_mode" else "OFF"
        if mode not in ECHO_MODES:
            error = "INVALID_MODE"
        elif mode != "OFF":
            if not self.boot_complete:
                error = "SERVER_NOT_READY"
            elif s.current_turn or not s.audio or self.owner not in {None, s.id}:
                error = "AUDIO_SESSION_BUSY"
            elif self.store is not None:
                preferences = await self.store.settings()
                budget = preferences.get("budget", {})
                if self.store.error:
                    error = "STORAGE_UNAVAILABLE"
                elif not diagnostic and budget.get("limit") is None:
                    error = "NOMINAL_BUDGET_REQUIRED"
                elif not diagnostic and budget.get("used", 0) >= budget["limit"]:
                    error = "NOMINAL_BUDGET_EXHAUSTED"
        if error is None:
            self.diagnostic_session = s.id if diagnostic else None
            s.requested_mode = mode
            s.command_id = command_id
            s.generation += 1
            s.last_error = ""
            await self.publish_state(s, force=True)
            try:
                if kind in {"clear_context", "clear_memory", "interrupt"}:
                    await self.stop_audio(s)
                    if kind == "clear_context":
                        s.context.clear()
                        s.context_epoch += 1
                        await self.voice.control("clear")
                        await s.send("context_state", {"count": 0})
                    elif kind == "clear_memory":
                        if self.store is None:
                            raise StorageError("STORAGE_UNAVAILABLE")
                        await self.cancel_memory_rollup()
                        await self.voice.chat.reset()
                        await self.store.clear_memory(s.device)
                        self.memory_status.update(
                            state="idle",
                            pending_turns=0,
                            summary_present=False,
                            last_error=None,
                            updated_at=None,
                        )
                    await s.send("state", s.snapshot())
                else:
                    await self.legacy_command(s, message, received_ns)
                if s.mode != mode:
                    error = "MODE_NOT_APPLIED"
            except (ChatError, LoopError, StorageError, ValueError, TimeoutError):
                error = "COMMAND_FAILED"
                s.requested_mode = "OFF"
                await self.stop_audio(s)
        if error is None and self.store is not None and hasattr(self.store, "remember_echo_mode"):
            persisted = (
                mode
                if kind == "set_mode" and not diagnostic
                else "OFF"
                if kind in {"clear_context", "clear_memory", "interrupt"}
                else None
            )
            if persisted is not None:
                await self.store.remember_echo_mode(s.device, persisted)
        ack = {
            "command_id": command_id,
            "status": "rejected" if error else "applied",
            "error": error,
        }
        s.command_acks[command_id] = ack
        audit["mode"] = mode if mode in ECHO_MODES else "INVALID"
        while len(s.command_acks) > 64:
            del s.command_acks[next(iter(s.command_acks))]
        await self.command_ack(s, ack, audit, deduplicated=False)
        await self.publish_state(s, force=True)

    async def on_audio_ready(self, s: Session) -> None:
        if (
            not self.boot_complete
            or s.mode != "OFF"
            or self.store is None
            or not hasattr(self.store, "echo_mode")
        ):
            return
        mode = await self.store.echo_mode(s.device)
        if mode not in CAPTURE_MODES:
            return
        await self.command(
            s,
            {
                "type": "set_mode",
                "payload": {"mode": mode, "command_id": uuid.uuid4().hex},
            },
            time.monotonic_ns(),
            surface="internal",
        )

    async def command_ack(
        self, s: Session, ack: dict[str, Any], audit: dict[str, Any], *, deduplicated: bool
    ) -> None:
        audit.update(
            status=ack["status"],
            error=ack["error"],
            deduplicated=deduplicated,
            requested_mode=s.requested_mode,
            confirmed_mode=s.mode,
            generation=s.generation,
            context_generation=s.context_epoch,
            conversation_session_after=self.voice.session,
            decision_ns=time.monotonic_ns(),
            ack_send_status="FAILED",
        )
        try:
            sent = await s.send("command_ack", ack)
            audit["ack_send_status"] = "SENT" if sent else "SKIPPED_INVALIDATED"
        finally:
            audit["ack_finished_ns"] = time.monotonic_ns()
            if self.store is not None:
                self.store.record_event(
                    "command", device_id=s.device, details=audit, diagnostic=True
                )

    async def legacy_command(self, s: Session, message: dict[str, Any], received_ns: int) -> None:
        mode = message["payload"].get("mode") if message["type"] == "set_mode" else None
        if mode in CAPTURE_MODES and not self.boot_complete:
            await s.send("error", {"code": "SERVER_NOT_READY"})
            return
        await super().command(s, message, received_ns)
        if mode in CAPTURE_MODES and s.mode == mode:
            if self.remote.bound is not s:
                await self.voice.control("clear")
                self.remote.bound = s
            if mode != "COMMAND":
                if self.diagnostic_session == s.id:
                    self.voice.chat.restore_memory("", [])
                elif self.store is not None:
                    await self.load_memory(s.device)
            await self.voice.control("resume")

    async def stop_audio(self, s: Session, *, notify: bool = True) -> None:
        s.mode = "OFF"  # Revoke the mode before awaiting worker drainage or renewal controls.
        if notify:
            await s.send("stop_audio", {})
        if self.remote.bound is s:
            await self.voice.control("pause")
        await super().stop_audio(s, notify=False)
        s.envelope_queue.clear()

    async def drop(self, s: Session) -> None:
        await super().drop(s)
        self.event_id += 1
        if self.store is not None:
            self.store.close_session(s.id)
        if self.remote.bound is s:
            await self.cancel_memory_rollup()
            self.remote.bound = None
            self.remote.listen_owner = None
            self.remote.token = ""
            await self.voice.chat.reset()

    async def close(self) -> None:
        """Drain memory work before closing sessions, engines and persistent storage."""
        await self.cancel_memory_rollup()
        await super().close()

    def report(self) -> dict[str, Any]:
        captures = [
            {key: dict(value) if key == "worker" else value for key, value in capture.items()}
            if len(json.dumps(capture, ensure_ascii=True)) <= 4096
            else {
                "server_epoch": capture["server_epoch"],
                "turn_id": capture["turn_id"],
                "diagnostic_truncated": True,
            }
            for capture in self.remote.capture_diagnostics
        ]
        return {
            "phase": "ECHO-02C",
            "server_epoch": self.server_epoch,
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
            "capture_diagnostics": captures,
            "capture_diagnostics_truncated": sum(
                bool(c.get("diagnostic_truncated")) for c in captures
            ),
            "capture_diagnostics_overwritten": self.remote.capture_diagnostics_overwritten,
            "capture_diagnostics_stale_events": self.remote.capture_diagnostics_stale_events,
            "storage_diagnostic_dropped": getattr(self.store, "diagnostic_dropped", 0),
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
                if self.settings.private_product:
                    await self.publish_state(s)
                # The visual sender is separate from PCM pacing; discard overflow, never audio.
                while s.envelope_queue:
                    stream, turn, offset, energy = s.envelope_queue.popleft()
                    if stream != s.down_stream or turn != s.current_turn:
                        continue
                    values = [energy]
                    while s.envelope_queue and len(values) < 25:
                        nxt = s.envelope_queue[0]
                        if nxt[:2] != (stream, turn) or nxt[2] != offset + len(values) * 960:
                            break
                        values.append(s.envelope_queue.popleft()[3])
                    await s.send(
                        "playback_envelope",
                        {
                            "stream_id": stream,
                            "turn_id": turn,
                            "offset_frames": offset,
                            "step_frames": 960,
                            "values": values,
                        },
                    )
                if s.id not in self.observed_sessions:
                    self.connections += 1
                    self.observed_sessions = {s.id}  # one live device, bounded metadata
                if self.remote.bound is s and s.mode != "OFF":
                    if (
                        self.voice.task
                        and self.voice.task.done()
                        and self.diagnostic_session != s.id
                    ):
                        # Keep each capture bounded, but renew while the authorized mode stays on.
                        remaining = self.voice.remaining
                        epoch = (s.id, s.up_stream, s.mode)
                        self.voice.error = None
                        try:
                            await self.voice.control("resume")
                        except LoopError:
                            if s.alive and s.mode != "OFF":
                                raise
                        if remaining > 0 and epoch == (s.id, s.up_stream, s.mode):
                            self.voice.remaining = remaining
                    if self.voice.error or (
                        self.diagnostic_session == s.id
                        and self.voice.task
                        and self.voice.task.done()
                    ):
                        internal = {
                            "surface": "internal",
                            "action": "set_mode",
                            "mode": "OFF",
                            "command_id": None,
                            "related_command_id": s.command_id or None,
                            "server_epoch": self.server_epoch,
                            "echo_connection_session": s.id,
                            "conversation_session": self.voice.session,
                            "turn_id": self.voice.turn or None,
                            "generation": s.generation,
                            "clock": "server_monotonic_ns",
                            "received_ns": time.monotonic_ns(),
                        }
                        s.last_error = self.voice.error or ""
                        s.requested_mode = "OFF"
                        await self.stop_audio(s)
                        if self.store is not None:
                            self.store.record_event(
                                "command",
                                device_id=s.device,
                                diagnostic=True,
                                details={
                                    **internal,
                                    "status": "applied",
                                    "confirmed_mode": s.mode,
                                    "decision_ns": time.monotonic_ns(),
                                    "ack_send_status": "NOT_ATTEMPTED",
                                },
                            )
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
        reserve_validation_request,
        path=budget_path,
        phase="ECHO-02C",
        limit=settings.api_request_limit,
    )
    # Existing Office instance lock also excludes a simultaneous local microphone conversation.
    instance = Instance()
    chat = DeepSeek(load_key(), config.chat, reserve_request=reserve)
    remote = RemoteEchoAudio(config, config_path, settings)
    voice = VoiceLoop(config, config_path, chat, audio=remote)
    remote.voice, remote.session = voice, voice.session
    gateway = LiveGateway(settings, voice, remote)
    store: Any = None
    dashboard: Any = None
    tv_hub: Any = None
    if settings.private_product:
        from jarvis_office.dashboard import DashboardServer
        from jarvis_office.storage import OfficeStore, StorageError

        store = OfficeStore(private_root() / "data" / "office.sqlite3")
        gateway.store = store
        voice.archive_generation = lambda: store.generation

        async def reserve_nominal() -> int:
            try:
                if remote.bound is not None and gateway.diagnostic_session == remote.bound.id:
                    return await asyncio.to_thread(reserve)
                return int((await store.consume_budget())["used"])
            except StorageError as exc:
                raise ConfigError(gateway.safe_error(str(exc)) or "STORAGE_UNAVAILABLE") from None

        chat.reserve_request = reserve_nominal
        dashboard = DashboardServer(
            store,
            snapshot=gateway.dashboard_state,
            command=gateway.dashboard_command,
            context=gateway.dashboard_context,
            tv_command=None,
            port=settings.dashboard_port,
            reports=historical_reports(),
        )
        if settings.config_path:
            from jarvis_office.tv import TvHub, TvSettings

            tv_settings = TvSettings.load(
                Path(settings.config_path),
                echo_bind=settings.bind,
                echo_port=settings.port,
                echo_cert=settings.cert,
                echo_key=settings.key,
            )
            if tv_settings is not None:

                def bump_tv() -> None:
                    gateway.event_id += 1

                tv_hub = TvHub(
                    tv_settings,
                    store,
                    chat=chat,
                    on_change=bump_tv,
                )
                gateway.tv = tv_hub
                voice.tv = tv_hub
                dashboard.tv_command = tv_hub.owner_command
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks: list[asyncio.Task[Any]] = []
    boot: asyncio.Task[None] | None = None
    try:
        if store is not None:
            await store.start()
            for device in settings.devices:
                store.register_device(device)
        if tv_hub is not None:
            await tv_hub.start()
        if dashboard is not None:
            await dashboard.start()
        async with await gateway.start():
            tasks = [
                asyncio.create_task(stop.wait()),
                asyncio.create_task(gateway.network_lost.wait()),
                asyncio.create_task(gateway.publish(report_path)),
            ]
            if dashboard:
                instance.record["dashboard"] = "private-v1"
                instance.record["server_epoch"] = gateway.server_epoch
            instance.publish(voice.session, settings.dashboard_port if dashboard else None)
            boot = asyncio.create_task(voice.start())
            await asyncio.wait([boot, *tasks], return_when=asyncio.FIRST_COMPLETED)
            if not boot.done():
                for task in tasks:
                    if task.done() and not task.cancelled():
                        task.result()
                return
            await boot
            gateway.boot_complete = True
            # A client can finish its audio handshake before the workers boot; replay
            # the persisted owner mode once the live pipeline is ready.
            for session in tuple(gateway.sessions.values()):
                if session.alive and session.audio is not None:
                    await gateway.on_audio_ready(session)
            print(f"ECHO_LIVE_READY {settings.security} REMOTE_STT_QUALIFIED=NO", flush=True)
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
            if tv_hub is not None:
                await tv_hub.close()
            if dashboard is not None:
                await dashboard.close()
            if store is not None:
                await store.close()
            instance.close()
