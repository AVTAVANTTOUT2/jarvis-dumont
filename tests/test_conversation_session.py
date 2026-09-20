"""Policy tests for ConversationSession.

Wake, speech and playback events here are simulated. They prove the session
contract, not acoustic wake-word recognition, STT, Echo playback or a live
microphone. This kernel is not wired into the nominal runtime.
"""

from __future__ import annotations

import unittest

from jarvis_office.conversation_session import (
    IDLE_SECONDS,
    MAX_EVENT_LAG_SECONDS,
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


def _wake_now(session: ConversationSession, clock: Clock, residual: str = "") -> SessionDecision:
    return session.wake(arm_id=session.arm_id, epoch=session.epoch, at=clock.now, residual=residual)


def _ids(decision: SessionDecision) -> dict[str, str | int]:
    return {
        "conversation_id": decision.conversation_id,
        "turn_id": decision.turn_id,
        "generation": decision.generation,
    }


class SessionHarness(unittest.TestCase):
    clock: Clock
    session: ConversationSession

    def setUp(self) -> None:
        self.clock = Clock()
        self.session = ConversationSession(clock=self.clock)
        self.assertEqual(IDLE_SECONDS, 120.0)
        self.assertEqual(MAX_EVENT_LAG_SECONDS, 2.0)

    def listen(self) -> None:
        self.session.set_capture(CaptureState.LISTENING)

    def origin(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "arm_id": self.session.arm_id,
            "epoch": self.session.epoch,
            "at": self.clock.now,
        }
        values.update(overrides)
        return values

    def wake(self, **kwargs: object) -> SessionDecision:
        return self.session.wake(**self.origin(**kwargs))

    def speech_start(self, **kwargs: object) -> SessionDecision:
        return self.session.speech_start(**self.origin(**kwargs))

    def finalize_idle(self) -> SessionDecision:
        deadline = self.session.idle_deadline
        if deadline is None:
            self.clock.advance(IDLE_SECONDS + MAX_EVENT_LAG_SECONDS)
        elif self.clock.now < deadline + MAX_EVENT_LAG_SECONDS:
            self.clock.now = deadline + MAX_EVENT_LAG_SECONDS
        return self.session.expire_due()

    def play(self, decision: SessionDecision, playback_id: str = "p1") -> SessionDecision:
        keys = _ids(decision)
        started = self.session.playback_started(**keys, playback_id=playback_id)
        self.assertFalse(started.ignored)
        return self.session.playback_finished(**keys, playback_id=playback_id)


class ConversationSessionTests(SessionHarness):
    def test_off_blocks_wake_and_reply(self) -> None:
        wake = self.wake(residual="organise ma journée", source="simulated")
        self.assertTrue(wake.ignored)
        self.assertFalse(wake.allow_reply)
        self.assertFalse(wake.collect_context)
        self.assertEqual(wake.permit, TalkPermit.IDLE)
        speech = self.speech_start()
        self.assertFalse(speech.allow_reply)
        self.assertEqual(speech.reason, "off_blocks_speech")

    def test_veille_ordinary_speech_has_no_reply(self) -> None:
        self.listen()
        speech = self.speech_start(source="user")
        self.assertFalse(speech.allow_reply)
        self.assertTrue(speech.collect_context)
        self.assertEqual(speech.permit, TalkPermit.IDLE)
        self.assertEqual(speech.reason, "veille_no_permit")

    def test_valid_wake_keeps_associated_request(self) -> None:
        self.listen()
        wake = self.wake(residual="organise ma journée", source="simulated")
        self.assertTrue(wake.allow_reply)
        self.assertEqual(wake.residual, "organise ma journée")
        self.assertEqual(wake.permit, TalkPermit.OPEN)
        self.assertEqual(wake.phase, TurnPhase.PROCESSING)
        self.assertTrue(wake.collect_context)
        self.assertFalse(wake.stop_capture)

    def test_bare_wake_waits_then_expires_silently(self) -> None:
        self.listen()
        wake = self.wake(source="simulated")
        self.assertFalse(wake.allow_reply)
        self.assertEqual(wake.reason, "wake_wait")
        self.assertEqual(wake.idle_deadline, IDLE_SECONDS)
        self.clock.advance(IDLE_SECONDS)
        held = self.session.expire_due()
        self.assertFalse(held.silent_expire)
        self.assertEqual(held.permit, TalkPermit.OPEN)
        expired = self.finalize_idle()
        self.assertTrue(expired.silent_expire)
        self.assertEqual(expired.permit, TalkPermit.IDLE)
        self.assertFalse(expired.allow_reply)
        self.assertFalse(expired.abort_turn)
        self.assertFalse(expired.stop_capture)
        self.assertFalse(expired.purge_context)
        self.assertTrue(expired.collect_context)

    def test_playback_finished_sets_deadline_exactly_plus_idle(self) -> None:
        self.listen()
        wake = self.wake(residual="organise ma journée")
        self.clock.advance(4.0)
        finished = self.play(wake, "stream-1")
        self.assertEqual(finished.idle_deadline, 4.0 + IDLE_SECONDS)
        self.assertEqual(finished.phase, TurnPhase.IDLE)
        self.assertEqual(finished.permit, TalkPermit.OPEN)
        self.assertEqual(finished.reason, "playback_finished")

    def test_resume_before_deadline_needs_no_wake(self) -> None:
        self.listen()
        finished = self.play(self.wake(residual="premier"), "p1")
        self.clock.advance(30.0)
        speech = self.speech_start()
        self.assertEqual(speech.reason, "speech_reserved")
        self.assertEqual(speech.idle_deadline, finished.idle_deadline)
        accepted = self.session.transcript_ready(**_ids(speech), text="et ensuite")
        self.assertTrue(accepted.allow_reply)
        self.assertEqual(accepted.residual, "et ensuite")
        self.assertEqual(accepted.permit, TalkPermit.OPEN)

    def test_speech_started_before_deadline_survives_late_stt(self) -> None:
        self.listen()
        self.play(self.wake(residual="premier"), "p1")
        self.clock.advance(100.0)
        speech = self.speech_start()
        self.assertEqual(speech.reason, "speech_reserved")
        self.session.begin_transcribe(**_ids(speech))
        self.clock.advance(40.0)
        self.assertGreaterEqual(self.clock.now, IDLE_SECONDS)
        accepted = self.session.transcript_ready(**_ids(speech), text="phrase tardive")
        self.assertTrue(accepted.allow_reply)
        self.assertEqual(accepted.permit, TalkPermit.OPEN)

    def test_speech_at_or_after_deadline_does_not_reopen(self) -> None:
        self.listen()
        self.play(self.wake(residual="premier"), "p1")
        self.clock.advance(IDLE_SECONDS)
        late = self.speech_start(at=IDLE_SECONDS)
        self.assertFalse(late.allow_reply)
        self.assertEqual(late.permit, TalkPermit.IDLE)
        self.assertEqual(late.reason, "speech_after_deadline")
        self.listen()
        self.play(self.wake(residual="second"), "p2")
        self.clock.advance(IDLE_SECONDS + MAX_EVENT_LAG_SECONDS)
        after = self.speech_start()
        self.assertFalse(after.allow_reply)
        self.assertEqual(after.permit, TalkPermit.IDLE)

    def test_rejected_vad_does_not_grant_a_fresh_idle_window(self) -> None:
        self.listen()
        finished = self.play(self.wake(residual="premier"), "p1")
        original = finished.idle_deadline
        self.clock.advance(50.0)
        speech = self.speech_start()
        self.assertEqual(speech.idle_deadline, original)
        rejected = self.session.speech_reject(**_ids(speech))
        self.assertFalse(rejected.silent_expire)
        self.assertEqual(rejected.idle_deadline, original)
        self.assertEqual(self.session.permit, TalkPermit.OPEN)
        expired = self.finalize_idle()
        self.assertTrue(expired.silent_expire)
        self.assertEqual(expired.permit, TalkPermit.IDLE)

    def test_processing_holds_idle_and_errors_restore_or_expire(self) -> None:
        self.listen()
        self.play(self.wake(residual="premier"), "p1")
        self.clock.advance(10.0)
        speech = self.speech_start()
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
        _wake_now(session, clock, residual="erreur technique")
        clock.advance(PROCESSING_HOLD_SECONDS)
        timed = session.expire_due()
        self.assertTrue(timed.silent_expire)
        self.assertTrue(timed.abort_turn)
        self.assertEqual(timed.permit, TalkPermit.IDLE)

        clock = Clock()
        session = ConversationSession(clock=clock)
        session.set_capture(CaptureState.LISTENING)
        turn = _wake_now(session, clock, residual="erreur après échéance")
        clock.advance(150.0)
        still_open = session.expire_due()
        self.assertEqual(still_open.permit, TalkPermit.OPEN)
        failed_late = session.processing_failed(**_ids(turn))
        self.assertTrue(failed_late.silent_expire)
        self.assertEqual(failed_late.permit, TalkPermit.IDLE)

    def test_successive_playback_renews_idle_tokens_do_not(self) -> None:
        self.listen()
        first = self.wake(residual="un")
        self.session.playback_started(**_ids(first), playback_id="p1")
        token = self.session.note_llm_token()
        segment = self.session.note_tts_segment()
        self.assertEqual(token.idle_deadline, first.idle_deadline)
        self.assertEqual(segment.idle_deadline, first.idle_deadline)
        done = self.session.playback_finished(**_ids(first), playback_id="p1")
        self.assertEqual(done.idle_deadline, IDLE_SECONDS)
        self.clock.advance(8.0)
        speech = self.speech_start()
        accepted = self.session.transcript_ready(**_ids(speech), text="deux")
        self.session.playback_started(**_ids(accepted), playback_id="p2")
        self.session.note_tts_segment()
        second = self.session.playback_finished(**_ids(accepted), playback_id="p2")
        self.assertEqual(second.idle_deadline, 8.0 + IDLE_SECONDS)
        self.assertNotEqual(second.idle_deadline, done.idle_deadline)

    def test_duplicate_and_stale_playback_finished_are_ignored(self) -> None:
        self.listen()
        first = self.wake(residual="un")
        self.session.playback_started(**_ids(first), playback_id="p1")
        done = self.session.playback_finished(**_ids(first), playback_id="p1")
        duplicate = self.session.playback_finished(**_ids(first), playback_id="p1")
        self.assertTrue(duplicate.ignored)
        self.assertEqual(duplicate.idle_deadline, done.idle_deadline)
        old = _ids(first)
        self.clock.advance(IDLE_SECONDS + MAX_EVENT_LAG_SECONDS)
        self.session.expire_due()
        self.listen()
        self.wake(residual="nouveau")
        stale = self.session.playback_finished(**old, playback_id="p1")
        self.assertTrue(stale.ignored)
        self.assertNotEqual(stale.generation, old["generation"])

    def test_off_and_cancel_reject_late_reply_and_differ(self) -> None:
        self.listen()
        turn = self.wake(residual="en cours")
        cancelled = self.session.cancel()
        self.assertTrue(cancelled.abort_turn)
        self.assertEqual(cancelled.abandoned_turn_id, turn.turn_id)
        self.assertFalse(cancelled.stop_capture)
        self.assertTrue(cancelled.collect_context)
        self.assertEqual(cancelled.permit, TalkPermit.IDLE)
        late = self.session.playback_finished(**_ids(turn), playback_id="p1")
        self.assertTrue(late.ignored)
        self.assertFalse(late.allow_reply)
        self.listen()
        turn = self.wake(residual="encore")
        stopped = self.session.off()
        self.assertTrue(stopped.stop_capture)
        self.assertFalse(stopped.collect_context)
        self.assertEqual(stopped.capture, CaptureState.OFF)
        later = self.wake(residual="trop tard")
        self.assertTrue(later.ignored)
        self.assertFalse(later.allow_reply)

    def test_self_audio_does_not_wake_or_extend(self) -> None:
        self.listen()
        ignored = self.wake(residual="Jarvis", source="self")
        self.assertTrue(ignored.ignored)
        self.assertEqual(ignored.permit, TalkPermit.IDLE)
        user = self.wake(residual="vrai")
        self.session.playback_started(**_ids(user), playback_id="p1")
        self.speech_start(source="self")
        echo = self.wake(source="self")
        self.assertTrue(echo.ignored)
        finished = self.session.playback_finished(**_ids(user), playback_id="p1")
        self.assertEqual(finished.idle_deadline, IDLE_SECONDS)
        self.clock.advance(10.0)
        noise = self.speech_start(source="self")
        self.assertTrue(noise.ignored)
        self.assertEqual(self.session.idle_deadline, IDLE_SECONDS)

    def test_expire_does_not_stop_capture_or_purge_context(self) -> None:
        self.listen()
        self.wake()
        expired = self.finalize_idle()
        self.assertTrue(expired.silent_expire)
        self.assertFalse(expired.stop_capture)
        self.assertFalse(expired.purge_context)
        self.assertFalse(expired.abort_turn)
        self.assertTrue(expired.collect_context)
        self.assertEqual(expired.capture, CaptureState.LISTENING)

    def test_repeated_cycles_stay_bounded_without_timers(self) -> None:
        self.listen()
        for index in range(24):
            turn = self.wake(residual=f"tour {index}")
            self.play(turn, f"p{index}")
            self.assertLessEqual(self.session.playback_memory(), 16)
            self.assertLessEqual(self.session.event_memory(), 32)
            self.assertEqual(self.session.pending_timers(), 0)
            expired = self.finalize_idle()
            self.assertTrue(expired.silent_expire)
            self.assertEqual(self.session.pending_timers(), 0)
            self.assertEqual(self.session.playback_memory(), 0)
        self.assertEqual(self.session.permit, TalkPermit.IDLE)
        self.assertEqual(self.session.capture, CaptureState.LISTENING)

    def test_command_mode_stays_outside_conversation(self) -> None:
        self.session.set_capture(CaptureState.COMMAND)
        wake = self.wake(residual="pause")
        self.assertTrue(wake.ignored)
        self.assertFalse(wake.allow_reply)
        self.assertFalse(wake.collect_context)
        speech = self.speech_start()
        self.assertFalse(speech.allow_reply)

    def test_reconnect_does_not_resurrect_an_open_conversation(self) -> None:
        self.listen()
        self.wake(residual="avant")
        dropped = self.session.reconnect()
        self.assertEqual(dropped.permit, TalkPermit.IDLE)
        self.assertFalse(dropped.allow_reply)
        self.assertTrue(dropped.collect_context)
        speech = self.speech_start()
        self.assertEqual(speech.reason, "veille_no_permit")


class ConversationSessionReviewFixes(SessionHarness):
    def _open_idle_window(self) -> SessionDecision:
        self.listen()
        return self.play(self.wake(residual="premier"), "p1")

    def test_r1_delayed_speech_start_before_deadline_is_reserved(self) -> None:
        finished = self._open_idle_window()
        prepared = self.session.acquisition()
        self.clock.now = 121.0
        speech = self.speech_start(arm_id=prepared.arm_id, epoch=prepared.epoch, at=119.0)
        self.assertEqual(speech.reason, "speech_reserved")
        self.assertEqual(speech.permit, TalkPermit.OPEN)
        self.assertEqual(speech.phase, TurnPhase.SPEECH)
        self.assertEqual(speech.idle_deadline, finished.idle_deadline)
        self.clock.advance(10.0)
        accepted = self.session.transcript_ready(**_ids(speech), text="suite")
        self.assertTrue(accepted.allow_reply)

    def test_r1_sub_millisecond_lag_around_deadline(self) -> None:
        self._open_idle_window()
        prepared = self.session.acquisition()
        self.clock.now = 120.001
        speech = self.speech_start(arm_id=prepared.arm_id, epoch=prepared.epoch, at=119.999)
        self.assertEqual(speech.reason, "speech_reserved")
        self.assertEqual(speech.phase, TurnPhase.SPEECH)

    def test_r1_expire_due_then_in_flight_speech_is_order_independent(self) -> None:
        self._open_idle_window()
        prepared = self.session.acquisition()
        self.clock.now = 121.0
        held = self.session.expire_due()
        self.assertFalse(held.silent_expire)
        self.assertEqual(held.permit, TalkPermit.OPEN)
        speech = self.speech_start(arm_id=prepared.arm_id, epoch=prepared.epoch, at=119.0)
        self.assertEqual(speech.reason, "speech_reserved")

        second = ConversationSessionTests()
        second.setUp()
        second.listen()
        second.play(second.wake(residual="premier"), "p1")
        prepared = second.session.acquisition()
        second.clock.now = 121.0
        speech = second.speech_start(arm_id=prepared.arm_id, epoch=prepared.epoch, at=119.0)
        self.assertEqual(speech.reason, "speech_reserved")
        held = second.session.expire_due()
        self.assertEqual(held.phase, TurnPhase.SPEECH)
        self.assertFalse(held.silent_expire)

    def test_r1_finalized_idle_does_not_reopen_from_backdated_speech(self) -> None:
        self._open_idle_window()
        prepared = self.session.acquisition()
        self.clock.now = IDLE_SECONDS + MAX_EVENT_LAG_SECONDS
        expired = self.session.expire_due()
        self.assertTrue(expired.silent_expire)
        late = self.speech_start(arm_id=prepared.arm_id, epoch=prepared.epoch, at=119.0)
        self.assertTrue(late.ignored)
        self.assertNotEqual(late.reason, "speech_reserved")
        self.assertEqual(self.session.permit, TalkPermit.IDLE)

    def test_r2_processing_timeout_requests_abort_of_abandoned_turn(self) -> None:
        self.listen()
        wake = self.wake(residual="demande")
        self.clock.now = PROCESSING_HOLD_SECONDS
        expired = self.session.expire_due()
        self.assertEqual(expired.reason, "turn_hold_timeout")
        self.assertTrue(expired.abort_turn)
        self.assertEqual(expired.abandoned_turn_id, wake.turn_id)
        self.assertEqual(expired.abandoned_conversation_id, wake.conversation_id)
        self.assertEqual(expired.abandoned_generation, wake.generation)
        self.assertEqual(expired.turn_id, "")
        self.assertNotEqual(expired.abandoned_turn_id, expired.turn_id)

    def test_r2_playback_timeout_requests_abort_with_playback_id(self) -> None:
        self.listen()
        wake = self.wake(residual="demande")
        self.session.playback_started(**_ids(wake), playback_id="stream-9")
        self.clock.now = PROCESSING_HOLD_SECONDS
        expired = self.session.expire_due()
        self.assertTrue(expired.abort_turn)
        self.assertEqual(expired.abandoned_turn_id, wake.turn_id)
        self.assertEqual(expired.abandoned_playback_id, "stream-9")

    def test_r2_timeout_discovered_by_ignored_token_is_not_lost(self) -> None:
        self.listen()
        wake = self.wake(residual="demande")
        self.clock.now = PROCESSING_HOLD_SECONDS
        token = self.session.note_llm_token()
        self.assertTrue(token.abort_turn)
        self.assertEqual(token.reason, "turn_hold_timeout")
        self.assertEqual(token.abandoned_turn_id, wake.turn_id)
        later = self.session.expire_due()
        self.assertFalse(later.abort_turn)
        self.assertEqual(later.reason, "no_due_deadline")

        clock = Clock()
        session = ConversationSession(clock=clock)
        session.set_capture(CaptureState.LISTENING)
        other = _wake_now(session, clock, residual="autre")
        clock.now = PROCESSING_HOLD_SECONDS
        stale = session.begin_transcribe(**_ids(other))
        self.assertTrue(stale.abort_turn)
        self.assertEqual(stale.abandoned_turn_id, other.turn_id)
        self.assertTrue(stale.ignored)

    def test_r3_duplicate_processing_notifications_do_not_extend_hold(self) -> None:
        self.listen()
        wake = self.wake(residual="demande")
        deadline = self.session.phase_deadline
        self.assertEqual(deadline, PROCESSING_HOLD_SECONDS)
        self.assertFalse(hasattr(self.session, "processing_started"))
        self.clock.now = 179.0
        token = self.session.note_llm_token()
        self.assertFalse(token.abort_turn)
        self.assertEqual(self.session.phase_deadline, deadline)
        self.clock.now = PROCESSING_HOLD_SECONDS
        timed = self.session.expire_due()
        self.assertTrue(timed.abort_turn)
        self.assertEqual(timed.abandoned_turn_id, wake.turn_id)

    def test_r3_duplicates_after_failure_cancel_and_new_turn(self) -> None:
        self.listen()
        first = self.wake(residual="un")
        self.session.processing_failed(**_ids(first))
        again = self.session.note_llm_token()
        self.assertFalse(again.abort_turn)
        second = self.wake(residual="deux")
        self.assertTrue(second.allow_reply)
        self.assertNotEqual(second.turn_id, first.turn_id)
        cancelled = self.session.cancel()
        self.assertEqual(cancelled.abandoned_turn_id, second.turn_id)
        stale = self.session.playback_started(**_ids(second), playback_id="p")
        self.assertTrue(stale.ignored)

    def test_r4_old_wake_after_off_and_rearm_is_rejected(self) -> None:
        self.listen()
        prepared = self.session.acquisition()
        self.clock.now = 1.0
        self.session.off()
        self.clock.now = 1.2
        self.listen()
        self.clock.now = 2.5
        stale = self.session.wake(
            arm_id=prepared.arm_id,
            epoch=prepared.epoch,
            at=1.0,
            residual="ancien",
        )
        self.assertEqual(stale.reason, "stale_source")
        self.assertTrue(stale.ignored)
        self.assertFalse(stale.allow_reply)
        self.assertEqual(self.session.permit, TalkPermit.IDLE)
        fresh = self.wake(residual="nouveau")
        self.assertTrue(fresh.allow_reply)
        duplicate = self.wake(residual="nouveau")
        self.assertEqual(duplicate.reason, "duplicate_event")
        self.assertTrue(duplicate.ignored)

    def test_r4_old_speech_after_rearm_does_not_join_new_conversation(self) -> None:
        self.listen()
        old = self.session.acquisition()
        self.clock.now = 0.4
        self.session.off()
        self.clock.now = 0.5
        self.listen()
        fresh = self.wake(residual="nouveau")
        self.play(fresh, "p1")
        self.clock.now = 1.5
        stolen = self.session.speech_start(arm_id=old.arm_id, epoch=old.epoch, at=0.2)
        self.assertEqual(stolen.reason, "stale_source")
        self.assertTrue(stolen.ignored)
        self.assertEqual(self.session.phase, TurnPhase.IDLE)
        self.assertEqual(self.session.permit, TalkPermit.OPEN)

    def test_r4_late_events_after_cancel_clear_reconnect(self) -> None:
        for name in ("cancel", "clear", "reconnect"):
            clock = Clock()
            session = ConversationSession(clock=clock)
            session.set_capture(CaptureState.LISTENING)
            prepared = session.acquisition()
            prepared_at = clock.now
            session.wake(
                arm_id=prepared.arm_id,
                epoch=prepared.epoch,
                at=prepared_at,
                residual="x",
            )
            getattr(session, name)()
            clock.now = prepared_at + 1.0
            stale = session.wake(
                arm_id=prepared.arm_id,
                epoch=prepared.epoch,
                at=prepared_at,
                residual="tard",
            )
            self.assertEqual(stale.reason, "stale_source", name)
            self.assertFalse(stale.allow_reply, name)
            fresh = session.wake(
                arm_id=session.arm_id,
                epoch=session.epoch,
                at=clock.now,
                residual="frais",
            )
            self.assertTrue(fresh.allow_reply, name)

    def test_r5_non_finite_and_incoherent_timestamps_are_rejected(self) -> None:
        self.listen()
        self.wake()
        for value in (float("nan"), float("inf"), float("-inf")):
            speech = self.speech_start(at=value)
            self.assertEqual(speech.reason, "invalid_timestamp")
            self.assertTrue(speech.ignored)
            self.assertEqual(self.session.phase, TurnPhase.IDLE)
            self.assertEqual(self.session.permit, TalkPermit.OPEN)
        future = self.speech_start(at=self.clock.now + 5.0)
        self.assertEqual(future.reason, "timestamp_in_future")
        self.clock.now = 10.0
        too_old = self.speech_start(at=0.0)
        self.assertEqual(too_old.reason, "event_too_late")
        self.clock.now = 1_000_000.0
        expired = self.session.expire_due()
        self.assertTrue(expired.silent_expire)
        self.assertEqual(self.session.phase, TurnPhase.IDLE)

    def test_r5_timestamp_before_invalidation_is_rejected(self) -> None:
        self.listen()
        prepared = self.session.acquisition()
        self.clock.now = 5.0
        self.session.cancel()
        self.clock.now = 6.0
        stale = self.session.wake(
            arm_id=prepared.arm_id,
            epoch=prepared.epoch,
            at=5.0,
            residual="avant barrière",
        )
        self.assertEqual(stale.reason, "stale_source")
        self.assertEqual(self.session.permit, TalkPermit.IDLE)

        self.session.off()
        self.clock.now = 10.6
        self.listen()
        current = self.session.acquisition()
        forged = self.session.wake(
            arm_id=current.arm_id,
            epoch=current.epoch,
            at=10.5,
            residual="avant armement",
        )
        self.assertEqual(forged.reason, "timestamp_before_invalidation")
        self.assertEqual(self.session.permit, TalkPermit.IDLE)
