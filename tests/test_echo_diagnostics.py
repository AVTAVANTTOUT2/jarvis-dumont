import unittest

from qualify_echo_device import buffer_observations, classify_playback, replay_ram, spectrum_metrics


class PlaybackClassificationTests(unittest.TestCase):
    def test_reserve_uses_written_minus_played_and_only_android_clock(self):
        trace = [
            {"phase": "U1", "prefill_frames": 4800},
            {
                "phase": "U2",
                "sequence": 5,
                "receive_ns": 1_000_000_000,
                "written_frames": 10000,
                "head_after": 6928,
                "underruns": 0,
                "server_send_ns": 9_000_000_000_000,
            },
            {
                "phase": "U2",
                "sequence": 6,
                "receive_ns": 1_205_000_000,
                "head_before": 10000,
                "underruns": 1,
                "server_send_ns": 1,
            },
        ]
        result = buffer_observations(trace)
        self.assertEqual(result["physical_prefill_ms"], 100)
        gap = result["gaps"][0]
        self.assertEqual(gap["receive_gap_ms"], 205)
        self.assertEqual(gap["preceding_write_reserve_ms"], 64)
        self.assertEqual(gap["remaining_before_next_write_ms"], 0)
        self.assertTrue(gap["underrun_observed"])
        self.assertEqual(result["gap_histogram"][-1]["associated_underruns"], 1)

    def test_gap_without_counter_increment_is_not_invented_underrun(self):
        result = buffer_observations(
            [
                {"phase": "U2", "sequence": 0, "receive_ns": 1, "underruns": 0},
                {"phase": "U2", "sequence": 1, "receive_ns": 180_000_001, "underruns": 0},
            ]
        )
        self.assertFalse(result["gaps"][0]["underrun_observed"])
        self.assertIsNone(result["gaps"][0]["preceding_write_reserve_ms"])
        self.assertIsNone(result["physical_prefill_ms"])

    def test_invalid_rate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "INVALID_SAMPLE_RATE"):
            buffer_observations([], 0)

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
    def test_energy_bands_distinguish_bass_from_voice_band_and_silence(self):
        import math
        import struct

        for frequency, band in ((100, "80_250_hz"), (1000, "250_4000_hz")):
            samples = [
                int(1000 * math.sin(2 * math.pi * frequency * n / 16000)) for n in range(16000)
            ]
            raw = bytearray(struct.pack("<16000h", *samples))
            result = spectrum_metrics(raw, 16000)
            self.assertGreater(result["bands"][band], 0.99)
            self.assertLess(abs(result["dc_pcm"]), 1)
            self.assertEqual(len(raw), 32000)
        self.assertTrue(
            all(v == 0 for v in spectrum_metrics(bytearray(640), 16000)["bands"].values())
        )

    async def test_level_comparison_halves_identical_pcm_and_wipes_the_copy(self):
        import struct

        raw = bytearray(struct.pack("<4h", 1200, -1200, 20000, -20000))
        captured = []

        async def inspect(pcm):
            self.assertEqual(struct.unpack("<4h", pcm), (600, -600, 10000, -10000))
            captured.append(pcm)

        size, gain = await replay_ram(raw, 48000, 48000, inspect, gain=0.5)
        self.assertEqual((size, gain), (8, 0.5))
        self.assertFalse(raw)
        self.assertFalse(captured[0])

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
