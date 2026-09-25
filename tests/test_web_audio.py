"""Phone PCM sink: no PortAudio, bounded RAM, estimated DAC."""

import os
import unittest

from jarvis_office.audio_client import LoopError
from jarvis_office.audio_ingress import REMOTE_RATE, RemotePipeIngress
from jarvis_office.audio_input import AudioError
from jarvis_office.audio_worker import bind_engine_config
from jarvis_office.config import Config, Speech
from jarvis_office.web_audio import FRAME_BYTES, PHONE_DEVICE, WebPhoneAudio, WebSink


class PhoneCaptureTests(unittest.TestCase):
    def setUp(self):
        self.frames = []

        class Worker:
            event = staticmethod(lambda event: None)

            def feed_remote(worker, token, pcm, received):
                self.frames.append((token, pcm))

        self.phone = WebPhoneAudio(Worker())
        self.arm("a" * 32)

    def arm(self, token):
        self.phone._on_event({"event": "listening", "data": {"device": {"ingress_token": token}}})

    def test_batches_preserve_original_frame_order(self):
        first, second = b"\x01\x00" * 320, b"\x02\x00" * 320
        self.phone.feed_uplink(first + second, capture="a" * 32, sequence=0)
        self.phone.feed_uplink(first, capture="a" * 32, sequence=2)
        self.assertEqual(self.frames, [("a" * 32, pcm) for pcm in (first, second, first)])

    def test_old_capture_cannot_enter_new_listen_window(self):
        self.arm("b" * 32)
        with self.assertRaisesRegex(LoopError, "stale_phone_capture"):
            self.phone.feed_uplink(b"\x00" * FRAME_BYTES, capture="a" * 32, sequence=0)
        self.assertEqual(self.frames, [])
        self.phone.feed_uplink(b"\x01" * FRAME_BYTES, capture="b" * 32, sequence=0)
        self.assertEqual(self.frames, [("b" * 32, b"\x01" * FRAME_BYTES)])

    def test_missing_or_duplicate_frame_invalidates_whole_take(self):
        for wrong in (0, 2):
            with self.subTest(sequence=wrong):
                self.arm("a" * 32)
                self.frames.clear()
                self.phone.feed_uplink(b"\x01" * FRAME_BYTES, capture="a" * 32, sequence=0)
                with self.assertRaisesRegex(LoopError, "phone_capture_discontinuity"):
                    self.phone.feed_uplink(b"\x02" * FRAME_BYTES, capture="a" * 32, sequence=wrong)
                self.assertEqual(self.phone.token, "")
                self.assertEqual(len(self.frames), 1)

    def test_transcribing_closes_uplink(self):
        self.phone._on_event({"event": "transcribing", "data": {}})
        with self.assertRaisesRegex(LoopError, "stale_phone_capture"):
            self.phone.feed_uplink(b"\x00" * FRAME_BYTES, capture="a" * 32, sequence=0)
        self.assertEqual(self.frames, [])


class WebSinkTests(unittest.TestCase):
    def test_begin_rejects_mac_style_rates_outside_bounds(self):
        sink = WebSink()
        with self.assertRaises(LoopError):
            sink.begin("t1", 0)
        device = sink.begin("t1", 24000)
        self.assertEqual(device["device"]["name"], PHONE_DEVICE["name"])
        self.assertEqual(device["device"]["sample_rate"], 24000)

    def test_pull_then_estimated_dac_marks_done_without_mac_playback(self):
        sink = WebSink()
        sink.begin("t1", 16000)
        pcm = b"\x00\x00" * 160  # 10 ms
        sink.feed("t1", pcm)
        sink.mark("t1", 1)
        pulled = sink.pull()
        self.assertEqual(pulled["rate"], 16000)
        self.assertTrue(pulled["pcm"])
        sink.finish("t1")
        first = sink.first_pull
        assert first is not None
        sink.first_pull = first - 1.0
        progress = sink.progress("t1")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["completed_segments"], [1])
        self.assertNotIn("PortAudio", str(progress))

    def test_receiving_the_pcm_does_not_finish_playback_early(self):
        sink = WebSink()
        sink.begin("t1", 16000)
        chunk = b"\x00\x00" * 8000
        sink.feed("t1", chunk)
        sink.feed("t1", chunk)
        sink.mark("t1", 1)
        sink.pull()
        sink.pull()
        sink.finish("t1")
        sink.hear(sink.produced, True)
        self.assertFalse(sink.progress("t1")["done"])
        sink.first_pull -= 1.05
        self.assertTrue(sink.progress("t1")["done"])

    def test_backpressure_and_wrong_ack_fail_closed(self):
        sink = WebSink()
        sink.begin("t1", 24000)
        sink.feed("t1", b"\x00" * 24000)
        sink.feed("t1", b"\x00" * 24000)
        self.assertEqual(len(sink.pending), 48000)
        with self.assertRaises(LoopError):
            sink.feed("t1", b"\x00\x00")
        with self.assertRaises(LoopError):
            sink.hear(48001, False)
        sink.hear(200, False)
        self.assertEqual(sink.acked, 200)
        sink.finish("t1")
        sink.hear(200, True)
        self.assertEqual(sink.acked, sink.produced)

    def test_frame_size_matches_echo_remote_ingress(self):
        self.assertEqual(FRAME_BYTES, 640)


class RemoteWorkerRateTests(unittest.TestCase):
    def test_bind_engine_config_opens_pipe_when_toml_is_48k(self):
        read, write = os.pipe()
        try:
            ingress = RemotePipeIngress(read)
            disk = Config(speech=Speech(input_rate=48000))
            with self.assertRaises(AudioError) as ctx:
                ingress.open(disk.speech)
            self.assertEqual(str(ctx.exception), "remote_ingress_unavailable")
            self.assertEqual(bind_engine_config(disk, object()).speech.input_rate, 48000)
            bound = bind_engine_config(disk, ingress)
            self.assertEqual(bound.speech.input_rate, REMOTE_RATE)
            _, device, stream = ingress.open(bound.speech)
            self.assertEqual(device["sample_rate"], REMOTE_RATE)
            self.assertTrue(device["ingress_token"])
            stream.close()
        finally:
            os.close(write)


if __name__ == "__main__":
    unittest.main()
