"""Deterministic cancellation interleavings; no model, device or real HTTP transport."""

import asyncio
import contextlib
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from test_voice import FakeAudio, FakeTTS, SSEStream

from jarvis_office.audio_client import LoopError
from jarvis_office.config import Config
from jarvis_office.deepseek import DeepSeek
from jarvis_office.tts import TTSError
from jarvis_office.voice import VoiceLoop

TEXTS = ["Une première phrase complète. ", "Une deuxième phrase distincte. "]


class CancelProbe(asyncio.Task):
    def __init__(self, coroutine, trace):
        super().__init__(coroutine)
        self.requested = asyncio.Event()
        self.trace = trace

    def cancel(self, msg=None):
        self.trace.append("cancel_requested")
        self.requested.set()
        return super().cancel(msg)


class ControlledTTS(FakeTTS):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace
        self.fail_second = False

    async def stream(self, text, cancel=None):
        self.texts.append(text)
        segment = len(self.texts)
        self.trace.append(f"tts_start:{segment}")
        try:
            if self.fail_second and segment == 2:
                raise TTSError("injected_producer_dead")
            yield b"\x01\x00" * 2400
        finally:
            self.trace.append(f"producer_finished:{segment}")


class ControlledAudio(FakeAudio):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace
        self.finalizer_entered = asyncio.Event()
        self.release_abort = asyncio.Event()
        self.abort_calls = 0

    async def call(self, op, **kwargs):
        result = await super().call(op, **kwargs)
        if op == "pcm":
            self.trace.append("first_delivery" if self.pcm == 4800 else "delivery")
        if op == "begin":
            self.trace.append(f"turn:{kwargs['turn']}")
        if op == "mark":
            self.trace.append(f"completed_segment:{kwargs['segment']}")
        return result

    async def abort(self, turn):
        self.abort_calls += 1
        self.trace.append(f"abort_started:{self.abort_calls}")
        if self.abort_calls == 1:
            self.finalizer_entered.set()
            try:
                await self.release_abort.wait()
            except asyncio.CancelledError:
                self.trace.append("abort_interrupted")
                raise
        result = await super().abort(turn)
        self.trace.append("abort_finished")
        self.trace.append("consumer_finished")
        return result


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.trace = []
        self.baseline_tasks = asyncio.all_tasks()
        self.unhandled = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, event: self.unhandled.append(event)
        )
        self.streams = []

        def handler(request):
            stream = SSEStream(TEXTS, delay=0)
            self.streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.chat = DeepSeek("test_key_only", transport=httpx.MockTransport(handler))
        self.audio, self.tts = ControlledAudio(self.trace), ControlledTTS(self.trace)
        self.voice = VoiceLoop(
            Config(), Path("unused.toml"), self.chat, audio=self.audio, tts=self.tts
        )
        commit = self.chat._commit

        def confirm(*args):
            self.trace.append("history_confirmation")
            commit(*args)

        self.chat._commit = confirm
        await self.voice.start()

    async def asyncTearDown(self):
        self.audio.release_abort.set()
        await self.voice.control("stop")
        self.assertEqual(self.unhandled, [])
        self.assertEqual(
            asyncio.all_tasks() - self.baseline_tasks - {asyncio.current_task()}, set()
        )

    async def test_cancel_during_failure_finalizer_keeps_completed_prefix(self):
        self.tts.fail_second = True
        owner = CancelProbe(self.voice.test_text("Jarvis, réponds.", no_play=False), self.trace)
        self.voice.task = owner
        await asyncio.wait_for(self.audio.finalizer_entered.wait(), 1)
        self.assertEqual(self.voice.completed, [1])
        control = asyncio.create_task(self.voice.control("cancel"))
        await asyncio.wait_for(owner.requested.wait(), 1)
        self.audio.release_abort.set()
        await asyncio.wait_for(control, 1)
        self.assertEqual(len(self.chat.history), 1, self.trace)
        self.assertIn(TEXTS[0].strip(), self.chat.history[0][1])
        self.assertNotIn(TEXTS[1].strip(), self.chat.history[0][1])
        self.assertIn("interrompue", self.chat.history[0][1])
        self.assertNotIn("abort_interrupted", self.trace)
        self.assertLess(
            self.trace.index("abort_finished"), self.trace.index("history_confirmation")
        )
        self.assertEqual(self.voice.metrics["status"], "CANCELLED")
        self.assertTrue(owner.done())
        self.assertIsNone(self.chat.active)
        self.assertTrue(all(stream.closed for stream in self.streams))
        with contextlib.suppress(asyncio.CancelledError, LoopError):
            await owner

    async def _cancel_boundary(self, op, *, after, expected):
        original = self.audio.call
        boundary = asyncio.Event()
        old_turn = None

        async def call(command, **kwargs):
            nonlocal old_turn
            if command != op or boundary.is_set():
                return await original(command, **kwargs)
            old_turn = kwargs["turn"]
            if after:
                await original(command, **kwargs)
            self.trace.append(f"boundary:{op}:{after}")
            boundary.set()
            await asyncio.Future()  # Released only by cancellation, not a timing guess.

        self.audio.call = call
        self.audio.release_abort.set()
        owner = asyncio.create_task(self.voice.test_text("Jarvis, réponds.", no_play=False))
        self.voice.task = owner
        await asyncio.wait_for(boundary.wait(), 1)
        await asyncio.wait_for(self.voice.control("cancel"), 1)
        self.assertTrue(owner.done())
        self.assertEqual(self.voice.completed, list(range(1, expected + 1)))
        self.assertEqual(len(self.chat.history), bool(expected), self.trace)
        if expected:
            for text in TEXTS[:expected]:
                self.assertIn(text.strip(), self.chat.history[0][1])
            for text in TEXTS[expected:]:
                self.assertNotIn(text.strip(), self.chat.history[0][1])
            self.assertFalse(self.voice.metrics["llm"]["confirmation_complete"])
        self.assertEqual(self.voice.metrics["status"], "CANCELLED")
        self.assertFalse(self.voice.armed)
        self.assertEqual(self.voice.state, "paused")
        self.audio.call = original
        await asyncio.wait_for(self.voice.test_text("Jarvis, nouvelle demande.", no_play=False), 1)
        self.assertEqual(self.voice.metrics["status"], "PASS")
        history = list(self.chat.history)
        completed = list(self.voice.completed)
        self.voice._progress({"completed_segments": [1, 2, 3]}, old_turn, final=True)
        self.assertEqual(self.chat.history, history)
        self.assertEqual(self.voice.completed, completed)

    async def test_cancel_before_first_pcm(self):
        await self._cancel_boundary("pcm", after=False, expected=0)

    async def test_cancel_after_first_pcm_is_not_segment_confirmation(self):
        await self._cancel_boundary("pcm", after=True, expected=0)

    async def test_cancel_before_segment_completion(self):
        await self._cancel_boundary("mark", after=False, expected=0)

    async def test_cancel_between_completed_segment_and_progress_reply(self):
        await self._cancel_boundary("mark", after=True, expected=1)

    async def test_cancel_after_playback_before_history_confirmation(self):
        await self._cancel_boundary("drained", after=True, expected=2)

    async def test_late_pcm_reply_after_cancel_does_not_confirm_future_text(self):
        original = self.audio.call
        boundary = asyncio.Event()

        async def call(op, **kwargs):
            if op != "pcm":
                return await original(op, **kwargs)
            boundary.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.trace.append("late_pcm_reply")
                return {"completed_segments": [1], "first_driver": 100}

        self.audio.call = call
        self.audio.release_abort.set()
        self.voice.task = asyncio.create_task(
            self.voice.test_text("Jarvis, réponds.", no_play=False)
        )
        await asyncio.wait_for(boundary.wait(), 1)
        await asyncio.wait_for(self.voice.control("cancel"), 1)
        self.assertIn("late_pcm_reply", self.trace)
        self.assertEqual(self.chat.history, [])
        self.assertEqual(self.voice.completed, [])
        self.assertNotIn("first_driver", self.voice.metrics)

    async def test_tts_drain_is_joined_before_resume(self):
        draining, release = asyncio.Event(), asyncio.Event()
        original = self.tts.stream

        async def stream(text, cancel=None):
            try:
                yield b"\x01\x00" * 2400
                await asyncio.Future()
            finally:
                self.trace.append("drain_started")
                draining.set()
                await release.wait()
                self.trace.append("drain_finished")

        delivered = asyncio.Event()
        call = self.audio.call

        async def audio(op, **kwargs):
            result = await call(op, **kwargs)
            if op == "pcm":
                delivered.set()
            return result

        self.tts.stream, self.audio.call = stream, audio
        self.audio.release_abort.set()
        self.voice.task = asyncio.create_task(
            self.voice.test_text("Jarvis, réponds.", no_play=False)
        )
        await asyncio.wait_for(delivered.wait(), 1)
        cancel = asyncio.create_task(self.voice.control("cancel"))
        await asyncio.wait_for(draining.wait(), 1)
        resume = asyncio.create_task(self.voice.control("resume"))
        self.assertFalse(cancel.done())
        self.assertFalse(self.voice.armed)
        release.set()
        await asyncio.wait_for(asyncio.gather(cancel, resume), 1)
        self.assertIn("drain_finished", self.trace)
        self.assertEqual(self.chat.history, [])
        await asyncio.wait_for(self.voice.task, 1)
        self.tts.stream = original
        await asyncio.wait_for(self.voice.test_text("Jarvis, encore.", no_play=False), 1)
        self.assertEqual(self.voice.metrics["status"], "PASS")

    async def test_progress_is_monotonic_and_turn_scoped(self):
        self.voice.turn = "current"
        self.voice.metrics = {"turn": "current"}
        self.voice._progress({"completed_segments": [1]}, "current")
        self.voice._progress({"completed_segments": []}, "current")
        self.voice._progress({"completed_segments": [1, 2]}, "old")
        self.assertEqual(self.voice.completed, [1])
        with self.assertRaisesRegex(LoopError, "invalid_playback_confirmation"):
            self.voice._progress({"completed_segments": [2]}, "current")

    async def test_full_pcm_queue_cancel_then_new_turn(self):
        original = self.audio.call
        full = asyncio.Event()

        async def call(op, **kwargs):
            result = await original(op, **kwargs)
            if op == "progress" and len(self.tts.texts) >= 2:
                result["free_source_bytes"] = 0
                self.trace.append("pcm_queue_full")
                full.set()
            return result

        self.audio.call = call
        self.audio.release_abort.set()
        owner = asyncio.create_task(self.voice.test_text("Jarvis, réponds.", no_play=False))
        self.voice.task = owner
        await asyncio.wait_for(full.wait(), 1)
        await asyncio.wait_for(self.voice.control("cancel"), 1)
        self.assertEqual(self.voice.metrics["confirmed_segments"], 1)
        self.assertEqual(len(self.chat.history), 1)
        self.assertFalse(self.voice.metrics["llm"]["confirmation_complete"])
        self.assertTrue(owner.done())
        self.audio.call = original
        await asyncio.wait_for(self.voice.test_text("Jarvis, encore.", no_play=False), 1)
        self.assertEqual(len(self.chat.history), 2)
        self.assertTrue(self.voice.metrics["llm"]["confirmation_complete"])

    async def test_repeated_cancel_during_finalization_then_reset(self):
        self.tts.fail_second = True
        owner = CancelProbe(self.voice.test_text("Jarvis, réponds.", no_play=False), self.trace)
        self.voice.task = owner
        await asyncio.wait_for(self.audio.finalizer_entered.wait(), 1)
        owner.cancel()
        owner.cancel()
        clear = asyncio.create_task(self.voice.control("clear"))
        old_session = self.voice.session
        self.audio.release_abort.set()
        await asyncio.wait_for(clear, 1)
        self.assertEqual(self.trace.count("history_confirmation"), 1)
        self.assertEqual(self.chat.history, [])
        self.assertNotEqual(self.voice.session, old_session)
        self.assertNotIn("abort_interrupted", self.trace)
        self.assertTrue(owner.done())
        await self.voice.control("clear")
        self.assertEqual(self.chat.history, [])
        self.tts.fail_second = False
        await asyncio.wait_for(self.voice.test_text("Jarvis, encore.", no_play=False), 1)
        self.assertEqual(len(self.chat.history), 1)
        await self.voice.control("stop")
        await self.voice.control("stop")
        self.assertTrue(self.voice.shutdown_verified)

    async def _failed_stage(self, producer):
        self.audio.release_abort.set()
        original = self.audio.call
        self.tts.fail_second = producer

        async def call(op, **kwargs):
            if op == "pcm" and len(self.tts.texts) == 2:
                self.trace.append("consumer_finished:error")
                raise LoopError("injected_consumer_dead")
            return await original(op, **kwargs)

        if not producer:
            self.audio.call = call
        with self.assertRaises(LoopError):
            await asyncio.wait_for(self.voice.test_text("Jarvis, réponds.", no_play=False), 1)
        self.assertEqual(self.voice.metrics["status"], "FAIL")
        self.assertEqual(len(self.chat.history), 1)
        self.assertFalse(self.voice.metrics["llm"]["confirmation_complete"])
        self.assertEqual(self.voice.metrics["confirmed_segments"], 1)
        self.tts.fail_second = False
        self.audio.call = original
        await asyncio.wait_for(self.voice.test_text("Jarvis, encore.", no_play=False), 1)
        self.assertEqual(self.voice.metrics["status"], "PASS")

    async def test_producer_dies_after_completed_prefix(self):
        await self._failed_stage(producer=True)

    async def test_consumer_dies_after_completed_prefix(self):
        await self._failed_stage(producer=False)

    async def _shutdown_deadline(self, unverified):
        entered, released = asyncio.Event(), asyncio.Event()

        async def owned():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await released.wait()

        owner = asyncio.create_task(owned())
        self.voice.task = owner
        await entered.wait()
        self.audio.release_abort.set()
        wait_for, wait, close = asyncio.wait_for, asyncio.wait, self.tts.close

        async def deadline(awaitable, timeout):
            if timeout == self.voice.config.tts.drain_timeout + 5:
                raise TimeoutError
            return await wait_for(awaitable, timeout)

        async def reaped(tasks, *, timeout):
            if unverified and tasks == {owner} and timeout == 4:
                return set(), {owner}
            return await wait(tasks, timeout=timeout)

        async def close_worker():
            await close()
            self.tts.ready = {}
            if not unverified:
                released.set()

        self.tts.close = close_worker
        try:
            with patch("asyncio.wait_for", deadline), patch("asyncio.wait", reaped):
                if unverified:
                    with self.assertRaisesRegex(LoopError, "turn_shutdown_unverified"):
                        await self.voice.control("cancel")
                    self.assertIs(self.voice.task, owner)
                    self.assertFalse(owner.done())
                    self.assertFalse(self.voice.ready)
                    await self.voice.control("resume")
                    self.assertIs(self.voice.task, owner)
                    self.assertFalse(self.voice.armed)
                else:
                    await self.voice.control("cancel")
                    self.assertTrue(owner.done())
                    self.assertIsNone(self.voice.task)
                    self.assertFalse(self.voice.ready)
        finally:
            released.set()
            with contextlib.suppress(asyncio.CancelledError):
                await owner

    async def test_shutdown_timeout_closes_and_joins_only_owned_workers(self):
        await self._shutdown_deadline(unverified=False)

    async def test_unverified_shutdown_keeps_task_owned_and_refuses_resume(self):
        await self._shutdown_deadline(unverified=True)


if __name__ == "__main__":
    unittest.main()
