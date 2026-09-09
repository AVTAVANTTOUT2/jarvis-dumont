import unittest

from qualify_echo_device import classify_playback, replay_ram


class PlaybackClassificationTests(unittest.TestCase):
    def test_drain_and_teardown_are_separate_from_data(self):
        trace = [
            {"phase": "U0", "underruns": 0},
            {"phase": "U1", "underruns": 0},
            {"phase": "U2", "sequence": 0, "underruns_before": 0, "underruns": 0, "queue_depth": 1},
            {"phase": "END_DECLARED", "last_sequence": 0},
            {"phase": "U5", "underruns": 1},
            {"phase": "U6_AFTER_PAUSE_FLUSH", "underruns": 2},
        ]
        result = classify_playback(trace)
        self.assertTrue(result["complete"])
        self.assertEqual(
            (result["during_data"], result["after_last_write"], result["teardown"]), (0, 1, 1)
        )
        trace[2]["underruns"] = 1
        result = classify_playback(trace)
        self.assertEqual((result["during_data"], result["after_last_write"]), (1, 0))
        self.assertFalse(classify_playback(trace[:-1])["complete"])
        trace[2]["sequence"] = 1
        trace[3]["last_sequence"] = 1
        self.assertFalse(classify_playback(trace)["complete"])


class RamReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_replay_gain_is_peak_bounded(self):
        import struct

        raw = bytearray(struct.pack("<320h", *([4000] * 320)))
        peaks = []

        async def inspect(pcm):
            peaks.append(max(abs(v) for v in struct.unpack(f"<{len(pcm) // 2}h", pcm)))

        size, gain = await replay_ram(raw, 16000, 16000, inspect, gain=8)
        self.assertEqual(size, 640)
        self.assertAlmostEqual(gain, 2.048, places=2)  # Resampling can dither by one PCM unit.
        self.assertGreaterEqual(peaks[0], 8191)
        self.assertLessEqual(peaks[0], 8192)
        self.assertFalse(raw)

    async def test_replay_resamples_and_destroys_both_buffers_on_failure(self):
        raw = bytearray(b"\0\1" * 320)
        held = []

        async def fail(pcm):
            self.assertFalse(raw)
            self.assertEqual(len(pcm), 1920)
            held.append(pcm)
            raise RuntimeError("synthetic failure")

        with self.assertRaises(RuntimeError):
            await replay_ram(raw, 16000, 48000, fail)
        self.assertFalse(raw)
        self.assertFalse(held[0])
