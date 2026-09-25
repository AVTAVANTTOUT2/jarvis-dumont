"""Five exclusive crew seats and one PCM intercom. RAM only; no VoiceLoop."""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Callable

from jarvis_office.audio_ingress import REMOTE_RATE
from jarvis_office.web_audio import FRAME_BYTES

SEATS = ("aymen", "evann", "alexandre", "elias", "faiz")
MAX_SESSIONS = 5
PRESENCE_TTL = 5.0
# Intercom bounds are independent of the STT/TTS queues: 200 ms per upload,
# 400 ms in the relay, and no replay of audio that waited more than 500 ms.
TALK_BATCH_BYTES = FRAME_BYTES * 10
TALK_CAPACITY = FRAME_BYTES * 20
TALK_MAX_AGE = 0.5


class TalkError(Exception):
    """Protocol code, same shape as LoopError."""


class _Session:
    def __init__(self, token: str, now: float) -> None:
        self.token = token
        self.profile: str | None = None
        self.seen = now
        self.downlink = bytearray()
        self.pending_since: float | None = None
        self.error: str | None = None


class TalkBridge:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self.sessions: dict[str, _Session] = {}
        self.call: tuple[str, str] | None = None
        self.call_id = ""

    def clear(self) -> None:
        self.sessions.clear()
        self.call = None
        self.call_id = ""

    def expire(self) -> None:
        now = self._now()
        dead = [
            token for token, session in self.sessions.items() if now - session.seen > PRESENCE_TTL
        ]
        hanging = False
        for token in dead:
            session = self.sessions.pop(token)
            if self.call is not None and session.profile in self.call:
                hanging = True
        if hanging:
            self._clear_call()

    def register(self) -> str:
        self.expire()
        if len(self.sessions) >= MAX_SESSIONS:
            raise TalkError("session_limit")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = _Session(token, self._now())
        return token

    def touch(self, token: str) -> None:
        session = self.sessions.get(token)
        if session is not None:
            session.seen = self._now()

    def claim(self, token: str, profile: str) -> None:
        self.expire()
        session = self.sessions.get(token)
        if session is None:
            raise TalkError("unknown_session")
        if profile not in SEATS:
            raise TalkError("unknown_seat")
        holder = self._session_for(profile)
        if holder is not None and holder.token != token:
            raise TalkError("seat_taken")
        if session.profile and self.call is not None and session.profile in self.call:
            self._clear_call()
        session.profile = profile
        session.seen = self._now()

    def release(self, token: str) -> None:
        self.expire()
        session = self.sessions.get(token)
        if session is None:
            raise TalkError("unknown_session")
        session.seen = self._now()
        if session.profile and self.call is not None and session.profile in self.call:
            self._clear_call()
        session.profile = None

    def start_call(self, token: str, peer: str) -> tuple[str, str]:
        self.expire()
        me = self._profile(token)
        if peer not in SEATS or peer == me:
            raise TalkError("unknown_seat")
        if self.call is not None:
            raise TalkError("call_busy")
        other = self._session_for(peer)
        if other is None:
            raise TalkError("peer_offline")
        self.call = (me, peer)
        self.call_id = secrets.token_hex(16)
        session = self.sessions[token]
        session.seen = self._now()
        other.seen = session.seen
        session.error = other.error = None
        return self.call

    def hangup(self, token: str, call_id: str) -> None:
        self._call_session(token, call_id)
        self._clear_call()

    def feed(self, token: str, pcm: bytes, call_id: str) -> None:
        session = self._call_session(token, call_id)
        if not 0 < len(pcm) <= TALK_BATCH_BYTES or len(pcm) % FRAME_BYTES:
            raise TalkError("invalid_remote_pcm")
        self._check_audio_age()
        assert self.call is not None
        peer = self.call[0] if self.call[1] == session.profile else self.call[1]
        other = self._session_for(peer)
        if other is None:
            raise TalkError("peer_offline")
        if len(pcm) > TALK_CAPACITY - len(other.downlink):
            self._clear_call("audio_stalled")
            raise TalkError("remote_pcm_backpressure")
        if not other.downlink:
            other.pending_since = self._now()
        other.downlink.extend(pcm)

    def pull(self, token: str, call_id: str, limit: int = TALK_CAPACITY) -> dict[str, str | int]:
        session = self._call_session(token, call_id)
        self._check_audio_age()
        if limit < 2 or limit > TALK_CAPACITY or limit % 2:
            limit = TALK_CAPACITY
        chunk = bytes(session.downlink[:limit])
        del session.downlink[: len(chunk)]
        if not session.downlink:
            session.pending_since = None
        return {
            "pcm": base64.b64encode(chunk).decode("ascii") if chunk else "",
            "rate": REMOTE_RATE,
            "call": self.call_id,
        }

    def view(self, token: str) -> dict[str, object]:
        self.expire()
        session = self.sessions.get(token)
        if session is not None:
            session.seen = self._now()
        seats = {name: {"online": self._session_for(name) is not None} for name in SEATS}
        call = (
            {"id": self.call_id, "a": self.call[0], "b": self.call[1]}
            if self.call is not None
            else None
        )
        return {
            "me": session.profile if session else None,
            "seats": seats,
            "call": call,
            "error": session.error if session else None,
        }

    def _clear_call(self, error: str | None = None) -> None:
        if self.call is not None:
            for name in self.call:
                other = self._session_for(name)
                if other is not None:
                    other.downlink.clear()
                    other.pending_since = None
                    other.error = error
        self.call = None
        self.call_id = ""

    def _call_session(self, token: str, call_id: str) -> _Session:
        self.expire()
        session = self.sessions.get(token)
        if session is None:
            raise TalkError("unknown_session")
        if not call_id or self.call is None or call_id != self.call_id:
            raise TalkError("stale_call")
        if session.profile not in self.call:
            raise TalkError("not_in_call")
        session.seen = self._now()
        return session

    def _check_audio_age(self) -> None:
        now = self._now()
        if any(
            session.pending_since is not None and now - session.pending_since > TALK_MAX_AGE
            for session in self.sessions.values()
        ):
            self._clear_call("audio_stalled")
            raise TalkError("audio_stalled")

    def _session_for(self, profile: str) -> _Session | None:
        for session in self.sessions.values():
            if session.profile == profile:
                return session
        return None

    def _profile(self, token: str) -> str:
        session = self.sessions.get(token)
        if session is None:
            raise TalkError("unknown_session")
        if session.profile is None:
            raise TalkError("unclaimed")
        return session.profile
