import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soxr
import test_diagnostics

from jarvis_office.assets import AssetError, import_stt, verify_bundle
from jarvis_office.audio_input import AudioError, Normalizer, Segmenter, Silero, read_wav
from jarvis_office.capture import (
    CaptureQueue,
    capture_wav,
    list_devices,
    microphone_preflight,
    resolve_input,
)
from jarvis_office.config import Config, ConfigError, Speech, load_config
from jarvis_office.stt import Recognizer, accept_text, load_corpus, summarize, word_errors, words


class SignalTests(unittest.TestCase):
    def test_continuous_resampling_rates_channels_lengths_and_antialias(self):
        for rate in (16000, 44100, 48000):
            for channels in (1, 2):
                with self.subTest(rate=rate, channels=channels):
                    t = np.arange(rate, dtype=np.float32) / rate
                    x = (np.sin(2 * np.pi * 1000 * t) * 0.25).astype(np.float32)
                    data = np.repeat(x[:, None], channels, axis=1)
                    converter = Normalizer(rate, channels)
                    chunks = [converter.feed(data[n : n + 137]) for n in range(0, len(data), 137)]
                    chunks.append(converter.feed(np.empty((0, channels), np.float32), last=True))
                    got = np.concatenate(chunks)
                    expected = soxr.resample(x, rate, 16000, quality="HQ")
                    self.assertEqual(len(got), 16000)
                    self.assertEqual(converter.input_samples, rate)
                    np.testing.assert_allclose(got, expected, atol=1e-6)
        t = np.arange(48000, dtype=np.float32) / 48000
        high = np.sin(2 * np.pi * 12000 * t).astype(np.float32)
        output = Normalizer(48000, 1).feed(high[:, None], last=True)
        self.assertLess(float(np.sqrt(np.mean(output[100:-100] ** 2))), 0.001)

    def test_frame_reassembly_preroll_internal_pause_and_real_end(self):
        frames = [0] * 20 + [1] * 10 + [0] * 8 + [1] * 8 + [0] * 16
        audio = np.repeat(np.array(frames, np.float32), 512)
        stream = Segmenter(Speech())
        calls = []

        def score(frame):
            self.assertEqual(len(frame), 512)
            calls.append(float(frame[0]))
            return float(frame[0])

        results = []
        for n in range(0, len(audio), 317):
            results += stream.feed(audio[n : n + 317], score)
        self.assertEqual(calls, frames)
        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.audio_start, 10 * 512)
        self.assertEqual(item.speech_start, 20 * 512)
        self.assertEqual(item.speech_end, 46 * 512)
        self.assertEqual(item.finalized, 62 * 512)
        self.assertEqual(item.timing()["endpoint_delay_s"], 0.512)
        self.assertEqual(item.timing()["hardware_latency"], "NOT_RUN")
        self.assertIsNone(item.finalized_wall)

    def test_eof_remainder_maximum_duration_and_explicit_session_reset(self):
        stream = Segmenter(Speech())
        self.assertEqual(stream.feed(np.ones(1537, np.float32), lambda x: 1), [])
        end = stream.finish(lambda x: 1)[0]
        self.assertEqual(len(end.audio), 1537)
        self.assertEqual(end.speech_end, 1537)
        self.assertEqual(end.reason, "end_of_file")
        self.assertEqual(end.timing()["endpoint_delay_s"], 0)
        stream.reset()
        self.assertEqual(stream.seen, 0)
        self.assertEqual(len(stream.pending), 0)
        maximum = Segmenter(dataclasses.replace(Speech(), max_utterance_s=1))
        outputs = maximum.feed(np.ones(32 * 512, np.float32), lambda x: 1)
        self.assertEqual(outputs[0].reason, "max_duration")
        self.assertLessEqual(len(outputs[0].audio), 16000 + 512)

    def test_silero_state_context_frame_contract_and_reset(self):
        vad = object.__new__(Silero)
        captured = []

        class Session:
            def run(self, names, inputs):
                captured.append(inputs)
                return np.array([[0.75]], np.float32), inputs["state"] + 1

        vad.session = Session()
        vad.reset()
        self.assertEqual(vad.score(np.ones(512, np.float32)), 0.75)
        self.assertEqual(captured[0]["input"].shape, (1, 576))
        self.assertEqual(captured[0]["state"].shape, (2, 1, 128))
        self.assertEqual(int(captured[0]["sr"]), 16000)
        np.testing.assert_equal(vad.context, 1)
        with self.assertRaises(AudioError):
            vad.score(np.zeros(480, np.float32))
        vad.reset()
        np.testing.assert_equal(vad.context, 0)
        np.testing.assert_equal(vad.state, 0)


