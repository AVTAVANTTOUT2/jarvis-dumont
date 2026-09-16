import asyncio
import json
import os
import queue
import secrets
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from test_voice import FakeTTS, SSEStream

from jarvis_office.audio_client import LoopError, source_signature
from jarvis_office.audio_ingress import PIPE_HEADER, RemotePipeIngress
from jarvis_office.audio_worker import AudioEngine
from jarvis_office.config import Config, ConfigError, Speech, Voice
from jarvis_office.credentials import reserve_validation_request
from jarvis_office.deepseek import DeepSeek
from jarvis_office.echo.audio import RemoteEchoAudio, RemoteEchoEgress
from jarvis_office.echo.context import PassiveContextBuffer
from jarvis_office.echo.gateway import EchoGateway, Session, Settings
from jarvis_office.echo.live import LiveGateway
from jarvis_office.echo.network import require_ethernet
from jarvis_office.echo.protocol import Packet
from jarvis_office.voice import VoiceLoop


class Endpoint:
    def __init__(self):
        self.control = []
        self.packets = []
        self.session = None

    async def send(self, value):
        if isinstance(value, bytes):
            self.packets.append(Packet.decode(value))
            return
        message = json.loads(value)
        self.control.append(message)
        if message["type"] == "speaking_started":
            self.session.playback_ready.set()
        elif message["type"] == "audio_end":
            self.session.playback_drained.set()

    async def close(self, *args):
        pass


