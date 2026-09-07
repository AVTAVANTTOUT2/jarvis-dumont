import contextlib
import importlib.abc
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from jarvis_office.cli import main
from jarvis_office.config import Assets, Config, ConfigError, load_config
from jarvis_office.diagnostics import asset_checks, inspect_tts, report


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jarvis office ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "private config.toml"

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_absent_and_invalid_configurations_are_redacted(self):
        with patch("pathlib.Path.home", return_value=self.root):
            code, data, _ = self.invoke("doctor", "--json")
        self.assertEqual(code, 3 if sys.platform == "darwin" else 1)
        self.assertEqual(data["checks"][0]["status"], "NOT_RUN")
        for content in (
            b"[invalid sk-private-secret",
            b'password = "sk-private-secret"',
            b"[assets]\ntts_model = 42",
            b'[assets]\ntts_model = "https://example.invalid/private"',
            b'[assets]\nunknown = "sk-private-secret"',
            b"assets = []",
            b"\xff",
            b"#" * 65_537,
        ):
            with self.subTest(content=content[:20]):
                self.config.write_bytes(content)
                code, data, raw = self.invoke("doctor", "--config", str(self.config), "--json")
                self.assertEqual(code, 2)
                self.assertEqual(data["status"], "FAIL")
                self.assertNotIn("sk-private-secret", raw)
                self.assertNotIn(str(self.root), raw)
        code, _, raw = self.invoke("doctor", "--unknown-sk-private-secret", "--json")
        self.assertEqual(code, 2)
        self.assertNotIn("sk-private-secret", raw)

    def test_missing_explicit_file_directory_fifo_and_broken_config(self):
        code, _, _ = self.invoke("doctor", "--config", str(self.config), "--json")
        self.assertEqual(code, 2)
        for target in (self.root, self.config):
            if target == self.config:
                self.config.symlink_to(self.root / "absent")
            code, _, _ = self.invoke("doctor", "--config", str(target), "--json")
            self.assertEqual(code, 2)
        if hasattr(os, "mkfifo"):
            fifo = self.root / "config fifo"
            os.mkfifo(fifo)
            code, _, _ = self.invoke("doctor", "--config", str(fifo), "--json")
            self.assertEqual(code, 2)

    def test_relative_paths_and_empty_settings(self):
        self.config.write_text('[assets]\ntts_model = "model with spaces"\nstt_model = ""')
        config = load_config(self.config)
        self.assertEqual(config.assets.tts_model, self.root / "model with spaces")
        self.assertIsNone(config.assets.stt_model)
        self.assertEqual(config.assets.voice_profile, None)

    def make_assets(self):
        tts, stt, voice = [self.root / name for name in ("tts model", "stt model", "voice private")]
        for folder in (tts, stt, voice, tts / "speech_tokenizer"):
            folder.mkdir()
        for name in (
            "config.json",
            "generation_config.json",
            "tokenizer_config.json",
            "vocab.json",
            "preprocessor_config.json",
            "speech_tokenizer/config.json",
        ):
            (tts / name).write_text('{"fixture": true}')
        (tts / "merges.txt").write_text("fixture")
        (tts / "model.safetensors.index.json").write_text(
            '{"weight_map": {"fixture": "model.safetensors"}}'
        )
        # Tiny structural fixture, deliberately not an inference-capable model.
        header = json.dumps({"fixture": {"dtype": "U8", "shape": [4], "data_offsets": [0, 4]}})
        tensor = struct.pack("<Q", len(header)) + header.encode() + b"data"
        for file in (tts / "model.safetensors", tts / "speech_tokenizer/model.safetensors"):
            file.write_bytes(tensor)
        for name in ("config.json", "tokenizer.json"):
            (stt / name).write_text('{"fixture": true}')
        (stt / "model.bin").write_bytes(b"fixture structure" * 2)
        (stt / "vocabulary.txt").write_text("fixture")
        with wave.open(str(voice / "reference.wav"), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x00" * 128)
        (voice / "transcript.txt").write_text("private phrase never printed", encoding="utf-8")
        (voice / "metadata.json").write_text('{"private": "never printed"}')
        vad = self.root / "silero.onnx"
        vad.write_bytes(b"fixture structure" * 2)
        return Config(Assets(tts, stt, voice, vad), source_present=True)

    def test_structural_success_and_sensitive_paths_never_leave_cli(self):
        config = self.make_assets()
        self.assertTrue(all(c.status == "PASS" for c in asset_checks(config)[:4]))
        with patch("jarvis_office.cli.load_config", return_value=config):
            code, data, raw = self.invoke("assets", "inspect", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(data["schema_version"], 1)
        for secret in (str(self.root), "private phrase", "never printed"):
            self.assertNotIn(secret, raw)
        self.assertEqual(data["checks"][-1]["status"], "NOT_RUN")

    def test_incomplete_corrupt_and_broken_assets_fail(self):
        config = self.make_assets()
        tts = config.assets.tts_model
        for name, content in (
            ("download.incomplete", b""),
            ("model.safetensors", b"version https://git-lfs.github.com/spec/v1\n"),
            ("model.safetensors", b"tiny"),
            ("config.json", b"[]"),
            ("config.json", b"{"),
            ("model.safetensors.index.json", b'{"weight_map":{"x":"../private.safetensors"}}'),
        ):
            with self.subTest(name=name, content=content):
                file = tts / name
                original = file.read_bytes() if file.exists() else None
                file.write_bytes(content)
                self.assertEqual(asset_checks(config)[0].status, "FAIL")
                if original is None:
                    file.unlink()
                else:
                    file.write_bytes(original)
        broken = tts / "broken"
        broken.symlink_to(tts / "absent")
        self.assertEqual(asset_checks(config)[0].reason, "asset_invalid_or_incomplete")
        broken.unlink()
        linked = self.root / "linked model"
        linked.symlink_to(tts, target_is_directory=True)
        inspect_tts(linked)
        (tts / "loop").symlink_to(tts, target_is_directory=True)
        self.assertEqual(asset_checks(config)[0].status, "FAIL")

    def test_truncated_wav_and_blank_transcript(self):
        config = self.make_assets()
        voice = config.assets.voice_profile
        wav = voice / "reference.wav"
        wav.write_bytes(wav.read_bytes()[:-10])
        self.assertEqual(asset_checks(config)[2].status, "FAIL")
        (voice / "transcript.txt").write_text("  ")
        self.assertEqual(asset_checks(config)[2].status, "FAIL")

    def test_permissions_are_blocked_without_exception_text(self):
        with patch(
            "jarvis_office.cli.load_config",
            side_effect=ConfigError("configuration_permission_denied", blocked=True),
        ):
            code, data, _ = self.invoke("doctor", "--json")
        self.assertEqual((code, data["status"]), (3, "BLOCKED_USER"))
        with patch(
            "jarvis_office.diagnostics.inspect_tts",
            side_effect=PermissionError("sk-private-secret"),
        ):
            checks = asset_checks(Config(Assets(tts_model=self.root)))
        self.assertEqual(checks[0].status, "BLOCKED_USER")
        self.assertNotIn("sk-private-secret", json.dumps(report("assets inspect", checks)))

    def test_no_network_subprocess_audio_or_engine_imports(self):
        config = self.make_assets()

        class BlockEngines(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in {
                    "mlx",
                    "mlx_audio",
                    "torch",
                    "faster_whisper",
                    "silero_vad",
                    "sounddevice",
                    "pyaudio",
                    "huggingface_hub",
                    "jarvis",
                    "native_audio",
                }:
                    raise AssertionError("engine import attempted")

        guard = BlockEngines()
        sys.meta_path.insert(0, guard)
        self.addCleanup(sys.meta_path.remove, guard)
        with (
            patch.object(socket, "socket", side_effect=AssertionError("network attempted")),
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS attempted")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess attempted")),
            patch("jarvis_office.cli.load_config", return_value=config),
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-private-secret"}),
        ):
            for args in (("doctor", "--json"), ("assets", "inspect", "--json")):
                _, _, raw = self.invoke(*args)
                self.assertNotIn("sk-private-secret", raw)

    def test_package_import_is_inert_in_fresh_process(self):
        script = """
import sys
def guard(event, args):
    if event.startswith(('socket.', 'subprocess.', 'ctypes.dlopen')):
        raise AssertionError(event)
sys.addaudithook(guard)
before = set(sys.modules)
import jarvis_office
assert set(sys.modules) - before == {'jarvis_office'}
"""
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()