class FileAndImportTests(unittest.TestCase):
    def setUp(self):
        fixture = test_diagnostics.DiagnosticsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.config = fixture.make_assets()

    def test_stt_candidates_copy_without_shared_links_and_corruption_fails(self):
        notice = self.root / "LICENSE"
        notice.write_text("test notice")
        destination = self.root / "candidates"
        args = (self.config.assets.stt_model, self.config.assets.vad_model, notice, destination)
        result = import_stt(*args, dry_run=True)
        self.assertFalse(destination.exists())
        result = import_stt(*args)
        bundle = destination / result["bundle_id"]
        self.assertEqual(verify_bundle(bundle)["kind"], "stt")
        copied = bundle / "model/model.bin"
        self.assertFalse(os.path.samefile(copied, self.config.assets.stt_model / "model.bin"))
        self.assertTrue(import_stt(*args)["reused"])
        copied.write_bytes(b"broken")
        with self.assertRaises(AssetError):
            verify_bundle(bundle)

    def test_wav_spaces_corruption_too_long_truncated_and_unsupported(self):
        output = self.root / "test with spaces.wav"
        with wave.open(str(output), "wb") as wav:
            wav.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(44100 * 2 * 2))
        self.assertEqual(len(read_wav(output)), 16000)
        with self.assertRaises(AudioError):
            read_wav(output, 0)
        output.write_bytes(output.read_bytes()[:-4])
        with self.assertRaises(AudioError):
            read_wav(output)
        output.write_bytes(b"corrupt")
        with self.assertRaises(AudioError):
            read_wav(output)

    def test_speech_configuration_is_typed_and_no_gpu_analogy(self):
        config = self.root / "speech.toml"
        for value in (
            'compute_type="mps"',
            "input_rate=30000",
            "pre_roll_ms=true",
            "vad_threshold=nan",
            'python="https://private.invalid/token"',
            'input_device="mic\\u0001"',
        ):
            config.write_text("[speech]\n" + value)
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_config(config)
        config.write_text('[speech]\ninput_device="Blue Snowball"')
        self.assertEqual(load_config(config).speech.input_device, "Blue Snowball")


