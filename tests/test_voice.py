"""Integration contracts without a microphone, model, secret or network."""

import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np

from jarvis_office.audio_client import AudioClient, LoopError, source_signature
from jarvis_office.audio_input import AudioError, Utterance
from jarvis_office.audio_output import Playback, resolve_output
from jarvis_office.audio_worker import AudioEngine
from jarvis_office.config import Chat, Config, ConfigError, Speech, Voice, load_config
from jarvis_office.credentials import reserve_validation_request
from jarvis_office.deepseek import DeepSeek
from jarvis_office.local_ui import PAGE, LocalUI
from jarvis_office.stt import Recognizer
from jarvis_office.tts import TTSError, worker_environment
from jarvis_office.voice import VoiceLoop, addressed, run_command


class SSEStream(httpx.AsyncByteStream):
    def __init__(self, texts, delay=0.01):
        self.texts, self.delay, self.ended, self.closed = texts, delay, False, False

    async def __aiter__(self):
        for text in self.texts:
            await asyncio.sleep(self.delay)
            yield (
                "data: "
                + json.dumps(
                    {
                        "model": "deepseek-v4-flash",
                        "choices": [{"delta": {"content": text}, "finish_reason": None}],
                    }
                )
                + "\n\n"
            ).encode()
        self.ended = True
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


class FakeTTS:
    def __init__(self):
        self.ready = {"sample_rate": 24000, "code_signature": source_signature()}
        self.last_metrics = {}
        self.last_drain_seconds = self.restarts = 0
        self.closed = False
        self.texts = []
        self.delay = 0.005
        self.fail = False
        self.started = asyncio.Event()

    async def start(self):
        pass

    async def stream(self, text, cancel=None):
        self.texts.append(text)
        self.started.set()
        for _ in range(2):
            await asyncio.sleep(self.delay)
            if self.fail:
                raise TTSError("injected_worker_dead")
            if cancel is not None and cancel.is_set():
                return
            yield b"\x01\x00" * 2400
        self.last_metrics = {"pcm_bytes": 9600, "first_model_audio_s": 0.001}

    async def close(self):
        self.closed = True