class Worker:
    def __init__(self, event):
        self.event = event
        self.session = ""
        self.ready = {"code_signature": source_signature()}
        self.process = None
        self.results = asyncio.Queue()
        self.starts = self.listens = 0

    async def start(self):
        self.starts += 1

    async def call(self, op, *, turn="", **args):
        if op == "clock":
            return {"clock": time.perf_counter()}
        if op == "listen":
            self.listens += 1
            self.event(
                {
                    "event": "listening",
                    "session": self.session,
                    "turn": turn,
                    "data": {"device": {"ingress_token": "a" * 32}},
                }
            )
            result = await self.results.get()
            self.event({"event": "transcribing", "session": self.session, "turn": turn, "data": {}})
            return result
        return {}

    async def abort(self, turn):
        return {}

    async def close(self):
        self.ready = {}


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        async def respond(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEStream(["Turquoise est la couleur de test. ", "Présent pour la suite."]),
            )

        self.chat = DeepSeek("test-secret", transport=httpx.MockTransport(respond))
        self.config = Config(
            speech=Speech(input_rate=16000),
            voice=Voice(arm_seconds=10, arm_turns=5, acoustic_delay=0.1),
        )
        self.settings = Settings(
            "127.0.0.1",
            8771,
            {"test": secrets.token_urlsafe(32)},
            enabled=True,
            security="DEV_INSECURE_LAN",
            playback_prefill_ms=100,
        )
        self.remote = RemoteEchoAudio(self.config, Path("unused"), self.settings)
        self.remote.worker = Worker(self.remote.event)
        self.tts = FakeTTS()
        self.voice = VoiceLoop(
            self.config, Path("unused"), self.chat, audio=self.remote, tts=self.tts
        )
        self.remote.voice, self.remote.session = self.voice, self.voice.session
        self.gateway = LiveGateway(self.settings, self.voice, self.remote)
        self.endpoint = Endpoint()
        self.s = Session(
            "test", self.endpoint, PassiveContextBuffer(), audio=self.endpoint, rate=16000
        )
        self.endpoint.session = self.s
        self.gateway.sessions["test"] = self.s
        await self.voice.start()
        self.gateway.boot_complete = True

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.voice.control("stop")

    async def wait_for(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    async def mode(self, mode):
        await self.gateway.command(
            self.s, {"type": "set_mode", "payload": {"mode": mode}}, time.monotonic_ns()
        )

    async def say(self, text):
        await self.remote.worker.results.put({"accepted": True, "text": text})

    async def test_same_voiceloop_two_remote_turns_and_one_stream_per_turn(self):
        self.assertIs(type(self.voice), VoiceLoop)
        await self.mode("ACTIVE")
        for count in (1, 2):
            previous_packets = len(self.endpoint.packets)
            await self.say("Jarvis, réponds présent.")
            await self.wait_for(
                lambda previous_packets=previous_packets: (
                    len(self.endpoint.packets) > previous_packets
                )
            )
            self.assertEqual(len(self.voice.results), count - 1)
            await self.wait_for(lambda count=count: len(self.voice.results) == count)
            self.assertEqual(self.voice.results[-1]["status"], "PASS", self.voice.results[-1])
            await self.wait_for(lambda: self.voice.state == "listening")
        self.assertEqual(len(self.requests), 2)
        self.assertGreaterEqual(len(self.tts.texts), 4)
        self.assertEqual(self.remote.worker.starts, 1)
        starts = [m for m in self.endpoint.control if m["type"] == "speaking_started"]
        ends = [m for m in self.endpoint.control if m["type"] == "audio_end"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(ends), 2)
        streams = {m["payload"]["stream_id"] for m in starts}
        self.assertEqual(len(streams), 2)
        self.assertEqual({p.stream for p in self.endpoint.packets}, streams)
        for stream in streams:
            packets = [p for p in self.endpoint.packets if p.stream == stream]
            self.assertEqual([p.sequence for p in packets], list(range(len(packets))))
            self.assertTrue(all(p.rate == 48000 and len(p.pcm) == 1920 for p in packets))
        self.assertTrue(all("first_remote_send" in r for r in self.voice.results))
        self.assertTrue(all(r.get("first_driver") is None for r in self.voice.results))

    async def test_active_unaddressed_speech_is_a_turn(self):
        await self.mode("ACTIVE")
        await self.say("Quelle heure est-il ?")
        await self.wait_for(lambda: len(self.voice.results) == 1)
        self.assertEqual(self.voice.results[-1]["status"], "PASS", self.voice.results[-1])
        self.assertEqual(self.gateway.non_addressed, 0)
        self.assertEqual(self.s.context.count, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["messages"][-1]["content"], "Quelle heure est-il ?")
        self.assertGreaterEqual(len(self.tts.texts), 1)

    async def test_passive_zero_cloud_then_ephemeral_context_clear_and_speaking_suppression(self):
        await self.mode("PASSIVE")
        await self.say("La couleur de test est turquoise.")
        await self.wait_for(lambda: self.s.context.count == 1)
        self.assertEqual(len(self.requests), 0)
        self.assertEqual(self.tts.texts, [])
        self.s.down_stream = 123
        self.gateway.transcript_context("Voix de Jarvis pendant sa réponse.")
        self.assertEqual(self.s.context.count, 1)
        self.s.down_stream = 0
        await self.say("Jarvis, quelle est la couleur de test ?")
        await self.wait_for(lambda: len(self.voice.results) == 1)
        self.assertEqual(self.voice.results[-1]["status"], "PASS", self.voice.results[-1])
        self.assertEqual(len(self.requests), 1)
        messages = self.requests[0]["messages"]
        self.assertEqual(messages[-2]["role"], "user")
        self.assertIn("passivement entendu", messages[-2]["content"])
        self.assertIn("turquoise", messages[-2]["content"])
        self.assertTrue(
            all("turquoise" not in m["content"] for m in messages if m["role"] == "system")
        )
        self.assertNotIn("passivement entendu", self.chat.history[0][0])
        self.assertEqual(self.s.context.count, 1)
        await self.gateway.command(
            self.s, {"type": "clear_context", "payload": {}}, time.monotonic_ns()
        )
        self.assertEqual(self.s.context.count, 0)
        self.assertEqual(len(self.requests), 1)

    async def test_passive_late_text_rejected_after_off_disconnect_or_generation_change(self):
        self.remote.bound = self.s
        for mode, alive, generation in (
            ("OFF", True, 0),
            ("PASSIVE", False, 0),
            ("PASSIVE", True, 1),
        ):
            with self.subTest(mode=mode, alive=alive, generation=generation):
                self.s.mode, self.s.alive, self.s.context_epoch = mode, alive, generation
                self.remote.listen_context_epoch = 0
                self.assertEqual(self.gateway.transcript_context("Texte tardif non adressé."), "")
                self.assertEqual(self.s.context.count, 0)
        self.s.alive, self.s.mode = True, "OFF"
        self.assertEqual(self.requests, [])
        self.assertEqual(self.tts.texts, [])

    async def test_producer_pause_never_flushes_pcm_as_a_catchup_burst(self):
        import numpy as np

        self.s.mode, self.s.up_stream = "ACTIVE", 17
        output = RemoteEchoEgress(self.s, self.settings, "paced-turn", 24000)
        await output.start()
        clock = SimpleNamespace(now=0.0)
        sent = []

        async def sleep(delay):
            clock.now += delay

        async def send(value):
            sent.append((clock.now, Packet.decode(value)))

        with (
            patch("jarvis_office.echo.audio.time.perf_counter", side_effect=lambda: clock.now),
            patch("jarvis_office.echo.audio.asyncio.sleep", side_effect=sleep),
            patch.object(self.endpoint, "send", side_effect=send),
        ):
            await output._output(np.zeros(4800, np.float32), clock.now)
            clock.now += 0.4  # Measured live gap at the next TTS segment's first PCM.
            resumed = clock.now
            await output._output(np.zeros(19200, np.float32), clock.now)
        self.assertEqual(sent[5][0], resumed)
        self.assertTrue(all(b[0] - a[0] >= 0.019 for a, b in zip(sent, sent[1:], strict=False)))
        self.assertEqual([p.sequence for _, p in sent], list(range(25)))
        self.assertEqual(sum(len(p.pcm) for _, p in sent), 48000)
        await output.abort()

    async def test_source_credit_bounds_pcm_while_sender_runs_independently(self):
        self.s.mode, self.s.up_stream = "ACTIVE", 17
        output = RemoteEchoEgress(self.s, self.settings, "credit-turn", 24000)
        await output.start()
        entered, release = asyncio.Event(), asyncio.Event()
        original_send = self.endpoint.send

        async def send(value):
            if isinstance(value, bytes) and not entered.is_set():
                entered.set()
                await release.wait()
            await original_send(value)

        with patch.object(self.endpoint, "send", side_effect=send):
            await asyncio.wait_for(output.feed("credit-turn", b"\0\0" * 12000), 0.1)
            await asyncio.wait_for(entered.wait(), 0.1)
            self.assertLess(output.progress("credit-turn")["free_source_bytes"], 19200)
            with self.assertRaisesRegex(LoopError, "remote_pcm_backpressure"):
                await output.feed("credit-turn", b"\0\0" * 9600)
            self.assertLessEqual(self.s.metrics["source_queue_peak_bytes"], 24000)
            release.set()
            await output.finish("credit-turn")
        self.assertEqual(sum(len(p.pcm) for p in self.endpoint.packets), 48000)
        self.assertEqual(output.progress("credit-turn")["free_source_bytes"], 24000)
        await output.drained("credit-turn")

    async def test_request_limit_is_explicit_and_never_above_ten(self):
        self.assertEqual(self.settings.api_request_limit, 8)
        replace(self.settings, api_request_limit=10).validate()
        with self.assertRaisesRegex(ValueError, "INVALID_ECHO_REQUEST_LIMIT"):
            replace(self.settings, api_request_limit=11).validate()

    async def test_off_reaches_client_before_worker_drain(self):
        self.s.mode = "ACTIVE"
        self.remote.bound = self.s

        async def pause(command):
            self.assertEqual(command, "pause")
            self.assertEqual(self.s.mode, "OFF")
            self.assertEqual(self.endpoint.control[-1]["type"], "stop_audio")

        with patch.object(self.voice, "control", side_effect=pause):
            await self.gateway.stop_audio(self.s)
        self.assertEqual(sum(m["type"] == "stop_audio" for m in self.endpoint.control), 1)

    async def test_stale_pcm_old_turn_and_disconnected_session_never_retarget(self):
        self.s.mode, self.s.up_stream = "ACTIVE", 17
        self.remote.bound = self.s
        output = RemoteEchoEgress(self.s, self.settings, "turn-one", 24000)
        await output.start()
        with self.assertRaisesRegex(LoopError, "stale_remote_turn"):
            await output.feed("turn-two", b"\0\0" * 480)
        self.s.mode = "OFF"
        with self.assertRaisesRegex(LoopError, "stale_remote_turn"):
            await output.feed("turn-one", b"\0\0" * 480)
        self.s.mode = "ACTIVE"
        self.s.alive = False
        new = Session("test", self.endpoint, PassiveContextBuffer(), audio=self.endpoint)
        self.remote.bound = new
        with self.assertRaisesRegex(LoopError, "stale_remote_turn"):
            await output.feed("turn-one", b"\0\0" * 480)
        self.assertEqual(self.endpoint.packets, [])
        self.assertEqual(new.mode, "OFF")
        self.s.alive = True
        self.remote.bound = self.s
        await output.abort()

    async def test_network_loss_closes_owned_sessions_without_fallback(self):
        gateway = EchoGateway(self.settings)
        gateway.sessions["test"] = self.s
        listener = Mock()
        with patch(
            "jarvis_office.echo.network.require_ethernet",
            side_effect=ValueError("NETWORK_PATH_UNAVAILABLE"),
        ):
            await gateway.watch_network(listener)
        self.assertTrue(gateway.network_lost.is_set())
        self.assertFalse(self.s.alive)
        self.assertEqual(self.s.mode, "OFF")
        self.assertEqual(self.s.last_error, "NETWORK_PATH_UNAVAILABLE")
        listener.close.assert_called_once()
        self.assertEqual(gateway.sessions, {})

    async def test_late_worker_event_cannot_rearm_an_old_turn(self):
        self.remote.bound = self.s
        self.voice.turn = "new-turn"
        for session, turn in (("old-session", "new-turn"), (self.voice.session, "old-turn")):
            self.remote.event(
                {
                    "event": "listening",
                    "session": session,
                    "turn": turn,
                    "data": {"device": {"ingress_token": "f" * 32}},
                }
            )
        self.assertEqual(self.remote.token, "")
        self.assertFalse(self.voice.armed)

    async def test_clear_context_discards_inflight_passive_transcription(self):
        await self.mode("PASSIVE")
        await self.wait_for(lambda: self.voice.state == "listening")
        self.s.context.add("Ancien contexte synthétique.")
        await self.gateway.command(
            self.s, {"type": "clear_context", "payload": {}}, time.monotonic_ns()
        )
        await self.say("Phrase commencée avant effacement.")
        await self.wait_for(lambda: self.gateway.non_addressed == 1)
        self.assertEqual(self.s.context.count, 0)
        self.assertEqual(self.requests, [])

    async def test_bounded_window_renews_same_workers_until_off(self):
        await self.mode("PASSIVE")
        await self.wait_for(lambda: self.voice.state == "listening")
        await self.remote.worker.results.put({"silence": True})
        await self.wait_for(lambda: self.voice.task.done())
        self.voice.deadline = time.perf_counter() - 1
        self.voice.remaining = 4
        self.s.welcomed = True
        with tempfile.TemporaryDirectory() as root:
            publishing = asyncio.create_task(self.gateway.publish(Path(root) / "report.json"))
            try:
                await self.wait_for(lambda: self.remote.worker.listens == 2)
                self.assertEqual(self.voice.remaining, 4)
                self.assertEqual(self.remote.worker.starts, 1)
                await self.mode("OFF")
                await asyncio.sleep(0.12)
                self.assertFalse(self.voice.armed)
                self.assertEqual(self.remote.worker.listens, 2)
                self.assertEqual(self.s.mode, "OFF")
            finally:
                publishing.cancel()
                await asyncio.gather(publishing, return_exceptions=True)


class BoundaryTests(unittest.TestCase):
    def test_remote_frames_use_existing_audioengine_vad_and_stt(self):
        read, write = os.pipe()
        source = RemotePipeIngress(read)
        listening = threading.Event()
        engine = AudioEngine.__new__(AudioEngine)
        engine.config = Config(
            speech=Speech(input_rate=16000, terminal_silence_ms=128, reconnect_attempts=0)
        )
        engine.ingress = source
        engine.sd = Mock()
        engine.lock = threading.Lock()
        engine.input = engine.playback = None
        engine.cancelled = threading.Event()
        engine.inference_busy = False
        engine.busy = True
        engine.turn = "owned-turn"
        engine.emit = lambda event, data: listening.set() if event == "listening" else None
        stt = Mock(return_value={"accepted": True, "text": "Jarvis, présent."})
        engine.recognizer = SimpleNamespace(
            vad=SimpleNamespace(
                reset=lambda: None, score=lambda samples: float(abs(samples).max() > 0.01)
            ),
            transcribe_utterance=stt,
        )
        results = []
        thread = threading.Thread(target=lambda: results.append(engine.listen("owned-turn", 2)))
        thread.start()
        try:
            self.assertTrue(listening.wait(1))
            token = source.current.token
            for i in range(35):
                pcm = (b"\x00\x10" if i < 15 else b"\0\0") * 320
                os.write(write, PIPE_HEADER.pack(token, time.perf_counter(), 640) + pcm)
                time.sleep(0.02)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(stt.call_count, 1)
            self.assertTrue(results[0]["accepted"])
            self.assertEqual(results[0]["timing"]["normalized_rate"], 16000)
            self.assertEqual(results[0]["timing"]["input_clock"], "server_receive_estimate")
            timing = results[0]["timing"]
            self.assertEqual(timing["segmentation_reason"], "terminal_silence")
            self.assertEqual(timing["input_samples"], timing["input_blocks"] * 320)
            self.assertEqual(timing["input_audio_s"], timing["input_samples"] / 16000)
            self.assertLessEqual(timing["utterance_samples"], timing["normalized_samples"])
            self.assertLessEqual(timing["normalized_samples"], timing["input_samples"])
            boundaries = [
                timing[key]
                for key in (
                    "capture_opened",
                    "first_block_consumed",
                    "last_block_consumed",
                    "vad_finalized",
                    "input_closed",
                )
            ]
            self.assertEqual(boundaries, sorted(boundaries))
            engine.sd.InputStream.assert_not_called()
            self.assertIsNone(engine.input)
        finally:
            engine.abort()
            os.close(write)
            thread.join(2)

    def test_binary_ingress_generation_age_queue_and_no_local_capture(self):
        read, write = os.pipe()
        source = RemotePipeIngress(read)
        try:
            channel, _, first = source.open(Speech(input_rate=16000))
            first.start()
            second_channel, _, second = source.open(Speech(input_rate=16000))
            second.start()
            payload = b"\x01\x00" * 320
            for token in (first.token, second.token):
                os.write(write, PIPE_HEADER.pack(token, time.perf_counter(), 640) + payload)
            block = second_channel.blocks.get(timeout=1)
            self.assertEqual(len(block.data), 320)
            self.assertTrue(channel.blocks.empty())
            with self.assertRaises(queue.Empty):
                second_channel.blocks.get(timeout=0.03)
            os.write(write, PIPE_HEADER.pack(second.token, time.perf_counter() - 1, 640) + payload)
            deadline = time.monotonic() + 1
            while not second_channel.lost and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(second_channel.lost)
            second.close()
        finally:
            os.close(write)

    def test_ethernet_missing_link_or_address_fails_closed(self):
        good = (
            "en0: flags=8863<UP,BROADCAST,RUNNING>\n inet 192.168.20.2 netmask x\n"
            " media: 100baseTX\n status: active\n"
        )
        with (
            patch("jarvis_office.echo.network.sys.platform", "darwin"),
            patch("jarvis_office.echo.network.socket.if_nametoindex", return_value=7),
            patch("jarvis_office.echo.network.subprocess.run") as run,
        ):
            run.return_value = Mock(stdout=good)
            self.assertEqual(require_ethernet("en0", "192.168.20.2"), 7)
            for value in (
                good.replace("active", "inactive"),
                good.replace(".20.2", ".20.3"),
                good.replace("100baseTX", "IEEE802.11"),
            ):
                run.return_value = Mock(stdout=value)
                with self.assertRaisesRegex(ValueError, "NETWORK_PATH_UNAVAILABLE"):
                    require_ethernet("en0", "192.168.20.2")

    def test_echo_budget_never_consumes_or_resets_phase06(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "echo.json"
            for expected in (1, 2):
                self.assertEqual(
                    reserve_validation_request(path=path, phase="ECHO", limit=2), expected
                )
            with self.assertRaises(ConfigError):
                reserve_validation_request(path=path, phase="ECHO", limit=2)
            self.assertEqual(json.loads(path.read_text())["attempts"], 2)
            self.assertEqual(list(Path(root).iterdir()), [path])