class CaptureTests(unittest.TestCase):
    def test_device_lists_are_passive_format_queries_not_stream_availability(self):
        class PortAudioError(Exception):
            pass

        calls = []

        def check(**args):
            calls.append(args)
            if args["samplerate"] == 44100:
                raise PortAudioError("private driver error must not escape")

        sd = types.SimpleNamespace(
            PortAudioError=PortAudioError,
            query_devices=lambda: [
                {
                    "name": "local device",
                    "max_input_channels": 1,
                    "max_output_channels": 2,
                    "default_samplerate": 48000,
                },
                {
                    "name": "unavailable",
                    "max_input_channels": 0,
                    "max_output_channels": 0,
                    "default_samplerate": 48000,
                },
            ],
            check_input_settings=check,
            check_output_settings=check,
        )
        from jarvis_office.speech_cli import run

        for direction, channels in (("input", 1), ("output", 2)):
            with patch.dict(sys.modules, sounddevice=sd):
                result = run(Config(), {"command": direction + "-list"})
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(len(result["devices"]), 1)
            device = result["devices"][0]
            self.assertEqual(device["max_channels"], channels)
            self.assertEqual(device["stream"], "NOT_RUN")
            self.assertEqual({f["rate"] for f in device["formats"]}, {16000, 24000, 48000})
            self.assertNotIn("index", device)
        self.assertTrue(calls)
        with self.assertRaises(AudioError):
            list_devices(sd, "invalid")

    def test_device_list_fresh_import_never_loads_engines_or_network(self):
        script = """
import sys, types
from jarvis_office.speech_cli import run, deny_network
from jarvis_office.config import Config
deny_network()
sys.modules['sounddevice'] = types.SimpleNamespace(query_devices=lambda: [])
for command in ('input-list', 'output-list'):
    assert run(Config(), {'command': command})['devices'] == []
assert not {'faster_whisper', 'ctranslate2', 'onnxruntime', 'mlx', 'httpx',
            'jarvis_office.stt', 'jarvis_office.tts_worker'} & sys.modules.keys()
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="speech capture ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_queue_overflow_and_status_invalidate_discontinuous_audio(self):
        channel = CaptureQueue(2)
        for n in range(3):
            channel.callback(
                np.ones((320, 1), np.float32),
                320,
                types.SimpleNamespace(inputBufferAdcTime=n * 0.02),
                False,
            )
        self.assertTrue(channel.lost)
        self.assertEqual(channel.dropped, 1)
        with self.assertRaisesRegex(AudioError, "discontinuity"):
            channel.get()
        channel = CaptureQueue(2)
        channel.callback(
            np.zeros((320, 1), np.float32), 320, types.SimpleNamespace(inputBufferAdcTime=0), True
        )
        with self.assertRaises(AudioError):
            channel.get()

    def fake_sd(self, *, ambiguous=False, disconnect_first=False):
        class PortAudioError(Exception):
            pass

        module = types.SimpleNamespace(PortAudioError=PortAudioError)
        module.query_devices = lambda: (
            [{"name": "selected mic", "max_input_channels": 1, "hostapi": 0}]
            * (2 if ambiguous else 1)
        )
        module.query_hostapis = lambda n: {"name": "test host"}
        module.check_input_settings = lambda **kwargs: None
        starts = []

        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def __enter__(self):
                starts.append(1)
                for n in range(13):
                    self.callback(
                        np.zeros((320, 1), np.float32),
                        320,
                        types.SimpleNamespace(inputBufferAdcTime=n * 0.02),
                        disconnect_first and len(starts) == 1,
                    )
                return self

            def __exit__(self, *args):
                pass

        module.InputStream = Stream
        return module, starts

    def test_exact_device_selection_missing_ambiguous_and_changed_index(self):
        module, _ = self.fake_sd()
        self.assertEqual(resolve_input(module, "selected mic", 48000)[0], 0)
        module.query_devices = lambda: [
            {"name": "other", "max_input_channels": 0},
            {"name": "selected mic", "max_input_channels": 1, "hostapi": 0},
        ]
        self.assertEqual(resolve_input(module, "selected mic", 48000)[0], 1)
        with self.assertRaises(AudioError):
            resolve_input(module, "absent", 48000)
        with self.assertRaises(AudioError):
            resolve_input(self.fake_sd(ambiguous=True)[0], "selected mic", 48000)

    def test_permission_and_active_input_fail_closed_without_capture(self):
        for native, reason in (
            (
                {"authorization": 2, "query_errors": 0, "active_inputs": 0},
                "microphone_permission_required",
            ),
            (
                {"authorization": 3, "query_errors": 0, "active_inputs": 1},
                "another_audio_input_active",
            ),
        ):
            result = subprocess.CompletedProcess([], 0, json.dumps(native).encode(), b"")
            with (
                patch("jarvis_office.capture.sys.platform", "darwin"),
                patch("jarvis_office.capture.private_root", return_value=self.root),
                patch("jarvis_office.capture.subprocess.run", return_value=result),
            ):
                check = microphone_preflight()
                self.assertEqual(check["status"], "BLOCKED_USER")
                self.assertEqual(check["reason"], reason)

    def test_reconnection_resets_session_and_does_not_join_failed_capture(self):
        module, starts = self.fake_sd(disconnect_first=True)
        vad = types.SimpleNamespace(reset=lambda: None, score=lambda frame: 0)
        config = dataclasses.replace(Speech(), input_device="selected mic", input_rate=16000)
        with (
            patch.dict(sys.modules, sounddevice=module),
            patch("jarvis_office.capture.microphone_preflight", return_value={"status": "PASS"}),
        ):
            result = capture_wav(config, vad, self.root / "capture.wav", 0.25)
        self.assertEqual(len(starts), 2)
        self.assertEqual(result["reconnects"], 1)
        self.assertEqual(result["input_samples"], 4000)
        self.assertEqual(result["duration_s"], 0.25)
        self.assertIsNotNone(result["first_block_received_wall_s"])
        self.assertIn("distinct from PortAudio", result["wall_clock"])
        self.assertIn("SILENCE_NO_SPEECH", result["transitions"])
        self.assertEqual(len(read_wav(self.root / "capture.wav")), 4000)


class QualityTests(unittest.TestCase):
    def test_critical_changes_and_no_acceptable_stt_are_explicit(self):
        from jarvis_office.stt import critical_errors, select_candidate

        self.assertEqual(
            critical_errors("Élodie ne veut pas 12", "Élodie ne veut pas 12", ["Élodie"]), []
        )
        errors = critical_errors("Élodie ne veut pas 12", "Émilie veut 13", ["Élodie"])
        self.assertEqual(len(errors), 3)
        group = {
            "wer": 0.01,
            "failures": 0,
            "critical_errors": 0,
            "invented_outputs": 0,
            "lost_phrases": 0,
        }
        candidates = {
            name: {"summary": {"quiet/holdout": dict(group)}} for name in ("small", "turbo")
        }
        self.assertEqual(select_candidate(candidates)["model"], "small")
        candidates["small"]["summary"]["quiet/holdout"]["wer"] = 0.09
        self.assertEqual(select_candidate(candidates)["model"], "turbo")
        candidates["turbo"]["summary"]["quiet/holdout"]["critical_errors"] = 1
        self.assertEqual(select_candidate(candidates)["status"], "NO_ACCEPTABLE_STT")

    def test_adapter_prompt_budget_no_second_vad_no_temperature_cascade_and_lock(self):
        engine = object.__new__(Recognizer)
        engine.settings = Speech()
        engine.vad = types.SimpleNamespace(reset=lambda: None, score=lambda frame: 1)
        engine.lock = threading.Lock()
        calls = []
        sentence = "Merci pour ta réponse, explique-moi la suite"

        def transcribe(audio, **kwargs):
            calls.append(kwargs)

            def segments():
                yield types.SimpleNamespace(
                    text=sentence, start=0, end=0.3, avg_logprob=-0.8, no_speech_prob=0.1
                )

            return segments(), None

        engine.model = types.SimpleNamespace(transcribe=transcribe)
        result = engine.transcribe(np.ones(4800, np.float32))
        self.assertEqual(result["text"], sentence)
        self.assertTrue(result["accepted"])
        self.assertEqual(calls[0]["temperature"], 0.0)
        self.assertIs(calls[0]["vad_filter"], False)
        self.assertEqual(calls[0]["language"], "fr")
        self.assertLessEqual(calls[0]["max_new_tokens"] + 4, 448)
        engine.lock.acquire()
        try:
            with self.assertRaisesRegex(AudioError, "one_stt"):
                engine.transcribe(np.ones(1600, np.float32))
        finally:
            engine.lock.release()
        engine.settings = dataclasses.replace(Speech(), max_utterance_s=1)
        result = engine.transcribe(np.ones(20000, np.float32))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "utterance_truncated")

    def test_speech_network_guard_denies_socket_and_no_engine_import_on_launch_module(self):
        code = (
            "from jarvis_office.speech_cli import deny_network\n"
            "import socket,sys\nassert 'faster_whisper' not in sys.modules\n"
            "deny_network()\ntry: socket.create_connection(('127.0.0.1',9))\n"
            "except PermissionError: pass\nelse: raise AssertionError('network allowed')"
        )
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_human_corpus_manifest_is_not_fake_ground_truth(self):
        from jarvis_office.corpus import prepare_human_corpus

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "corpus/human/manifest.json"
            with patch("jarvis_office.corpus.private_root", return_value=root):
                result = prepare_human_corpus(output)
            rows = json.loads(output.read_text())["items"]
            self.assertEqual(result["planned_recordings"], 50)
            self.assertEqual(sum(r["split"] == "holdout" for r in rows), 10)
            self.assertTrue(
                all(r["reference"] == "" and r["human_verified"] is False for r in rows)
            )
            self.assertEqual(list(output.parent.glob("*.wav")), [])

    def test_ordinary_french_not_blacklisted_and_negation_names_numbers_count(self):
        for text in (
            "Merci pour ta réponse, explique-moi la suite",
            "L’une de mes questions concerne les camions",
            "Parlons de musique",
            "Ne ferme pas la porte",
        ):
            self.assertTrue(accept_text(text)[0])
        self.assertFalse(accept_text(" ")[0])
        self.assertEqual(words("L’une, Élodie ! 12"), ["l", "une", "élodie", "12"])
        self.assertEqual(word_errors("ne ferme pas", "ferme")["errors"], 2)
        self.assertEqual(word_errors("Élodie 12", "Émilie 13")["errors"], 2)

    def test_metric_groups_count_failures_and_nearest_rank_percentile(self):
        rows = [
            {
                "condition": "quiet",
                "split": "holdout",
                "status": "PASS",
                "inference_s": x,
                "reference_words": 10,
                "errors": 0,
                "critical_errors": [],
                "lost_phrase": False,
            }
            for x in (1, 2, 3)
        ]
        rows.append(
            {
                "condition": "quiet",
                "split": "holdout",
                "status": "FAIL",
                "reference_words": 10,
                "errors": 10,
                "lost_phrase": True,
            }
        )
        result = summarize(rows)["quiet/holdout"]
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["wer"], 0.25)
        self.assertEqual(result["inference_p50_s"], 2)
        self.assertEqual(result["inference_p95_s"], 3)

    def test_invalid_corpus_is_an_error_not_a_reference_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for value in (
                [],
                {"schema_version": 1, "items": []},
                {"schema_version": 1, "items": [{"id": "x", "reference": None}]},
            ):
                path.write_text(json.dumps(value))
                with self.assertRaises(AudioError):
                    load_corpus(path)


if __name__ == "__main__":
    unittest.main()
