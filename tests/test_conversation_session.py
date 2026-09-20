"""Policy tests for ConversationSession.

Wake, speech and playback events here are simulated. They prove the session
contract, not acoustic wake-word recognition, STT, Echo playback or a live
microphone. This kernel is not wired into the nominal runtime.
"""

from __future__ import annotations

import unittest

from jarvis_office.conversation_session import (
    IDLE_SECONDS,
    PROCESSING_HOLD_SECONDS,
    CaptureState,
    ConversationSession,
    SessionDecision,
    TalkPermit,
    TurnPhase,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ids(decision: SessionDecision) -> dict[str, str | int]:
    return {
        "conversation_id": decision.conversation_id,
        "turn_id": decision.turn_id,
        "generation": decision.generation,
    }


class ConversationSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.session = ConversationSession(clock=self.clock)
        self.assertEqual(IDLE_SECONDS, 120.0)

    def listen(self) -> None:
        self.session.set_capture(CaptureState.LISTENING)

    def play(self, decision: SessionDecision, playback_id: str = "p1") -> SessionDecision:
        keys = _ids(decision)
        started = self.session.playback_started(**keys, playback_id=playback_id)
        self.assertFalse(started.ignored)
        return self.session.playback_finished(**keys, playback_id=playback_id)

    def test_off_blocks_wake_and_reply(self) -> None:
        wake = self.session.wake(residual="organise ma journée", source="simulated")
        self.assertTrue(wake.ignored)
        self.assertFalse(wake.allow_reply)
        self.assertFalse(wake.collect_context)
        self.assertEqual(wake.permit, TalkPermit.IDLE)
        speech = self.session.speech_start()
        self.assertFalse(speech.allow_reply)
        self.assertEqual(speech.reason, "off_blocks_speech")

    def test_veille_ordinary_speech_has_no_reply(self) -> None:
        self.listen()
        speech = self.session.speech_start(source="user")
        self.assertFalse(speech.allow_reply)
        self.assertTrue(speech.collect_context)
        self.assertEqual(speech.permit, TalkPermit.IDLE)
        self.assertEqual(speech.reason, "veille_no_permit")

    def test_valid_wake_keeps_associated_request(self) -> None:
        self.listen()
        wake = self.session.wake(residual="organise ma journée", source="simulated")
        self.assertTrue(wake.allow_reply)
        self.assertEqual(wake.residual, "organise ma journée")
        self.assertEqual(wake.permit, TalkPermit.OPEN)
        self.assertEqual(wake.phase, TurnPhase.PROCESSING)
        self.assertTrue(wake.collect_context)
        self.assertFalse(wake.stop_capture)

    def test_bare_wake_waits_then_expires_silently(self) -> None:
        self.listen()
        wake = self.session.wake(source="simulated")
        self.assertFalse(wake.allow_reply)
        self.assertEqual(wake.reason, "wake_wait")
        self.assertEqual(wake.idle_deadline, IDLE_SECONDS)
        self.clock.advance(IDLE_SECONDS)
        expired = self.session.expire_due()
        self.assertTrue(expired.silent_expire)
        self.assertEqual(expired.permit, TalkPermit.IDLE)
        self.assertFalse(expired.allow_reply)
        self.assertFalse(expired.stop_capture)
        self.assertFalse(expired.purge_context)
        self.assertTrue(expired.collect_context)

    def test_playback_finished_sets_deadline_exactly_plus_idle(self) -> None:
        self.listen()
        wake = self.session.wake(residual="organise ma journée")
        self.clock.advance(4.0)
        finished = self.play(wake, "stream-1")
        self.assertEqual(finished.idle_deadline, 4.0 + IDLE_SECONDS)
        self.assertEqual(finished.phase, TurnPhase.IDLE)
        self.assertEqual(finished.permit, TalkPermit.OPEN)
        self.assertEqual(finished.reason, "playback_finished")

    def test_resume_before_deadline_needs_no_wake(self) -> None:
        self.listen()
        finished = self.play(self.session.wake(residual="premier"), "p1")
        self.clock.advance(30.0)
        speech = self.session.speech_start()
        self.assertEqual(speech.reason, "speech_reserved")
        self.assertEqual(speech.idle_deadline, finished.idle_deadline)
        accepted = self.session.transcript_ready(**_ids(speech), text="et ensuite")
        self.assertTrue(accepted.allow_reply)
        self.assertEqual(accepted.residual, "et ensuite")
        self.assertEqual(accepted.permit, TalkPermit.OPEN)

    def test_speech_started_before_deadline_survives_late_stt(self) -> None:
        self.listen()
        self.play(self.session.wake(residual="premier"), "p1")
        self.clock.advance(100.0)
        speech = self.session.speech_start()
        self.assertEqual(speech.reason, "speech_reserved")
        self.session.begin_transcribe(**_ids(speech))
        self.clock.advance(40.0)
        self.assertGreaterEqual(self.clock.now, IDLE_SECONDS)
        accepted = self.session.transcript_ready(**_ids(speech), text="phrase tardive")
        self.assertTrue(accepted.allow_reply)
        self.assertEqual(accepted.permit, TalkPermit.OPEN)

    def test_speech_at_or_after_deadline_does_not_reopen(self) -> None:
        self.listen()
        self.play(self.session.wake(residual="premier"), "p1")
        self.clock.advance(119.0)
        late = self.session.speech_start(at=IDLE_SECONDS)
        self.assertFalse(late.allow_reply)
        self.assertEqual(late.permit, TalkPermit.IDLE)
        self.assertEqual(late.reason, "speech_after_deadline")
        self.listen()
        self.play(self.session.wake(residual="second"), "p2")
        self.clock.advance(IDLE_SECONDS)
        after = self.session.speech_start()
        self.assertFalse(after.allow_reply)
        self.assertEqual(after.permit, TalkPermit.IDLE)

    def test_rejected_vad_does_not_grant_a_fresh_idle_window(self) -> None:
        self.listen()
        finished = self.play(self.session.wake(residual="premier"), "p1")
        original = finished.idle_deadline
        self.clock.advance(50.0)
        speech = self.session.speech_start()
        self.assertEqual(speech.idle_deadline, original)
        rejected = self.session.speech_reject(**_ids(speech))
        self.assertFalse(rejected.silent_expire)
        self.assertEqual(rejected.idle_deadline, original)
        self.assertEqual(self.session.permit, TalkPermit.OPEN)
        self.clock.advance(70.0)
        expired = self.session.expire_due()
        self.assertTrue(expired.silent_expire)
        self.assertEqual(expired.permit, TalkPermit.IDLE)

    def test_processing_holds_idle_and_errors_restore_or_expire(self) -> None:
        self.listen()
        self.play(self.session.wake(residual="premier"), "p1")
        self.clock.advance(10.0)
        speech = self.session.speech_start()
        accepted = self.session.transcript_ready(**_ids(speech), text="suite")
        self.clock.advance(80.0)
        held = self.session.expire_due()
        self.assertFalse(held.silent_expire)
        self.assertEqual(held.permit, TalkPermit.OPEN)
        self.assertEqual(held.phase, TurnPhase.PROCESSING)
        failed = self.session.processing_failed(**_ids(accepted))
        self.assertFalse(failed.silent_expire)
        self.assertEqual(failed.permit, TalkPermit.OPEN)
        stale = self.session.processing_failed(**_ids(accepted))
        self.assertTrue(stale.ignored)
        self.clock.advance(40.0)
        due = self.session.expire_due()
        self.assertTrue(due.silent_expire)

        clock = Clock()
        session = ConversationSession(clock=clock)
        session.set_capture(CaptureState.LISTENING)
        session.wake(residual="erreur technique")
        clock.advance(PROCESSING_HOLD_SECONDS)
        timed = session.expire_due()
        self.assertTrue(timed.silent_expire)
        self.assertEqual(timed.permit, TalkPermit.IDLE)

        clock = Clock()
        session = ConversationSession(clock=clock)
        session.set_capture(CaptureState.LISTENING)
        turn = session.wake(residual="erreur après échéance")
        clock.advance(150.0)
        still_open = session.expire_due()
        self.assertEqual(still_open.permit, TalkPermit.OPEN)
        failed_late = session.processing_failed(**_ids(turn))
        self.assertTrue(failed_late.silent_expire)
        self.assertEqual(failed_late.permit, TalkPermit.IDLE)

    def test_successive_playback_renews_idle_tokens_do_not(self) -> None:
        self.listen()
        first = self.session.wake(residual="un")
        self.session.playback_started(**_ids(first), playback_id="p1")
        token = self.session.note_llm_token()
        segment = self.session.note_tts_segment()
        self.assertEqual(token.idle_deadline, first.idle_deadline)
        self.assertEqual(segment.idle_deadline, first.idle_deadline)
        done = self.session.playback_finished(**_ids(first), playback_id="p1")
        self.assertEqual(done.idle_deadline, IDLE_SECONDS)
        self.clock.advance(8.0)
        speech = self.session.speech_start()
        accepted = self.session.transcript_ready(**_ids(speech), text="deux")
        self.session.playback_started(**_ids(accepted), playback_id="p2")
        self.session.note_tts_segment()
        second = self.session.playback_finished(**_ids(accepted), playback_id="p2")
        self.assertEqual(second.idle_deadline, 8.0 + IDLE_SECONDS)
        self.assertNotEqual(second.idle_deadline, done.idle_deadline)

    def test_duplicate_and_stale_playback_finished_are_ignored(self) -> None:
        self.listen()
        first = self.session.wake(residual="un")
        self.session.playback_started(**_ids(first), playback_id="p1")
        done = self.session.playback_finished(**_ids(first), playback_id="p1")
        duplicate = self.session.playback_finished(**_ids(first), playback_id="p1")
        self.assertTrue(duplicate.ignored)
        self.assertEqual(duplicate.idle_deadline, done.idle_deadline)
        old = _ids(first)
        self.clock.advance(IDLE_SECONDS)
        self.session.expire_due()
        self.listen()
        self.session.wake(residual="nouveau")
        stale = self.session.playback_finished(**old, playback_id="p1")
        self.assertTrue(stale.ignored)
        self.assertNotEqual(stale.generation, old["generation"])

    def test_off_and_cancel_reject_late_reply_and_differ(self) -> None:
        self.listen()
        turn = self.session.wake(residual="en cours")
        cancelled = self.session.cancel()
        self.assertTrue(cancelled.abort_turn)
        self.assertFalse(cancelled.stop_capture)
        self.assertTrue(cancelled.collect_context)
        self.assertEqual(cancelled.permit, TalkPermit.IDLE)
        late = self.session.playback_finished(**_ids(turn), playback_id="p1")
        self.assertTrue(late.ignored)
        self.assertFalse(late.allow_reply)
        self.listen()
        turn = self.session.wake(residual="encore")
        stopped = self.session.off()
        self.assertTrue(stopped.stop_capture)
        self.assertFalse(stopped.collect_context)
        self.assertEqual(stopped.capture, CaptureState.OFF)
        later = self.session.wake(residual="trop tard")
        self.assertTrue(later.ignored)
        self.assertFalse(later.allow_reply)

    def test_self_audio_does_not_wake_or_extend(self) -> None:
        self.listen()
        ignored = self.session.wake(residual="Jarvis", source="self")
        self.assertTrue(ignored.ignored)
        self.assertEqual(ignored.permit, TalkPermit.IDLE)
        user = self.session.wake(residual="vrai")
        self.session.playback_started(**_ids(user), playback_id="p1")
        self.session.speech_start(source="self")
        echo = self.session.wake(source="self")
        self.assertTrue(echo.ignored)
        finished = self.session.playback_finished(**_ids(user), playback_id="p1")
        self.assertEqual(finished.idle_deadline, IDLE_SECONDS)
        self.clock.advance(10.0)
        noise = self.session.speech_start(source="self")
        self.assertTrue(noise.ignored)
        self.assertEqual(self.session.idle_deadline, IDLE_SECONDS)

    def test_expire_does_not_stop_capture_or_purge_context(self) -> None:
        self.listen()
        self.session.wake()
        self.clock.advance(IDLE_SECONDS)
        expired = self.session.expire_due()
        self.assertTrue(expired.silent_expire)
        self.assertFalse(expired.stop_capture)
        self.assertFalse(expired.purge_context)
        self.assertTrue(expired.collect_context)
        self.assertEqual(expired.capture, CaptureState.LISTENING)

    def test_repeated_cycles_stay_bounded_without_timers(self) -> None:
        self.listen()
        for index in range(24):
            turn = self.session.wake(residual=f"tour {index}")
            self.play(turn, f"p{index}")
            self.assertLessEqual(self.session.playback_memory(), 16)
            self.assertEqual(self.session.pending_timers(), 0)
            self.clock.advance(IDLE_SECONDS)
            expired = self.session.expire_due()
            self.assertTrue(expired.silent_expire)
            self.assertEqual(self.session.pending_timers(), 0)
            self.assertEqual(self.session.playback_memory(), 0)
        self.assertEqual(self.session.permit, TalkPermit.IDLE)
        self.assertEqual(self.session.capture, CaptureState.LISTENING)

    def test_command_mode_stays_outside_conversation(self) -> None:
        self.session.set_capture(CaptureState.COMMAND)
        wake = self.session.wake(residual="pause")
        self.assertTrue(wake.ignored)
        self.assertFalse(wake.allow_reply)
        self.assertFalse(wake.collect_context)
        speech = self.session.speech_start()
        self.assertFalse(speech.allow_reply)

    def test_reconnect_does_not_resurrect_an_open_conversation(self) -> None:
        self.listen()
        self.session.wake(residual="avant")
        dropped = self.session.reconnect()
        self.assertEqual(dropped.permit, TalkPermit.IDLE)
        self.assertFalse(dropped.allow_reply)
        self.assertTrue(dropped.collect_context)
        speech = self.session.speech_start()
        self.assertEqual(speech.reason, "veille_no_permit")