class FakeAudio:
    def __init__(self):
        self.ready = {"code_signature": source_signature()}
        self.calls = []
        self.session = ""
        self.completed = []
        self.ended = False
        self.driver = None
        self.pcm = 0
        self.closed = False
        self.fail = None

    async def start(self):
        pass

    async def call(self, op, *, turn="", timeout=5, **args):
        self.calls.append(op)
        if self.fail == op:
            raise LoopError("injected_audio_failure")
        if op == "clock":
            return {"clock": time.perf_counter()}
        if op in {"check", "begin"}:
            self.ended = False
            self.completed = []
            self.driver = None
            return {"device": {"name": "EXPLICIT TEST OUTPUT"}}
        if op == "pcm":
            self.pcm += len(base64.b64decode(args["pcm"]))
            self.driver = self.driver or time.perf_counter()
        if op == "mark":
            self.completed.append(args["segment"])
        if op == "finish":
            self.ended = True
        if op == "listen":
            await asyncio.sleep(0.05)
            return {"silence": True}
        return {
            "completed_segments": list(self.completed),
            "done": self.ended,
            "first_driver": self.driver,
            "pcm_driver_bytes": self.pcm,
            "underflows": 0,
        }

    async def abort(self, turn):
        self.calls.append("abort")
        return {"completed_segments": list(self.completed)}

    async def close(self):
        self.closed = True


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.streams = []
        self.texts = [
            "Voici une première phrase utile. ",
            "La longueur est 3,14 mètres. ",
            "Ne supprime pas la négation. ",
        ]
        self.delay = 0.015

        def handler(request):
            stream = SSEStream(self.texts, self.delay)
            self.streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.chat = DeepSeek("test_key_only", Chat(), transport=httpx.MockTransport(handler))
        self.audio, self.tts = FakeAudio(), FakeTTS()
        self.voice = VoiceLoop(
            Config(), Path("unused.toml"), self.chat, audio=self.audio, tts=self.tts
        )
        await self.voice.start()

    async def asyncTearDown(self):
        await self.voice.control("stop")

    async def test_progressive_order_and_ten_successive_turns(self):
        for _ in range(10):
            await self.voice.test_text("Jarvis, explique la suite.", no_play=False)
            self.assertEqual(self.voice.metrics["status"], "PASS")
            self.assertEqual(self.voice.metrics["confirmed_segments"], 3)
        self.assertEqual(len(self.streams), 10)
        self.assertTrue(all(s.closed for s in self.streams))
        self.assertLessEqual(len(self.chat.history), 4)
        self.assertEqual(self.tts.texts[:3], [t.strip() for t in self.texts])
        self.assertLess(
            self.voice.metrics["first_pcm_delivered"],
            self.voice.metrics["request_started"] + self.voice.metrics["llm"]["text_end_s"],
        )
        self.assertEqual(self.audio.calls.count("begin"), 10)
        self.assertEqual(self.audio.calls.count("drained"), 10)

    async def test_unaddressed_and_bare_name_never_call_llm(self):
        for text in [
            "Merci pour ta réponse, explique-moi la suite",
            "L’une de mes questions concerne les camions",
            "Je cite Jarvis.",
            "Jarvisien bonjour",
        ]:
            await self.voice._respond(text, "test")
            self.assertEqual(self.voice.accepted, "")
        await self.voice._respond("JARVIS !", "test")
        self.assertEqual(len(self.streams), 0)
        self.assertEqual(self.tts.texts, [])

    async def test_fast_network_slow_tts_text_budget(self):
        self.voice.config = replace(self.voice.config, voice=Voice(text_queue_chars=256))
        self.texts = ["Une phrase complète suffisamment longue pour être segmentée. "] * 12
        self.delay = 0.001
        self.tts.delay = 0.2
        with self.assertRaisesRegex(LoopError, "text_queue_full"):
            await self.voice.test_text("Jarvis, réponds.", no_play=True)
        self.assertEqual(self.chat.history, [])
        self.assertTrue(self.streams[-1].closed)
        self.assertLessEqual(self.voice.metrics["text_queue_peak_chars"], 256)

    async def test_cancel_then_second_request_and_clear(self):
        self.tts.delay = 0.1
        task = asyncio.create_task(self.voice.test_text("Jarvis, réponds.", no_play=False))
        self.voice.task = task
        await self.tts.started.wait()
        await self.voice.control("pause")
        self.assertTrue(task.done())
        self.assertFalse(self.voice.armed)
        self.assertEqual(self.voice.microphone, "closed")
        self.assertEqual(self.chat.history, [])
        self.tts.delay = 0.001
        await self.voice.test_text("Jarvis, recommence.", no_play=False)
        self.assertEqual(len(self.chat.history), 1)
        old = self.voice.session
        await self.voice.control("clear")
        self.assertNotEqual(old, self.voice.session)
        self.assertEqual(self.chat.history, [])
        self.assertEqual(self.voice.answer, "")
        self.voice.audio_event(
            {"session": old, "turn": "old", "event": "level", "data": {"level": 1}}
        )
        self.assertEqual(self.voice.level, 0)

    async def test_worker_error_and_recovery_no_history_promotion(self):
        self.tts.fail = True
        with self.assertRaisesRegex(LoopError, "injected_worker_dead"):
            await self.voice.test_text("Jarvis, réponds.", no_play=False)
        self.assertEqual(self.chat.history, [])
        self.tts.fail = False
        await self.voice.test_text("Jarvis, réponds.", no_play=False)
        self.assertEqual(len(self.chat.history), 1)

    async def test_dead_consumer_pcm_queue_error(self):
        self.audio.fail = "pcm"
        with self.assertRaises(LoopError):
            await asyncio.wait_for(self.voice.test_text("Jarvis, réponds.", no_play=False), 1)
        self.assertEqual(self.chat.history, [])
        self.assertIn("abort", self.audio.calls)

    async def test_silent_bounded_capture_and_duplicate_resume(self):
        await self.voice.control("resume")
        task = self.voice.task
        await self.voice.control("resume")
        self.assertIs(self.voice.task, task)
        await task
        self.assertEqual(self.voice.state, "paused")
        self.assertEqual(len(self.streams), 0)
        self.assertEqual(self.audio.calls.count("listen"), 1)

    async def test_ui_reconnection_security_and_no_side_effects(self):
        ui = LocalUI(self.voice, 8768)
        headers = {
            "host": ui.host,
            "origin": ui.origin,
            "sec-fetch-site": "same-origin",
            "x-jarvis-local": "1",
            "content-type": "application/json",
        }
        for _ in range(3):
            status, _, extra = ui.route("POST", "/bootstrap", headers, b"{}")
            self.assertEqual(status, 200)
            self.assertIn("HttpOnly", extra["Set-Cookie"])
        auth = {**headers, "cookie": ui.cookie_name + "=" + ui.token}
        self.voice.accepted = "<img src=x onerror=alert(1)>"
        self.assertEqual(ui.route("POST", "/snapshot", auth, b"{}")[0], 200)
        for changed in (
            {"host": "evil.test"},
            {"origin": "https://evil.test"},
            {"sec-fetch-site": "cross-site"},
            {"cookie": ""},
            {"x-jarvis-local": ""},
        ):
            self.assertEqual(ui.route("POST", "/snapshot", {**auth, **changed}, b"{}")[0], 403)
            self.assertEqual(
                ui.route("POST", "/control", {**auth, **changed}, b'{"action":"resume"}')[0], 403
            )
        self.assertEqual(ui.route("GET", "/control", auth, b"")[0], 403)
        self.assertEqual(ui.route("POST", "/control", auth, b'{"action":"shell"}')[0], 400)
        self.assertNotIn("innerHTML", PAGE)
        self.assertIn("textContent", PAGE)
        self.assertEqual(len(self.streams), 0)
        self.assertFalse(self.voice.armed)

    async def test_full_run_command_without_network_or_hardware(self):
        ui = SimpleNamespace(origin="http://127.0.0.1:8768", start=AsyncMock(), close=AsyncMock())
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "reports/integration.json"
            with (
                patch("jarvis_office.voice.AudioClient", return_value=self.audio),
                patch("jarvis_office.voice.TTSClient.for_config", return_value=self.tts),
                patch("jarvis_office.voice.DeepSeek", return_value=self.chat),
                patch("jarvis_office.local_ui.LocalUI", return_value=ui),
                patch("jarvis_office.credentials.load_key", return_value="test_key_only"),
                patch("jarvis_office.assets.private_root", return_value=Path(root)),
            ):
                result = await run_command(
                    Config(),
                    Path("unused"),
                    text="Jarvis, explique.",
                    no_play=True,
                    arm=False,
                    report=report,
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(report.read_text())["status"], "PASS")
            self.assertTrue(self.audio.closed)

    async def test_failed_run_still_writes_report_and_stops(self):
        ui = SimpleNamespace(origin="http://127.0.0.1:8768", start=AsyncMock(), close=AsyncMock())
        self.tts.fail = True
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "reports/integration.json"
            with (
                patch("jarvis_office.voice.AudioClient", return_value=self.audio),
                patch("jarvis_office.voice.TTSClient.for_config", return_value=self.tts),
                patch("jarvis_office.voice.DeepSeek", return_value=self.chat),
                patch("jarvis_office.local_ui.LocalUI", return_value=ui),
                patch("jarvis_office.credentials.load_key", return_value="test_key_only"),
                patch("jarvis_office.assets.private_root", return_value=Path(root)),
            ):
                result = await run_command(
                    Config(),
                    Path("unused"),
                    text="Jarvis, explique.",
                    no_play=True,
                    arm=False,
                    report=report,
                )
            data = json.loads(report.read_text())
            self.assertEqual(result, 1)
            self.assertTrue(data["owned_workers_stopped"])
            self.assertEqual(data["status"], "FAIL")

    async def test_cancel_full_pcm_backpressure_and_completed_prefix(self):
        original = self.audio.call
        hold = asyncio.Event()

        async def call(op, **kw):
            result = await original(op, **kw)
            if op == "progress" and len(self.tts.texts) > 1:
                result["free_source_bytes"] = 0
                hold.set()
            return result

        self.audio.call = call
        self.voice.task = asyncio.create_task(
            self.voice.test_text("Jarvis, explique.", no_play=False)
        )
        await asyncio.wait_for(hold.wait(), 1)
        await asyncio.wait_for(self.voice.control("cancel"), 1)
        self.assertEqual(len(self.chat.history), 1)
        self.assertIn(self.texts[0].strip(), self.chat.history[0][1])
        self.assertNotIn(self.texts[1].strip(), self.chat.history[0][1])
        self.assertIn("interrompue", self.chat.history[0][1])

    async def test_stt_deadline_closes_owned_child(self):
        original = self.audio.call

        async def call(op, **kw):
            if op == "listen":
                self.voice.stt_deadline = time.perf_counter() + 0.01
                await asyncio.sleep(2)
            return await original(op, **kw)

        self.audio.call = call
        await self.voice.control("resume")
        await asyncio.wait_for(self.voice.task, 1)
        self.assertEqual(self.voice.error, "stt_inference_timeout")
        self.assertTrue(self.audio.closed)
        self.assertEqual(len(self.streams), 0)

    async def test_cancel_during_output_preflight_never_starts_llm(self):
        original = self.audio.call
        entered = asyncio.Event()

        async def call(op, **kw):
            if op == "begin":
                entered.set()
                await asyncio.sleep(0.1)
            return await original(op, **kw)

        self.audio.call = call
        self.voice.task = asyncio.create_task(
            self.voice.test_text("Jarvis, explique.", no_play=False)
        )
        await entered.wait()
        await self.voice.control("pause")
        self.assertEqual(len(self.streams), 0)
        self.assertEqual(self.tts.texts, [])

    async def test_failed_audio_stop_is_not_reported_as_closed_or_rearmed(self):
        original = self.audio.abort
        self.audio.abort = AsyncMock(side_effect=LoopError("injected_stop_failure"))
        await self.voice.control("pause")
        self.assertEqual(self.voice.microphone, "closure_unverified")
        with self.assertRaisesRegex(LoopError, "audio_shutdown_unverified"):
            await self.voice.control("resume")
        self.assertFalse(self.voice.armed)
        self.audio.abort = original

    async def test_ipc_truncation_stale_ids_and_bounded_stderr(self):
        client = AudioClient(Config(), Path("unused"), "session", lambda e: None)
        for payload in (b"{invalid}\n", b"", b'{"id":1,"session":"old","turn":"t","result":{}}\n'):
            reader = asyncio.StreamReader(limit=98305)
            reader.feed_data(payload)
            reader.feed_eof()
            future = asyncio.get_running_loop().create_future()
            client.pending = {1: ("session", "t", future)}
            client.process = SimpleNamespace(stdout=reader)
            await client._read()
            with self.assertRaisesRegex(LoopError, "dead_or_invalid"):
                await future
        errors = asyncio.StreamReader()
        errors.feed_data(b"x" * 262144)
        errors.feed_eof()
        client.process = SimpleNamespace(stderr=errors)
        await client._stderr()
        self.assertEqual(len(client.stderr), 32768)
        self.assertEqual(client.stderr_bytes, 262144)
        client.process = None

    async def test_owned_blocked_child_is_killed_and_reaped(self):
        with tempfile.TemporaryDirectory() as root:
            client = AudioClient(Config(), Path("unused"), "session", lambda e: None)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                "print('ready',flush=True); time.sleep(30)",
                stdout=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                env=worker_environment(Path(root) / "cache"),
                start_new_session=True,
            )
            await asyncio.wait_for(process.stdout.readline(), 2)
            client.process = process
            client.owns_group = True
            future = asyncio.get_running_loop().create_future()
            client.pending = {1: ("session", "turn", future)}
            await asyncio.wait_for(client.close(), 5)
            self.assertIsNotNone(process.returncode)
            with self.assertRaisesRegex(LoopError, "closed"):
                await future

    async def test_http_parser_rejects_smuggling_and_foreign_origin(self):
        ui = LocalUI(self.voice, 8768)

        class Writer:
            def __init__(self):
                self.data = b""
                self.closed = False

            def write(self, data):
                self.data += data

            async def drain(self):
                pass

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        requests = [
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1:8768\r\nHost: evil\r\n\r\n",
            b"POST /control HTTP/1.1\r\nHost: 127.0.0.1:8768\r\nContent-Length: 100000\r\n\r\n",
            b"POST /control HTTP/1.1\r\nHost: 127.0.0.1:8768\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"POST /snapshot HTTP/1.1\r\nHost: evil\r\nContent-Length: 0\r\n\r\n",
        ]
        for raw in requests:
            reader = asyncio.StreamReader()
            reader.feed_data(raw)
            reader.feed_eof()
            writer = Writer()
            await ui.handle(reader, writer)
            self.assertTrue(writer.closed)
            self.assertNotIn(b"200", writer.data)
        self.assertEqual(ui.controls.qsize(), 0)
        with patch("asyncio.start_server", side_effect=OSError("private path")):
            with self.assertRaisesRegex(LoopError, "loopback_port_unavailable"):
                await ui.start()


