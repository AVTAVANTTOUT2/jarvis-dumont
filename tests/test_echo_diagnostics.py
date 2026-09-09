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
