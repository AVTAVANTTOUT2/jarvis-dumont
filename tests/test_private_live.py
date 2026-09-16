import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import test_echo_live as fixtures

from jarvis_office.echo.audio import RemoteEchoEgress
from jarvis_office.echo.protocol import Packet
from jarvis_office.storage import OfficeStore


class PrivateLiveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.LiveTests.asyncSetUp
    asyncTearDown = fixtures.LiveTests.asyncTearDown

    async def command(self, kind, **payload):
        await self.gateway.command(self.s, {"type": kind, "payload": payload}, time.monotonic_ns())

    async def test_worker_opened_metadata_is_filtered_before_retention_and_callback(self):
        await self.command("set_mode", mode="PASSIVE", command_id="numeric-open")
        await fixtures.LiveTests.wait_for(self, lambda: bool(self.remote.token))
        # Exercise the real VoiceLoop rearm consumer as well as the diagnostic ring.
        for value in (
            "worker-number-fixture",
            {},
            [],
            None,
            True,
            float("nan"),
            float("inf"),
            -1,
            2**63,
            10**400,
            12,
            12.5,
        ):
            with self.subTest(value_type=type(value).__name__):
                event = {
                    "event": "listening",
                    "session": self.voice.session,
                    "turn": self.voice.turn,
                    "data": {"device": {}, "opened_at": value},
                }
                self.remote.event(event)
                valid = type(value) in (int, float) and 0 <= value < 2**63
                self.assertEqual(
                    self.remote.capture.get("worker_opened_s"), value if valid else None
                )
                if not valid:
                    self.assertTrue(self.remote.capture["diagnostic_incomplete"])
                for captures in (
                    self.remote.capture_diagnostics,
                    self.gateway.report()["capture_diagnostics"],
                ):
                    self.assertNotIn("worker-number-fixture", json.dumps(list(captures)))
                self.voice.results.append({"rearm_eligible": 1.0})
                self.remote.event(event)
                self.voice.results.clear()
        self.voice.results.clear()

    async def test_invalid_worker_timing_keeps_valid_measurements_and_transcribing_callback(self):
        await self.command("set_mode", mode="PASSIVE", command_id="numeric-timing")
        await fixtures.LiveTests.wait_for(self, lambda: bool(self.remote.token))
        for value in (
            10**400,
            {},
            [],
            "worker-number-fixture",
            True,
            float("nan"),
            float("inf"),
            -1,
            2**63,
        ):
            with self.subTest(value_type=type(value).__name__):
                self.voice.state = "listening"
                self.remote.worker_diagnostic(self.remote.capture, {"input_samples": 640})
                event = {
                    "event": "transcribing",
                    "session": self.voice.session,
                    "turn": self.voice.turn,
                    "data": {"timing": {"input_samples": value, "input_blocks": 2}},
                }
                with patch.object(
                    self.voice, "audio_event", wraps=self.voice.audio_event
                ) as callback:
                    self.remote.event(event)
                self.assertEqual(self.voice.state, "transcribing")
                self.assertFalse(self.remote.token)
                self.assertNotIn("input_samples", self.remote.capture["worker"])
                self.assertEqual(self.remote.capture["worker"]["input_blocks"], 2)
                self.assertTrue(self.remote.capture["diagnostic_incomplete"])
                delivered = callback.call_args.args[0]["data"]["timing"]
                self.assertEqual(delivered, {"input_blocks": 2})
        # The timing container itself may also be invalid JSON data.
        for timing in (None, [], "worker-number-fixture"):
            with patch.object(self.voice, "audio_event", wraps=self.voice.audio_event) as callback:
                self.remote.event(
                    {
                        "event": "transcribing",
                        "session": self.voice.session,
                        "turn": self.voice.turn,
                        "data": {"timing": timing},
                    }
                )
            self.assertEqual(callback.call_args.args[0]["data"]["timing"], {})
        self.assertNotIn("worker-number-fixture", json.dumps(self.gateway.report()))

    async def test_worker_result_metadata_is_filtered_before_voice_metrics_and_report(self):
        observed = []

        async def response_without_engines(*args, **kwargs):
            observed.append(self.gateway.report())
            self.voice.armed = False

        legitimate = {
            "capture_opened": 40.0,
            "first_block_consumed": 40.1,
            "last_block_consumed": 41.0,
            "input_closed": 41.01,
            "segmentation_reason": "terminal_silence",
            "input_blocks": 2,
            "input_audio_s": 0.04,
            "normalized_samples": 640,
            "utterance_samples": 512,
            "speech_start_estimate": None,
            "speech_end_estimate": None,
            "vad_finalized": 41.0,
            "vad_delay_s": 0.2,
            "input_clock": "UNAVAILABLE",
            "reconnects": 0,
            "pre_roll_s": 0.1,
            "terminal_silence_ms": 200,
            "normalized_rate": 16000,
            "vad_frame_samples": 512,
            "callback_dropped": 0,
            "rms_capture": 0.2,
        }
        with patch.object(self.voice, "_respond", side_effect=response_without_engines):
            await self.command("set_mode", mode="ACTIVE", command_id="result-boundary")
            await fixtures.LiveTests.wait_for(self, lambda: bool(self.remote.token))
            await self.remote.worker.results.put(
                {
                    "accepted": True,
                    "text": "Jarvis, question synthétique.",
                    "timing": {
                        **legitimate,
                        "input_samples": "worker-result-fixture",
                        "unknown_field": "worker-result-fixture",
                    },
                    "stt_started": "worker-result-fixture",
                    "stt_finished": 41.2,
                }
            )
            await fixtures.LiveTests.wait_for(self, lambda: bool(observed) or self.voice.error)
        self.assertIsNone(self.voice.error)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.tts.texts, [])
        for key, value in legitimate.items():
            expected = value
            if key == "vad_finalized":
                expected += self.voice.audio_offset
            self.assertEqual(self.voice.metrics[key], expected)
        self.assertIsNone(self.voice.metrics["stt_started"])
        self.assertEqual(self.voice.metrics["stt_finished"], 41.2 + self.voice.audio_offset)
        self.assertNotIn("stt_wait_s", self.voice.metrics)
        self.assertTrue(observed[0]["capture_diagnostics"][-1]["diagnostic_incomplete"])
        self.assertNotIn("worker-result-fixture", json.dumps(observed[0]))

    async def test_worker_callbacks_without_capture_still_filter_metadata(self):
        self.remote.capture = None
        self.remote.bound = None
        self.voice.turn = "missing-capture"
        self.voice.armed = True
        self.voice.results.append({"rearm_eligible": 1.0})
        for event, data in (
            ("listening", {"device": {}, "opened_at": "worker-callback-fixture"}),
            ("transcribing", {"timing": "worker-callback-fixture"}),
        ):
            with patch.object(self.voice, "audio_event", wraps=self.voice.audio_event) as callback:
                self.remote.event(
                    {
                        "event": event,
                        "session": self.voice.session,
                        "turn": self.voice.turn,
                        "data": data,
                    }
                )
            self.assertNotIn("worker-callback-fixture", json.dumps(callback.call_args.args[0]))
        self.assertEqual(self.voice.results, [{"rearm_eligible": 1.0}])
        self.assertEqual(self.voice.state, "transcribing")

    async def test_audio_start_failure_finalizes_without_worker_listen(self):
        self.s.mode = "PASSIVE"
        self.remote.bound = self.s
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                entered, release = asyncio.Event(), asyncio.Event()

                async def send(_, entered=entered, release=release):
                    entered.set()
                    await release.wait()
                    raise TimeoutError

                with patch.object(self.endpoint, "send", side_effect=send):
                    task = asyncio.create_task(self.remote.call("listen", turn=self.voice.turn))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        requested = self.remote.capture["status"]
                        if cancelled:
                            task.cancel()
                        else:
                            release.set()
                        with self.assertRaises(
                            asyncio.CancelledError if cancelled else TimeoutError
                        ):
                            await task
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                capture = self.gateway.report()["capture_diagnostics"][-1]
                self.assertEqual(capture["status"], "CANCELLED" if cancelled else "FAILED")
                self.assertEqual(requested, "REQUESTED")
                self.assertIsNotNone(capture["listen_finished_s"])
                for key in ("ingress_opened_s", "worker_opened_s", "ingress_closed_s"):
                    self.assertIsNone(capture[key])
                self.assertEqual(self.remote.worker.listens, 0)
                self.assertFalse(self.remote.token)
                self.assertTrue(task.done())
                self.assertFalse(self.s.lock.locked())

    async def test_failed_duplicate_ack_storage_does_not_block_active_command(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OfficeStore(fixtures.Path(directory) / "office.sqlite3")
            await store.start()
            self.gateway.store = store
            try:
                await store.update_settings({"budget": {"limit": 5}})
                await self.command("set_mode", mode="OFF", command_id="duplicate-store")
                await store.flush()
                original = store._record_event

                def fail_duplicate(db, kind, device, data):
                    if json.loads(data).get("deduplicated"):
                        raise sqlite3.OperationalError("diagnostic-only fixture")
                    return original(db, kind, device, data)

                with patch.object(store, "_record_event", side_effect=fail_duplicate):
                    await self.command("set_mode", mode="OFF", command_id="duplicate-store")
                    await store.flush()
                # Exercise the business path even when the diagnostic assertion fails before fix.
                await self.command("set_mode", mode="ACTIVE", command_id="after-store-failure")
                self.assertEqual(self.s.command_acks["after-store-failure"]["status"], "applied")
                self.assertEqual(store.diagnostic_dropped, 1)
                self.assertIsNone(store.error)
                await self.command("set_mode", mode="OFF", command_id="store-final-off")
                await store.flush()
                self.assertGreater((await store.rows(table="events"))["total"], 0)
            finally:
                await self.command("set_mode", mode="OFF", command_id="store-cleanup")
                await store.close()
                self.gateway.store = None

    async def test_duplicate_ack_invalidated_while_waiting_for_send_lock(self):
        self.gateway.store = SimpleNamespace(error=None, close_session=Mock(), record_event=Mock())
        await self.command("set_mode", mode="OFF", command_id="locked-ack")
        generation = self.s.generation
        self.endpoint.control.clear()
        entered = asyncio.Event()
        acquire = self.s.lock.acquire

        async def acquire_observed():
            entered.set()
            return await acquire()

        await self.s.lock.acquire()
        with patch.object(self.s.lock, "acquire", side_effect=acquire_observed):
            task = asyncio.create_task(
                self.command("set_mode", mode="OFF", command_id="locked-ack")
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.s.alive = False
                self.s.lock.release()
                await asyncio.wait_for(task, 1)
                audit = self.gateway.store.record_event.call_args.kwargs["details"]
                self.assertFalse(audit.get("ack_send_completed", False))
                self.assertEqual(audit["ack_send_status"], "SKIPPED_INVALIDATED")
                self.assertTrue(audit["deduplicated"])
                self.assertEqual(self.endpoint.control, [])
                self.assertEqual(self.s.generation, generation)
                self.assertEqual(self.remote.worker.listens, 0)
            finally:
                if self.s.lock.locked():
                    self.s.lock.release()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.s.alive = True

    async def test_capture_report_is_snapshot_while_ring_keeps_rearm_enrichment(self):
        await self.command("set_mode", mode="PASSIVE", command_id="snapshot")
        await fixtures.LiveTests.wait_for(self, lambda: bool(self.remote.token))
        first = self.remote.capture
        self.remote.worker_diagnostic(first, {"input_blocks": 2})
        exported = self.gateway.report()["capture_diagnostics"]
        snapshot = json.dumps(exported)
        await fixtures.LiveTests.say(self, "Fait local pour réarmement.")
        await fixtures.LiveTests.wait_for(self, lambda: first.get("rearmed_s") is not None)
        self.remote.worker_diagnostic(first, {"input_blocks": 3})
        self.assertEqual(json.dumps(exported), snapshot)
        self.assertEqual(first["worker"]["input_blocks"], 3)
        self.assertIsNotNone(first["rearmed_s"])

    async def test_off_then_active_preserves_passive_context_in_next_request(self):
        self.s.capabilities = {"private_state_v1"}
        await self.command("set_mode", mode="PASSIVE", command_id="passive")
        for fact in ("Le code de test est alpha.", "La couleur de test est turquoise."):
            await fixtures.LiveTests.say(self, fact)
        await fixtures.LiveTests.wait_for(self, lambda: self.s.context.count == 2)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.tts.texts, [])
        connection, conversation, epoch = self.s.id, self.voice.session, self.s.context_epoch

        await self.command("set_mode", mode="OFF", command_id="off")
        self.assertEqual(self.s.context.count, 2)
        await self.command("set_mode", mode="ACTIVE", command_id="active")
        self.assertEqual(
            (self.s.id, self.voice.session, self.s.context_epoch),
            (connection, conversation, epoch),
        )
        await fixtures.LiveTests.say(self, "Jarvis, rappelle les deux informations de test.")
        await fixtures.LiveTests.wait_for(self, lambda: len(self.voice.results) == 1)
        self.assertEqual(len(self.requests), 1)
        context = self.requests[0]["messages"][-2]
        self.assertEqual(context["role"], "user")
        self.assertIn("alpha", context["content"])
        self.assertIn("turquoise", context["content"])
        proof = self.voice.results[0]["llm"]["request_context"]
        self.assertEqual(proof["incorporated_entries"], 2)
        self.assertEqual(proof["incorporated_payload_chars"], len(context["content"]))
        self.assertEqual(proof["echo_connection_session"], connection)
        self.assertEqual(proof["conversation_session"], conversation)
        self.assertEqual(proof["context_generation"], epoch)
        captures = list(self.remote.capture_diagnostics)
        self.assertEqual(proof["selected_entry_ids"], [c["context_entry_id"] for c in captures[:2]])
        self.assertEqual(proof["turn_id"], self.voice.results[0]["turn"])
        self.assertEqual(self.s.context.count, 2)
        await self.command("set_mode", mode="OFF", command_id="final-off")

    async def test_command_ack_deduplicates_and_requested_never_claims_physical_capture(self):
        self.s.capabilities = {"private_state_v1"}
        self.gateway.store = SimpleNamespace(
            error=None,
            close_session=Mock(),
            record_event=Mock(),
            settings=AsyncMock(return_value={"budget": {"limit": 5, "used": 0}}),
        )
        await self.command("set_mode", mode="ACTIVE", command_id="activate", surface="dashboard")
        await asyncio.sleep(0)
        generation, stream = self.s.generation, self.s.up_stream
        await self.gateway.dashboard_command(
            {
                "device_id": self.s.device,
                "action": "set_mode",
                "mode": "ACTIVE",
                "command_id": "activate",
                "surface": "echo",
            }
        )
        self.assertEqual((self.s.generation, self.s.up_stream), (generation, stream))
        state = self.gateway.product_state(self.s)
        self.assertEqual(state["confirmed_mode"], "ACTIVE")
        self.assertIsNone(state["physical"]["microphone"])
        self.assertEqual(self.s.command_acks["activate"]["status"], "applied")
        events = [call.kwargs["details"] for call in self.gateway.store.record_event.call_args_list]
        self.assertEqual([e["surface"] for e in events], ["echo", "dashboard"])
        self.assertEqual([e["deduplicated"] for e in events], [False, True])
        self.assertEqual([e["generation"] for e in events], [generation, generation])
        for event in events:
            self.assertEqual(event["ack_send_status"], "SENT")
            self.assertEqual(event["command_id"], "activate")
            self.assertEqual(event["echo_connection_session"], self.s.id)
            self.assertEqual(event["server_epoch"], self.gateway.server_epoch)
            times = [
                event[key]
                for key in ("received_ns", "processing_ns", "decision_ns", "ack_finished_ns")
            ]
            self.assertEqual(times, sorted(times))
            self.assertLess(len(json.dumps(event)), 4096)
        self.assertIsNone(events[0]["turn_id"])
        await self.command("set_mode", mode="OFF", command_id="stop")
        self.assertEqual(self.s.mode, "OFF")
        with patch.object(self.endpoint, "send", side_effect=ConnectionError("private detail")):
            with self.assertRaises(ConnectionError):
                await self.command("set_mode", mode="OFF", command_id="stop")
        failed_ack = self.gateway.store.record_event.call_args.kwargs["details"]
        self.assertTrue(failed_ack["deduplicated"])
        self.assertEqual(failed_ack["ack_send_status"], "FAILED")
        self.assertNotIn("private detail", json.dumps(failed_ack))

    async def test_owner_mode_survives_reconnect_and_capture_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = OfficeStore(fixtures.Path(root) / "office.sqlite3")
            await store.start()
            self.gateway.store = store
            try:
                await store.update_settings({"budget": {"limit": 5}})
                await self.command("set_mode", mode="ACTIVE", command_id="keep-alive")
                await fixtures.LiveTests.wait_for(self, lambda: self.voice.state == "listening")
                await store.flush()
                self.assertEqual(await store.echo_mode("test"), "ACTIVE")
                await self.remote.worker.results.put({"silence": True})
                await fixtures.LiveTests.wait_for(self, lambda: self.voice.task.done())
                self.voice.error = "capture_disconnected"
                self.voice.state = "error"
                self.s.welcomed = True
                with patch("jarvis_office.echo.live.atomic_json"):
                    publisher = asyncio.create_task(self.gateway.publish(fixtures.Path("unused")))
                    try:
                        await fixtures.LiveTests.wait_for(
                            self, lambda: self.remote.worker.listens >= 2
                        )
                    finally:
                        publisher.cancel()
                        await asyncio.gather(publisher, return_exceptions=True)
                self.assertEqual(self.s.mode, "ACTIVE")
                self.assertIsNone(self.voice.error)
                await self.gateway.drop(self.s)
                self.s = fixtures.Session(
                    "test",
                    self.endpoint,
                    fixtures.PassiveContextBuffer(),
                    audio=self.endpoint,
                    rate=16000,
                )
                self.endpoint.session = self.s
                self.gateway.sessions["test"] = self.s
                self.s.welcomed = True
                await self.gateway.on_audio_ready(self.s)
                await fixtures.LiveTests.wait_for(self, lambda: self.s.mode == "ACTIVE")
                self.assertEqual(self.s.mode, "ACTIVE")
                self.assertTrue(self.voice.armed)
            finally:
                self.gateway.store = None
                await store.close()

    async def test_internal_window_renewal_has_no_invented_command_or_turn(self):
        self.gateway.store = SimpleNamespace(
            error=None,
            close_session=Mock(),
            record_event=Mock(),
            settings=AsyncMock(return_value={"budget": {"limit": 5, "used": 0}}),
        )
        self.s.welcomed = True
        await self.command("set_mode", mode="PASSIVE", command_id="internal-test")
        await self.remote.worker.results.put({"silence": True})
        await fixtures.LiveTests.wait_for(self, lambda: self.voice.task.done())
        recorded = self.gateway.store.record_event.call_count
        with patch("jarvis_office.echo.live.atomic_json"):
            publisher = asyncio.create_task(self.gateway.publish(fixtures.Path("unused")))
            try:
                await fixtures.LiveTests.wait_for(self, lambda: self.remote.worker.listens == 2)
            finally:
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)
        self.assertEqual(self.s.mode, "PASSIVE")
        self.assertEqual(self.gateway.store.record_event.call_count, recorded)
        self.assertEqual((self.requests, self.tts.texts), ([], []))

    async def test_missing_budget_rejects_without_microphone_or_paid_request(self):
        self.gateway.store = SimpleNamespace(
            error=None,
            close_session=Mock(),
            record_event=Mock(),
            settings=AsyncMock(return_value={"budget": {"limit": None, "used": 0}}),
        )
        await self.command("set_mode", mode="PASSIVE", command_id="budget")
        self.assertEqual(self.s.mode, "OFF")
        self.assertEqual(self.s.command_acks["budget"]["error"], "NOMINAL_BUDGET_REQUIRED")
        self.assertFalse(self.voice.armed)
        self.assertEqual(self.requests, [])
        event = self.gateway.store.record_event.call_args.kwargs["details"]
        self.assertEqual(
            (event["status"], event["mode"], event["confirmed_mode"]),
            ("rejected", "PASSIVE", "OFF"),
        )
        self.assertEqual(event["generation_before"], event["generation"])

    async def test_capture_metadata_correlates_closed_gap_rearm_clear_and_bounded_loss(self):
        worker = self.remote.worker
        worker.feed_remote = Mock()
        original_call = worker.call
        gap, release = asyncio.Event(), asyncio.Event()
        fixture_text = "Fait de fixture exclusivement local."

        async def call(op, **kwargs):
            result = await original_call(op, **kwargs)
            if op == "listen" and not gap.is_set():
                gap.set()
                await release.wait()
                result.update(
                    timing={
                        "input_samples": 640,
                        "input_blocks": 2,
                        "input_audio_s": 0.04,
                        "segmentation_reason": "terminal_silence",
                        "vad_finalized": 41.0,
                        "input_closed": 41.01,
                    },
                    stt_started=41.02,
                    stt_finished=41.2,
                )
            return result

        worker.call = call
        await self.command("set_mode", mode="PASSIVE", command_id="metadata-start")
        await fixtures.LiveTests.wait_for(self, lambda: bool(self.remote.token))
        first = self.remote.capture
        stream = self.s.up_stream
        for sequence in (1, 2):
            self.remote.feed(
                self.s, Packet(stream, sequence, 10, 10, 16000, 1, bytes(640)), 1000 + sequence
            )
        await fixtures.LiveTests.say(self, fixture_text)
        await asyncio.wait_for(gap.wait(), 1)
        self.assertFalse(self.remote.token)
        self.remote.feed(self.s, Packet(stream, 3, 10, 10, 16000, 1, bytes(640)), 1003)
        release.set()
        await fixtures.LiveTests.wait_for(
            self, lambda: self.s.context.count == 1 and first.get("rearmed_s") is not None
        )
        self.assertEqual(
            (
                first["received_frames"],
                first["forwarded_frames"],
                first["ignored_frames"],
                first["ignored_while_closed_frames"],
            ),
            (3, 2, 1, 1),
        )
        self.assertEqual((first["received_samples"], first["forwarded_samples"]), (960, 640))
        self.assertEqual(first["worker"]["input_samples"], 640)
        self.assertEqual(
            first["worker"]["stt_finished"] - first["worker"]["stt_started"], 41.2 - 41.02
        )
        self.assertEqual(first["segmentation_reason"], "terminal_silence")
        self.assertEqual(first["produced_chars"], len(fixture_text))
        self.assertGreaterEqual(first["input_closed_interval_s"], 0)
        self.assertLessEqual(first["ingress_closed_s"], first["context_delivery_s"])
        self.assertLessEqual(first["context_delivery_s"], first["rearmed_s"])
        self.assertEqual(worker.feed_remote.call_count, 2)
        current = self.remote.capture
        current["received_frames"] = 2**63 - 1
        worker.feed_remote.side_effect = fixtures.LoopError("remote_ingress_overrun")
        with self.assertRaises(fixtures.LoopError):
            self.remote.feed(
                self.s, Packet(self.s.up_stream, 1, 10, 10, 16000, 1, bytes(640)), 1004
            )
        self.assertEqual(current["received_frames"], 2**63 - 1)
        self.assertTrue(current["counter_saturated"])
        self.assertEqual((current["ingress_failed_frames"], current["forwarded_frames"]), (1, 0))
        worker.feed_remote.side_effect = None
        self.assertEqual((self.requests, self.tts.texts), ([], []))
        for count in range(21):
            await fixtures.LiveTests.say(self, fixture_text)
            await fixtures.LiveTests.wait_for(
                self, lambda count=count: self.s.context.count == count + 2
            )
        self.assertEqual(len(self.remote.capture_diagnostics), 20)
        self.assertGreater(self.remote.capture_diagnostics_overwritten, 0)
        before_clear = self.remote.capture
        old_epoch = self.s.context_epoch
        await self.command("clear_context", command_id="metadata-clear")
        await self.command("set_mode", mode="PASSIVE", command_id="metadata-rearm")
        await fixtures.LiveTests.wait_for(self, lambda: bool(self.remote.token))
        current = dict(self.remote.capture)
        stale = self.remote.capture_diagnostics_stale_events
        self.remote.event(
            {
                "event": "transcribing",
                "session": before_clear["conversation_session"],
                "turn": before_clear["turn_id"],
                "data": {"text": fixture_text},
            }
        )
        self.assertEqual(self.remote.capture, current)
        self.assertEqual(self.remote.capture_diagnostics_stale_events, stale + 1)
        self.assertEqual(self.s.context.count, 0)
        self.assertEqual(self.remote.capture["context_generation"], old_epoch + 1)
        self.assertIsNone(before_clear["rearmed_s"])
        exported = json.dumps(self.gateway.report()["capture_diagnostics"])
        self.assertNotIn(fixture_text, exported)
        self.assertNotIn("a" * 32, exported)  # Worker ingress credential is excluded.
        self.assertTrue(all(len(json.dumps(c)) < 4096 for c in self.remote.capture_diagnostics))
        self.remote.capture["oversize_test_diagnostic"] = "x" * 5000
        bounded = self.gateway.report()
        self.assertEqual(bounded["capture_diagnostics_truncated"], 1)
        self.assertEqual(
            bounded["capture_diagnostics"][-1],
            {
                "server_epoch": self.gateway.server_epoch,
                "turn_id": self.voice.turn,
                "diagnostic_truncated": True,
            },
        )
        self.assertEqual(self.s.context.count, 0)
        await self.command("set_mode", mode="OFF", command_id="metadata-final-off")

    async def test_physical_state_expires_and_old_playback_is_ignored(self):
        self.s.mode, self.s.down_stream = "ACTIVE", 21
        await self.command(
            "client_state", microphone=False, playing=True, playback_frames=960, stream_id=21
        )
        state = self.gateway.product_state(self.s)
        self.assertEqual(state["activity"], "PLAYING")
        await self.command(
            "client_state", microphone=False, playing=True, playback_frames=9000, stream_id=20
        )
        self.assertEqual(self.s.playback_frames, 960)
        self.s.physical_at -= 4
        state = self.gateway.product_state(self.s)
        self.assertIsNone(state["physical"]["playing"])
        self.assertEqual(state["connection"], "DEGRADED")

    async def test_explicit_passive_smoke_uses_only_remaining_diagnostic_authorization(self):
        self.gateway.store = SimpleNamespace(
            error=None,
            close_session=Mock(),
            record_event=Mock(),
            settings=AsyncMock(return_value={"budget": {"limit": None, "used": 0}}),
        )
        with patch("pathlib.Path.read_text", return_value='{"attempts":9,"limit":10}'):
            result = await self.gateway.dashboard_command(
                {
                    "device_id": "test",
                    "action": "passive_smoke",
                    "command_id": "smoke",
                }
            )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.gateway.diagnostic_session, self.s.id)
        self.assertEqual(self.voice.remaining, 1)
        self.assertEqual(self.requests, [])
        await self.command("set_mode", mode="OFF", command_id="stop-smoke")
        self.assertIsNone(self.gateway.diagnostic_session)
        self.assertEqual(self.s.mode, "OFF")

    async def test_archive_uses_capture_generation_and_only_confirmed_prefix(self):
        record = Mock()
        self.gateway.store = SimpleNamespace(record_turn=record, close_session=Mock())
        self.gateway.record_owner = ("test", self.s.id, "turn", 3)
        self.gateway.record_turn(
            "turn", "Jarvis question", "futur entier", "préfixe", {"status": "CANCELLED"}
        )
        self.assertEqual(record.call_args.kwargs["generation"], 3)
        self.assertEqual(record.call_args.kwargs["status"], "interrupted")
        self.assertEqual(record.call_args.kwargs["delivered_text"], "préfixe")

    async def test_mode_activation_restores_persistent_summary_and_recent_turns(self):
        with tempfile.TemporaryDirectory() as root:
            store = OfficeStore(fixtures.Path(root) / "office.sqlite3")
            await store.start()
            self.gateway.store = store
            self.voice.archive_generation = lambda: store.generation
            try:
                await store.update_settings({"memory_enabled": True, "budget": {"limit": 10}})
                store.record_turn(
                    "test",
                    "archived",
                    "summarized",
                    generation=store.generation,
                    user_text="Quelle couleur ai-je choisie ?",
                    delivered_text="Vous avez choisi turquoise.",
                    status="complete",
                )
                await store.flush()
                memory = await store.memory_context("test")
                cursor = memory["pending"][-1]
                await store.commit_memory(
                    "test",
                    generation=memory["generation"],
                    revision=memory["revision"],
                    through_created_at=cursor["created_at"],
                    through_turn_id=cursor["turn_id"],
                    summary="Le propriétaire a choisi la couleur turquoise.",
                )
                store.record_turn(
                    "test",
                    "archived",
                    "recent",
                    generation=store.generation,
                    user_text="Je préfère aussi les réponses courtes.",
                    delivered_text="Les réponses resteront courtes.",
                    status="complete",
                )
                await store.flush()

                await self.command("set_mode", mode="ACTIVE", command_id="memory-restore")
                await fixtures.LiveTests.wait_for(self, lambda: self.voice.state == "listening")

                self.assertEqual(
                    self.chat.memory_summary,
                    "Le propriétaire a choisi la couleur turquoise.",
                )
                self.assertEqual(
                    self.chat.history,
                    [
                        (
                            "Je préfère aussi les réponses courtes.",
                            "Les réponses resteront courtes.",
                        )
                    ],
                )
                await self.command("set_mode", mode="OFF", command_id="memory-stop")
            finally:
                self.gateway.store = None
                await store.close()

    async def test_four_confirmed_turns_roll_up_in_background(self):
        with tempfile.TemporaryDirectory() as root:
            store = OfficeStore(fixtures.Path(root) / "office.sqlite3")
            await store.start()
            self.gateway.store = store
            try:
                await store.update_settings({"memory_enabled": True})
                for index in range(4):
                    turn = f"memory-{index}"
                    self.gateway.record_owner = ("test", self.s.id, turn, store.generation)
                    self.gateway.record_turn(
                        turn,
                        f"Question confirmée {index}",
                        f"Réponse confirmée {index}",
                        f"Réponse confirmée {index}",
                        {"status": "PASS"},
                    )
                async with asyncio.timeout(2):
                    while self.gateway.memory_task is None or not self.gateway.memory_task.done():
                        await asyncio.sleep(0.01)

                memory = await store.memory_context("test")
                self.assertIn("Turquoise", memory["summary"])
                self.assertEqual(memory["pending"], [])
            finally:
                self.gateway.store = None
                await store.close()

    async def test_envelope_is_bounded_and_does_not_change_pcm(self):
        samples = np.concatenate((np.zeros(960, np.float32), np.full(1920, 0.1, np.float32)))
        outputs = []
        for animation in (False, True):
            self.s.mode, self.s.up_stream = "ACTIVE", 17
            self.s.capabilities = {"playback_envelope_v1"} if animation else set()
            output = RemoteEchoEgress(self.s, self.settings, "mouth", 24000)
            await output.start()
            self.endpoint.packets.clear()
            await output._output(samples, time.perf_counter())
            outputs.append([p.pcm for p in self.endpoint.packets])
            if animation:
                values = list(self.s.envelope_queue)
                self.assertEqual([x[2] for x in values], [0, 960, 1920])
                self.assertEqual(values[0][3], 0)
                self.assertGreater(values[1][3], 0)
            await output.abort()
            self.assertEqual(len(self.s.envelope_queue), 0)
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