class FakeSD:
    class PortAudioError(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    class CallbackStop(Exception):
        pass

    def __init__(self):
        self.streams = []

    def query_devices(self):
        return [{"name": "TV test", "max_output_channels": 2}]

    def check_output_settings(self, **args):
        pass

    def CoreAudioSettings(self, **args):
        return None

    def OutputStream(self, **args):
        stream = SimpleNamespace(active=True, start=lambda: None, calls=[])
        stream.abort = lambda **kw: stream.calls.append("abort")
        stream.stop = lambda **kw: stream.calls.append("stop")
        stream.close = lambda **kw: stream.calls.append("close")
        self.streams.append(stream)
        return stream


class OutputTests(unittest.TestCase):
    def test_late_capture_thread_preserves_pause(self):
        import threading

        engine = AudioEngine.__new__(AudioEngine)
        engine.config = Config()
        engine.lock = threading.Lock()
        engine.input = engine.playback = None
        engine.busy = True
        engine.cancelled = threading.Event()
        engine.cancelled.set()
        with patch("jarvis_office.capture.microphone_preflight", return_value={"status": "PASS"}):
            result = engine.listen("old-turn", 1)
        self.assertTrue(result["cancelled"])
        self.assertFalse(engine.busy)
        self.assertIsNone(engine.input)

    def test_pcm_credits_bound_faster_than_realtime_production(self):
        sd = FakeSD()
        p = Playback(sd, Voice(output_device="TV test"), 24000)
        chunk = b"\x01\x00" * 9600
        for _ in range(30):
            while p.progress()["free_source_bytes"] < len(chunk):
                p.callback(
                    np.zeros((960, 2), np.float32),
                    960,
                    SimpleNamespace(outputBufferDacTime=1, currentTime=1),
                    False,
                )
            p.feed(chunk)
            self.assertLessEqual(p.queued, p.capacity)
        p.mark(1)
        p.finish()
        while p.queued:
            try:
                p.callback(
                    np.zeros((960, 2), np.float32),
                    960,
                    SimpleNamespace(outputBufferDacTime=1, currentTime=1),
                    False,
                )
            except sd.CallbackStop:
                pass
        self.assertEqual(p.delivered, 30 * 19200)
        self.assertEqual(p.underflows, 0)
        p.close(abort=True)

    def test_continuous_resampling_tail_and_device_formats(self):
        for rate in (24000, 44100, 48000):
            sd = FakeSD()
            p = Playback(sd, Voice(output_device="TV test", output_rate=rate), 24000)
            pcm = (np.ones(2400) * 1000).astype("<i2").tobytes()
            p.feed(pcm[:2000])
            p.feed(pcm[2000:])
            p.mark(1)
            p.finish()
            self.assertEqual(p.produced, round(0.1 * rate))
            frames = p.produced + 10
            out = np.zeros((frames, 2), np.float32)
            with self.assertRaises(sd.CallbackStop):
                p.callback(
                    out, frames, SimpleNamespace(outputBufferDacTime=1, currentTime=1), False
                )
            self.assertEqual(p.delivered, round(0.1 * rate))
            self.assertGreater(float(np.max(out)), 0)
            p.stream.active = False
            with patch(
                "jarvis_office.audio_output.time.perf_counter", return_value=time.perf_counter() + 1
            ):
                result = p.close(abort=False)
            self.assertEqual(result["completed_segments"], [1])
            self.assertEqual(sd.streams[0].calls, ["stop", "close"])

    def test_full_pcm_starvation_abort_and_missing_output(self):
        sd = FakeSD()
        with self.assertRaisesRegex(AudioError, "missing_or_ambiguous"):
            resolve_output(sd, Voice(output_device="NOT THIS TV"))
        p = Playback(sd, Voice(output_device="TV test", pcm_seconds=0.5), 24000)
        p.feed(b"\x01\x00" * 6000)
        with self.assertRaisesRegex(AudioError, "pcm_queue_full"):
            p.feed(b"\x01\x00" * 12000)
        p.close(abort=True)
        self.assertEqual(sd.streams[0].calls, ["abort", "close"])
        p = Playback(sd, Voice(output_device="TV test"), 24000)
        with self.assertRaises(sd.CallbackAbort):
            p.callback(
                np.zeros((960, 2), np.float32),
                960,
                SimpleNamespace(outputBufferDacTime=1, currentTime=1),
                False,
            )
        self.assertEqual(p.error, "output_starvation")
        self.assertEqual(p.progress()["completed_segments"], [])

    def test_address_boundaries_and_config(self):
        for text in ["Jarvisien parle", "Jarvis2 parle", "je parle de Jarvis", "Jarvis-bonjour"]:
            self.assertIsNone(addressed(text))
        for text in ["jarvis, ne change pas 3,14 mètres", " JARVIS: ne change pas 3,14 mètres"]:
            self.assertEqual(addressed(text), "ne change pas 3,14 mètres")
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "config with spaces.toml"
            for raw in [
                "[voice]\nport=true",
                "[voice]\noutput_rate=16000",
                "[voice]\nacoustic_delay=nan",
            ]:
                p.write_text(raw)
                with self.assertRaises(ConfigError):
                    load_config(p)

    def test_stt_already_segmented_does_not_run_vad(self):
        import threading

        recognizer = Recognizer.__new__(Recognizer)
        recognizer.settings = Speech()
        recognizer.lock = threading.Lock()
        recognizer._decode = lambda audio: ("L’une de mes questions concerne les camions", [])
        result = recognizer.transcribe_utterance(
            Utterance(np.zeros(16000, np.float32), 0, 0, 8000, 16000, "terminal_silence")
        )
        self.assertTrue(result["accepted"])
        with self.assertRaisesRegex(AudioError, "truncated"):
            recognizer.transcribe_utterance(
                Utterance(np.zeros(16000, np.float32), 0, 0, 8000, 16000, "max_duration")
            )

    def test_persistent_common_api_ceiling_and_worker_environment(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("jarvis_office.credentials.private_root", return_value=Path(root)):
                self.assertEqual(
                    [reserve_validation_request() for _ in range(20)], list(range(1, 21))
                )
                with self.assertRaisesRegex(ConfigError, "budget_exhausted"):
                    reserve_validation_request()
                report = Path(root) / "config/phase05-api-budget.json"
                self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            env = worker_environment(Path(root) / "cache")
            self.assertFalse(any("KEY" in k or k == "PYTHONPATH" for k in env))
        self.assertEqual(len(source_signature()), 64)


if __name__ == "__main__":
    unittest.main()
