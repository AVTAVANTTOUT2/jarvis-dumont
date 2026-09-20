"""Conversation permit kernel. Not wired into the nominal runtime.

This module decides whether a turn may receive a conversational reply. It does
not own the microphone, STT, TTS, network or private storage. Simulated wake
and speech events exercise policy only; they are not acoustic recognition.

Future hook (lot 3, do not import this from VoiceLoop yet):
``VoiceLoop._listen_loop`` after an accepted utterance,
``LiveGateway.transcript_context`` for ambient collection,
``RemoteEchoEgress.drained`` after ``playback_drained``.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

IDLE_SECONDS = 120.0
SPEECH_HOLD_SECONDS = 30.0
TRANSCRIBE_HOLD_SECONDS = 60.0
PROCESSING_HOLD_SECONDS = 180.0
PLAYBACK_HOLD_SECONDS = 180.0
_PLAYBACK_MEMORY = 16


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
        self._idle_before_speech: float | None = None
        self._consumed: deque[str] = deque(maxlen=_PLAYBACK_MEMORY)

    def pending_timers(self) -> int:
        return 0

    def playback_memory(self) -> int:
        return len(self._consumed)

    def expire_due(self) -> SessionDecision:
        expired = self._sync()
        return self._decision(
            reason="silent_expire" if expired else "no_due_deadline",
            silent_expire=expired,
        )

    def set_capture(self, mode: CaptureState | str) -> SessionDecision:
        capture = mode if isinstance(mode, CaptureState) else CaptureState(mode)
        if capture is CaptureState.OFF:
            return self.off()
        self.capture = capture
        if capture is CaptureState.COMMAND:
            self._invalidate()
            return self._decision(reason="command_mode", abort_turn=True)
        return self._decision(reason="listening")

    def off(self) -> SessionDecision:
        self.capture = CaptureState.OFF
        self._invalidate()
        return self._decision(reason="off", abort_turn=True, stop_capture=True)

    def cancel(self) -> SessionDecision:
        self._invalidate()
        return self._decision(reason="cancel", abort_turn=True)

    def clear(self) -> SessionDecision:
        self._invalidate()
        return self._decision(reason="clear", abort_turn=True)

    def reconnect(self) -> SessionDecision:
        self._invalidate()
        return self._decision(reason="reconnect", abort_turn=True)

    def wake(
        self,
        *,
        residual: str = "",
        source: str = "simulated",
        at: float | None = None,
    ) -> SessionDecision:
        del at
        self._sync()
        if self.capture is CaptureState.OFF:
            return self._decision(reason="off_blocks_wake", ignored=True)
        if self.capture is CaptureState.COMMAND:
            return self._decision(reason="command_ignores_wake", ignored=True)
        if source == "self":
            return self._decision(reason="self_audio_ignored", ignored=True)
        if self.phase in {TurnPhase.PROCESSING, TurnPhase.PLAYING}:
            return self._decision(reason="turn_in_progress", ignored=True)
        if self.phase in {TurnPhase.SPEECH, TurnPhase.TRANSCRIBING}:
            return self._decision(reason="speech_in_progress", ignored=True)
        residual = residual.strip()
        if self.permit is TalkPermit.OPEN and not residual:
            return self._decision(reason="redundant_bare_wake", ignored=True)
        if self.permit is not TalkPermit.OPEN:
            self.generation += 1
            self.conversation_id = uuid.uuid4().hex
            self._consumed.clear()
            self.idle_deadline = self._clock() + IDLE_SECONDS
        self.permit = TalkPermit.OPEN
        self.residual = residual
        if residual:
            self.turn_id = uuid.uuid4().hex
            self.phase = TurnPhase.PROCESSING
            self.phase_deadline = self._clock() + PROCESSING_HOLD_SECONDS
            self._idle_before_speech = self.idle_deadline
            return self._decision(reason="wake_with_request", allow_reply=True)
        self.turn_id = ""
        self.phase = TurnPhase.IDLE
        self.phase_deadline = None
        return self._decision(reason="wake_wait")

    def speech_start(self, *, source: str = "user", at: float | None = None) -> SessionDecision:
        self._sync()
        if self.capture is CaptureState.OFF:
            return self._decision(reason="off_blocks_speech", ignored=True)
        if source == "self":
            return self._decision(reason="self_audio_ignored", ignored=True)
        if self.capture is CaptureState.COMMAND:
            return self._decision(reason="command_ignores_speech", ignored=True)
        if self.permit is not TalkPermit.OPEN:
            return self._decision(reason="veille_no_permit")
        if self.phase is not TurnPhase.IDLE:
            return self._decision(reason="turn_in_progress", ignored=True)
        now = self._clock() if at is None else at
        if self.idle_deadline is not None and now >= self.idle_deadline:
            self._expire()
            return self._decision(reason="speech_after_deadline")
        self._idle_before_speech = self.idle_deadline
        self.turn_id = uuid.uuid4().hex
        self.phase = TurnPhase.SPEECH
        self.phase_deadline = now + SPEECH_HOLD_SECONDS
        self.residual = ""
        return self._decision(reason="speech_reserved")

    def begin_transcribe(
        self, *, conversation_id: str, turn_id: str, generation: int
    ) -> SessionDecision:
        self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._decision(reason="stale_event", ignored=True)
        if self.phase is not TurnPhase.SPEECH:
            return self._decision(reason="stale_event", ignored=True)
        self.phase = TurnPhase.TRANSCRIBING
        self.phase_deadline = self._clock() + TRANSCRIBE_HOLD_SECONDS
        return self._decision(reason="transcribing")

    def speech_reject(
        self, *, conversation_id: str, turn_id: str, generation: int
    ) -> SessionDecision:
        self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._decision(reason="stale_event", ignored=True)
        if self.phase not in {TurnPhase.SPEECH, TurnPhase.TRANSCRIBING}:
            return self._decision(reason="stale_event", ignored=True)
        expired = self._release_turn()
        return self._decision(
            reason="silent_expire" if expired else "false_start",
            silent_expire=expired,
        )

    def transcript_ready(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        generation: int,
        text: str,
    ) -> SessionDecision:
        self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._decision(reason="stale_event", ignored=True)
        if self.phase not in {TurnPhase.SPEECH, TurnPhase.TRANSCRIBING}:
            return self._decision(reason="stale_event", ignored=True)
        text = text.strip()
        if not text:
            expired = self._release_turn()
            return self._decision(
                reason="silent_expire" if expired else "empty_transcript",
                silent_expire=expired,
            )
        self.residual = text
        self.phase = TurnPhase.PROCESSING
        self.phase_deadline = self._clock() + PROCESSING_HOLD_SECONDS
        return self._decision(reason="turn_accepted", allow_reply=True)

    def processing_started(
        self, *, conversation_id: str, turn_id: str, generation: int
    ) -> SessionDecision:
        self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._decision(reason="stale_event", ignored=True)
        if self.phase not in {TurnPhase.IDLE, TurnPhase.PROCESSING}:
            return self._decision(reason="stale_event", ignored=True)
        if self.permit is not TalkPermit.OPEN:
            return self._decision(reason="veille_no_permit")
        self.phase = TurnPhase.PROCESSING
        self.phase_deadline = self._clock() + PROCESSING_HOLD_SECONDS
        return self._decision(reason="processing", allow_reply=True)

    def processing_failed(
        self, *, conversation_id: str, turn_id: str, generation: int
    ) -> SessionDecision:
        self._sync()
        if not self._match(conversation_id, turn_id, generation):
            return self._decision(reason="stale_event", ignored=True)
        if self.phase not in {TurnPhase.PROCESSING, TurnPhase.PLAYING}:
            return self._decision(reason="stale_event", ignored=True)
        expired = self._release_turn()
        return self._decision(
            reason="silent_expire" if expired else "processing_failed",
            silent_expire=expired,
            abort_turn=True,
        )

    def playback_started(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        generation: int,
        playback_id: str,
    ) -> SessionDecision:
        self._sync()
        if not playback_id or not self._match(conversation_id, turn_id, generation):
            return self._decision(reason="stale_event", ignored=True)
        if self.phase is not TurnPhase.PROCESSING:
            return self._decision(reason="stale_event", ignored=True)
        self.phase = TurnPhase.PLAYING
        self.phase_deadline = self._clock() + PLAYBACK_HOLD_SECONDS
        self.playing_playback_id = playback_id
        return self._decision(reason="playing", playback_id=playback_id)

    def note_llm_token(self) -> SessionDecision:
        self._sync()
        return self._decision(reason="llm_token_ignored")

    def note_tts_segment(self) -> SessionDecision:
        self._sync()
        return self._decision(reason="tts_segment_ignored")

    def playback_finished(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        generation: int,
        playback_id: str,
    ) -> SessionDecision:
        self._sync()
        if (
            not playback_id
            or playback_id in self._consumed
            or playback_id != self.playing_playback_id
            or self.phase is not TurnPhase.PLAYING
            or not self._match(conversation_id, turn_id, generation)
        ):
            return self._decision(reason="stale_event", ignored=True)
        self._consumed.append(playback_id)
        self.playing_playback_id = ""
        self.turn_id = ""
        self.residual = ""
        self.phase = TurnPhase.IDLE
        self.phase_deadline = None
        self._idle_before_speech = None
        self.idle_deadline = self._clock() + IDLE_SECONDS
        return self._decision(reason="playback_finished", playback_id=playback_id)

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

    def _expire(self) -> None:
        self._invalidate()

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

    def _release_turn(self) -> bool:
        self._restore_idle()
        if self.permit is TalkPermit.OPEN and self._idle_passed():
            self._expire()
            return True
        return False

    def _sync(self) -> bool:
        now = self._clock()
        if (
            self.phase in {TurnPhase.SPEECH, TurnPhase.TRANSCRIBING}
            and self.phase_deadline is not None
            and now >= self.phase_deadline
        ):
            return self._release_turn()
        if (
            self.phase in {TurnPhase.PROCESSING, TurnPhase.PLAYING}
            and self.phase_deadline is not None
            and now >= self.phase_deadline
        ):
            return self._release_turn()
        if self.permit is TalkPermit.OPEN and self.phase is TurnPhase.IDLE and self._idle_passed():
            self._expire()
            return True
        return False

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
    ) -> SessionDecision:
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
        )
