"""Conversation permit kernel. Not wired into the nominal runtime.

This module decides whether a turn may receive a conversational reply. It does
not own the microphone, STT, TTS, network or private storage. Simulated wake
and speech events exercise policy only; they are not acoustic recognition.

Future hook (lot 3, do not import this from VoiceLoop yet):
``VoiceLoop._listen_loop`` after an accepted utterance,
``LiveGateway.transcript_context`` for ambient collection,
``RemoteEchoEgress.drained`` after ``playback_drained``.

PROCESSING starts at ``wake(residual)`` or ``transcript_ready``. There is no
``processing_started`` refresh: duplicates must not extend technical holds.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

IDLE_SECONDS = 120.0
MAX_EVENT_LAG_SECONDS = 2.0
SPEECH_HOLD_SECONDS = 30.0
TRANSCRIBE_HOLD_SECONDS = 60.0
PROCESSING_HOLD_SECONDS = 180.0
PLAYBACK_HOLD_SECONDS = 180.0
_PLAYBACK_MEMORY = 16
_EVENT_MEMORY = 32


class CaptureState(StrEnum):
    OFF = "off"
    LISTENING = "listening"
    COMMAND = "command"


class TalkPermit(StrEnum):
    IDLE = "idle"
    OPEN = "open"


class TurnPhase(StrEnum):
    IDLE = "idle"
    SPEECH = "speech"
    TRANSCRIBING = "transcribing"
    PROCESSING = "processing"
    PLAYING = "playing"


@dataclass(frozen=True)
class Acquisition:
    """Identity captured when an event is prepared, not when it is delivered."""

    arm_id: str
    epoch: int


@dataclass(frozen=True)
class SessionDecision:
    allow_reply: bool
    collect_context: bool
    stop_capture: bool
    purge_context: bool
    ignored: bool
    silent_expire: bool
    abort_turn: bool
    reason: str
    residual: str
    capture: CaptureState
    permit: TalkPermit
    phase: TurnPhase
    conversation_id: str
    turn_id: str
    generation: int
    idle_deadline: float | None
    playback_id: str
    arm_id: str
    epoch: int
    abandoned_conversation_id: str
    abandoned_turn_id: str
    abandoned_generation: int
    abandoned_playback_id: str


class ConversationSession:
    """One conversation permit, one active reply turn, injectable monotonic clock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.capture = CaptureState.OFF
        self.permit = TalkPermit.IDLE
        self.phase = TurnPhase.IDLE
        self.conversation_id = ""
        self.turn_id = ""
        self.generation = 0
        self.residual = ""
        self.idle_deadline: float | None = None
        self.phase_deadline: float | None = None
        self.playing_playback_id = ""
        self.arm_id = ""
        self.epoch = 0
        self.epoch_started_at = 0.0
        self._idle_before_speech: float | None = None
        self._consumed: deque[str] = deque(maxlen=_PLAYBACK_MEMORY)
        # ponytail: 32 keys; an identical replay after eviction is accepted.
        self._seen: deque[tuple[str, int, str, float, str]] = deque(maxlen=_EVENT_MEMORY)

    def acquisition(self) -> Acquisition:
        return Acquisition(self.arm_id, self.epoch)

    def pending_timers(self) -> int:
        return 0

    def playback_memory(self) -> int:
        return len(self._consumed)

    def event_memory(self) -> int:
        return len(self._seen)

    def expire_due(self) -> SessionDecision:
        pending = self._sync()
        if pending is not None:
            return pending
        return self._decision(reason="no_due_deadline")

    def set_capture(self, mode: CaptureState | str) -> SessionDecision:
        capture = mode if isinstance(mode, CaptureState) else CaptureState(mode)
        if capture is CaptureState.OFF:
            return self.off()
        if capture is CaptureState.COMMAND:
            snapshot = self._turn_snapshot()
            self.capture = CaptureState.COMMAND
            self.arm_id = ""
            self._bump_epoch()
            self._invalidate()
            return self._decision(reason="command_mode", abort_turn=True, abandoned=snapshot)
        if self.capture is CaptureState.LISTENING:
            return self._decision(reason="listening")
        self.capture = CaptureState.LISTENING
        self._new_arm()
        return self._decision(reason="listening")

    def off(self) -> SessionDecision:
        snapshot = self._turn_snapshot()
        self.capture = CaptureState.OFF
        self.arm_id = ""
        self._bump_epoch()
        self._invalidate()
        return self._decision(reason="off", abort_turn=True, stop_capture=True, abandoned=snapshot)

    def cancel(self) -> SessionDecision:
        snapshot = self._turn_snapshot()
        self._bump_epoch()
        self._invalidate()
        return self._decision(reason="cancel", abort_turn=True, abandoned=snapshot)

    def clear(self) -> SessionDecision:
        snapshot = self._turn_snapshot()
        self._bump_epoch()
        self._invalidate()
        return self._decision(reason="clear", abort_turn=True, abandoned=snapshot)

    def reconnect(self) -> SessionDecision:
        snapshot = self._turn_snapshot()
        self._bump_epoch()
        self._invalidate()
        return self._decision(reason="reconnect", abort_turn=True, abandoned=snapshot)

    def wake(
        self,
        *,
        arm_id: str,
        epoch: int,
        at: float,
        residual: str = "",
        source: str = "simulated",
    ) -> SessionDecision:
        invalid = self._timestamp_error(at)
        pending = None if invalid else self._sync()
        if invalid:
            return self._decision(reason=invalid, ignored=True)
        if source == "self":
            return self._emit(pending, self._decision(reason="self_audio_ignored", ignored=True))
        if self.capture is CaptureState.OFF:
            return self._emit(pending, self._decision(reason="off_blocks_wake", ignored=True))
        if self.capture is CaptureState.COMMAND:
            return self._emit(pending, self._decision(reason="command_ignores_wake", ignored=True))
        origin = self._origin_error(arm_id, epoch, at)
        if origin:
            return self._emit(pending, self._decision(reason=origin, ignored=True))
        residual = residual.strip()
        key = (arm_id, epoch, "wake", at, residual)
        if self._duplicate(key):
            return self._emit(pending, self._decision(reason="duplicate_event", ignored=True))
        if self.phase in {TurnPhase.PROCESSING, TurnPhase.PLAYING}:
            return self._emit(pending, self._decision(reason="turn_in_progress", ignored=True))
        if self.phase in {TurnPhase.SPEECH, TurnPhase.TRANSCRIBING}:
            return self._emit(pending, self._decision(reason="speech_in_progress", ignored=True))
        if self.permit is TalkPermit.OPEN and not residual:
            return self._emit(pending, self._decision(reason="redundant_bare_wake", ignored=True))
        self._remember(key)
        if self.permit is not TalkPermit.OPEN:
            self.generation += 1
            self.conversation_id = uuid.uuid4().hex
            self._consumed.clear()
            self.idle_deadline = at + IDLE_SECONDS
        self.permit = TalkPermit.OPEN
        self.residual = residual
        if residual:
            self.turn_id = uuid.uuid4().hex
            self.phase = TurnPhase.PROCESSING
            self.phase_deadline = self._clock() + PROCESSING_HOLD_SECONDS
            self._idle_before_speech = self.idle_deadline
            return self._emit(pending, self._decision(reason="wake_with_request", allow_reply=True))
        self.turn_id = ""
        self.phase = TurnPhase.IDLE
        self.phase_deadline = None
        return self._emit(pending, self._decision(reason="wake_wait"))

    def speech_start(
        self, *, arm_id: str, epoch: int, at: float, source: str = "user"
    ) -> SessionDecision:
        invalid = self._timestamp_error(at)
        pending = None if invalid else self._sync()
        if invalid:
            return self._decision(reason=invalid, ignored=True)
        if source == "self":
            return self._emit(pending, self._decision(reason="self_audio_ignored", ignored=True))
        if self.capture is CaptureState.OFF:
            return self._emit(pending, self._decision(reason="off_blocks_speech", ignored=True))
        if self.capture is CaptureState.COMMAND:
            return self._emit(
                pending, self._decision(reason="command_ignores_speech", ignored=True)
            )
        origin = self._origin_error(arm_id, epoch, at)
        if origin:
            return self._emit(pending, self._decision(reason=origin, ignored=True))
        if self.permit is not TalkPermit.OPEN:
            return self._emit(pending, self._decision(reason="veille_no_permit"))
        if self.phase is not TurnPhase.IDLE:
            return self._emit(pending, self._decision(reason="turn_in_progress", ignored=True))
        key = (arm_id, epoch, "speech", at, "")
        if self._duplicate(key):
            return self._emit(pending, self._decision(reason="duplicate_event", ignored=True))
        if self.idle_deadline is not None and at >= self.idle_deadline:
            snapshot = self._turn_snapshot()
            self._bump_epoch()
            self._invalidate()
            return self._emit(
                pending,
                self._decision(reason="speech_after_deadline", abandoned=snapshot),
            )
        self._remember(key)
        self._idle_before_speech = self.idle_deadline
        self.turn_id = uuid.uuid4().hex
        self.phase = TurnPhase.SPEECH
        self.phase_deadline = self._clock() + SPEECH_HOLD_SECONDS
        self.residual = ""
        return self._emit(pending, self._decision(reason="speech_reserved"))

    def begin_transcribe(
        self, *, conversation_id: str, turn_id: str, generation: int
    ) -> SessionDecision:
        pending = self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        if self.phase is not TurnPhase.SPEECH:
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        self.phase = TurnPhase.TRANSCRIBING
        self.phase_deadline = self._clock() + TRANSCRIBE_HOLD_SECONDS
        return self._emit(pending, self._decision(reason="transcribing"))

    def speech_reject(
        self, *, conversation_id: str, turn_id: str, generation: int
    ) -> SessionDecision:
        pending = self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        if self.phase not in {TurnPhase.SPEECH, TurnPhase.TRANSCRIBING}:
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        snapshot = self._turn_snapshot()
        expired = self._release_turn()
        return self._emit(
            pending,
            self._decision(
                reason="silent_expire" if expired else "false_start",
                silent_expire=expired,
                abort_turn=True,
                abandoned=snapshot,
            ),
        )

    def transcript_ready(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        generation: int,
        text: str,
    ) -> SessionDecision:
        pending = self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        if self.phase not in {TurnPhase.SPEECH, TurnPhase.TRANSCRIBING}:
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        text = text.strip()
        if not text:
            snapshot = self._turn_snapshot()
            expired = self._release_turn()
            return self._emit(
                pending,
                self._decision(
                    reason="silent_expire" if expired else "empty_transcript",
                    silent_expire=expired,
                    abort_turn=True,
                    abandoned=snapshot,
                ),
            )
        self.residual = text
        self.phase = TurnPhase.PROCESSING
        self.phase_deadline = self._clock() + PROCESSING_HOLD_SECONDS
        return self._emit(pending, self._decision(reason="turn_accepted", allow_reply=True))

    def processing_failed(
        self, *, conversation_id: str, turn_id: str, generation: int
    ) -> SessionDecision:
        pending = self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        if self.phase not in {TurnPhase.PROCESSING, TurnPhase.PLAYING}:
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        snapshot = self._turn_snapshot()
        expired = self._release_turn()
        return self._emit(
            pending,
            self._decision(
                reason="silent_expire" if expired else "processing_failed",
                silent_expire=expired,
                abort_turn=True,
                abandoned=snapshot,
            ),
        )

    def playback_started(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        generation: int,
        playback_id: str,
    ) -> SessionDecision:
        pending = self._sync()
        if not playback_id or not self._match(conversation_id, turn_id, generation):
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        if self.phase is not TurnPhase.PROCESSING:
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        self.phase = TurnPhase.PLAYING
        self.phase_deadline = self._clock() + PLAYBACK_HOLD_SECONDS
        self.playing_playback_id = playback_id
        return self._emit(pending, self._decision(reason="playing", playback_id=playback_id))

    def note_llm_token(self) -> SessionDecision:
        pending = self._sync()
        return self._emit(pending, self._decision(reason="llm_token_ignored", ignored=True))

    def note_tts_segment(self) -> SessionDecision:
        pending = self._sync()
        return self._emit(pending, self._decision(reason="tts_segment_ignored", ignored=True))

    def playback_finished(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        generation: int,
        playback_id: str,
    ) -> SessionDecision:
        pending = self._sync()
        if (
            not playback_id
            or playback_id in self._consumed
            or playback_id != self.playing_playback_id
            or self.phase is not TurnPhase.PLAYING
            or not self._match(conversation_id, turn_id, generation)
        ):
            return self._emit(pending, self._decision(reason="stale_event", ignored=True))
        self._consumed.append(playback_id)
        self.playing_playback_id = ""
        self.turn_id = ""
        self.residual = ""
        self.phase = TurnPhase.IDLE
        self.phase_deadline = None
        self._idle_before_speech = None
        self.idle_deadline = self._clock() + IDLE_SECONDS
        return self._emit(
            pending, self._decision(reason="playback_finished", playback_id=playback_id)
        )

    def _timestamp_error(self, at: float) -> str:
        if type(at) is not float and type(at) is not int:
            return "invalid_timestamp"
        value = float(at)
        if not math.isfinite(value):
            return "invalid_timestamp"
        now = self._clock()
        if value > now:
            return "timestamp_in_future"
        if now - value > MAX_EVENT_LAG_SECONDS:
            return "event_too_late"
        return ""

    def _origin_error(self, arm_id: str, epoch: int, at: float) -> str:
        if arm_id != self.arm_id or epoch != self.epoch or not arm_id:
            return "stale_source"
        if at < self.epoch_started_at:
            return "timestamp_before_invalidation"
        return ""

    def _duplicate(self, key: tuple[str, int, str, float, str]) -> bool:
        return key in self._seen

    def _remember(self, key: tuple[str, int, str, float, str]) -> None:
        self._seen.append(key)

    def _new_arm(self) -> None:
        self.arm_id = uuid.uuid4().hex
        self._bump_epoch()

    def _bump_epoch(self) -> None:
        self.epoch += 1
        self.epoch_started_at = self._clock()

    def _turn_snapshot(self) -> tuple[str, str, int, str]:
        return (self.conversation_id, self.turn_id, self.generation, self.playing_playback_id)

    def _match(self, conversation_id: str, turn_id: str, generation: int) -> bool:
        return (
            generation == self.generation
            and conversation_id == self.conversation_id
            and turn_id == self.turn_id
            and bool(turn_id)
        )

    def _invalidate(self) -> None:
        self.generation += 1
        self.permit = TalkPermit.IDLE
        self.phase = TurnPhase.IDLE
        self.conversation_id = ""
        self.turn_id = ""
        self.residual = ""
        self.idle_deadline = None
        self.phase_deadline = None
        self.playing_playback_id = ""
        self._idle_before_speech = None
        self._consumed.clear()

    def _restore_idle(self) -> None:
        self.phase = TurnPhase.IDLE
        self.phase_deadline = None
        self.turn_id = ""
        self.residual = ""
        self.playing_playback_id = ""
        self.idle_deadline = self._idle_before_speech
        self._idle_before_speech = None
        self.generation += 1

    def _idle_passed(self) -> bool:
        return self.idle_deadline is not None and self._clock() >= self.idle_deadline

    def _idle_finalized(self) -> bool:
        return (
            self.idle_deadline is not None
            and self._clock() >= self.idle_deadline + MAX_EVENT_LAG_SECONDS
        )

    def _release_turn(self) -> bool:
        self._restore_idle()
        if self.permit is TalkPermit.OPEN and self._idle_passed():
            self._bump_epoch()
            self._invalidate()
            return True
        return False

    def _timeout_work(self) -> SessionDecision:
        snapshot = self._turn_snapshot()
        expired = self._release_turn()
        return self._decision(
            reason="turn_hold_timeout",
            abort_turn=True,
            silent_expire=expired,
            abandoned=snapshot,
        )

    def _sync(self) -> SessionDecision | None:
        now = self._clock()
        holding = {
            TurnPhase.SPEECH,
            TurnPhase.TRANSCRIBING,
            TurnPhase.PROCESSING,
            TurnPhase.PLAYING,
        }
        if self.phase in holding and self.phase_deadline is not None and now >= self.phase_deadline:
            return self._timeout_work()
        if (
            self.permit is TalkPermit.OPEN
            and self.phase is TurnPhase.IDLE
            and self._idle_finalized()
        ):
            self._bump_epoch()
            self._invalidate()
            return self._decision(reason="silent_expire", silent_expire=True)
        return None

    def _emit(self, pending: SessionDecision | None, decision: SessionDecision) -> SessionDecision:
        if pending is None:
            return decision
        if pending.abort_turn:
            reason = (
                pending.reason
                if decision.ignored
                or decision.reason in {"llm_token_ignored", "tts_segment_ignored", "stale_event"}
                else decision.reason
            )
            return replace(
                decision,
                abort_turn=True,
                silent_expire=decision.silent_expire or pending.silent_expire,
                reason=reason,
                abandoned_conversation_id=pending.abandoned_conversation_id,
                abandoned_turn_id=pending.abandoned_turn_id,
                abandoned_generation=pending.abandoned_generation,
                abandoned_playback_id=pending.abandoned_playback_id,
            )
        if pending.silent_expire:
            return replace(
                pending,
                ignored=decision.ignored or pending.ignored,
            )
        return decision

    def _decision(
        self,
        *,
        reason: str,
        allow_reply: bool = False,
        ignored: bool = False,
        silent_expire: bool = False,
        abort_turn: bool = False,
        stop_capture: bool = False,
        playback_id: str = "",
        abandoned: tuple[str, str, int, str] | None = None,
    ) -> SessionDecision:
        leftover = abandoned or ("", "", 0, "")
        return SessionDecision(
            allow_reply=allow_reply,
            collect_context=self.capture is CaptureState.LISTENING,
            stop_capture=stop_capture,
            purge_context=False,
            ignored=ignored,
            silent_expire=silent_expire,
            abort_turn=abort_turn,
            reason=reason,
            residual=self.residual,
            capture=self.capture,
            permit=self.permit,
            phase=self.phase,
            conversation_id=self.conversation_id,
            turn_id=self.turn_id,
            generation=self.generation,
            idle_deadline=self.idle_deadline,
            playback_id=playback_id or self.playing_playback_id,
            arm_id=self.arm_id,
            epoch=self.epoch,
            abandoned_conversation_id=leftover[0],
            abandoned_turn_id=leftover[1],
            abandoned_generation=leftover[2],
            abandoned_playback_id=leftover[3],
        )
