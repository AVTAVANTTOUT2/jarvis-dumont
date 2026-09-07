import asyncio
import contextlib
import dataclasses
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_diagnostics

from jarvis_office.assets import AssetError, import_bundle, validate_profile, verify_bundle
from jarvis_office.config import TTS, ConfigError, load_config
from jarvis_office.tts import MAX_FRAME, TTSClient, TTSError, write_frame
from jarvis_office.tts_worker import serve


class ImportTests(unittest.TestCase):
    def setUp(self):
        fixture = test_diagnostics.DiagnosticsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.config = fixture.make_assets()
        (self.config.assets.voice_profile / "metadata.json").write_text(
            json.dumps(
                {
                    "reference_audio": "reference.wav",
                    "reference_text": "transcript.txt",
                    "language": "fr-FR",
                }
            )
        )
        self.destination = self.root / "private assets"

    def test_dry_run_independent_symlink_copy_idempotence_and_corruption(self):
        model = self.config.assets.tts_model
        original = self.root / "external weight"
        shutil.move(model / "model.safetensors", original)
        (model / "model.safetensors").symlink_to(original)
        (self.config.assets.voice_profile / "reference.npy").write_bytes(b"untrusted cache")
        result = import_bundle(self.config, self.destination, dry_run=True)
        self.assertFalse(self.destination.exists())
        result = import_bundle(self.config, self.destination)
        bundle = self.destination / result["bundle_id"]
        verify_bundle(bundle)
        copied = bundle / "model/model.safetensors"
        self.assertFalse(copied.is_symlink())
        self.assertFalse(os.path.samefile(copied, original))
        self.assertEqual(copied.stat().st_nlink, 1)
        self.assertFalse((bundle / "voice/reference.npy").exists())
        self.assertTrue(import_bundle(self.config, self.destination)["reused"])
        self.assertEqual(len(list(self.destination.iterdir())), 1)
        copied.write_bytes(b"corrupt")
        with self.assertRaises(AssetError):
            import_bundle(self.config, self.destination)
        self.assertNotEqual(original.read_bytes(), b"corrupt")

    def test_changed_source_never_published_even_with_identical_contents(self):
        copy = shutil.copyfile
        touched = False

        def unstable(src, dst, **kwargs):
            nonlocal touched
            result = copy(src, dst, **kwargs)
            if not touched:
                touched = True
                content = Path(src).read_bytes()
                Path(src).write_bytes(content)
            return result

        with patch("jarvis_office.assets.shutil.copyfile", side_effect=unstable):
            with self.assertRaisesRegex(AssetError, "source_changed"):
                import_bundle(self.config, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_profile_missing_transcript_metadata_mismatch_and_disk_shortage(self):
        voice = self.config.assets.voice_profile
        details = validate_profile(voice)
        self.assertEqual(details["sample_rate"], 16000)
        with patch(
            "jarvis_office.assets.shutil.disk_usage",
            return_value=shutil._ntuple_diskusage(100, 100, 0),
        ):
            with self.assertRaisesRegex(AssetError, "insufficient_disk"):
                import_bundle(self.config, self.destination)
        (voice / "metadata.json").write_text('{"reference_audio":"wrong.wav"}')
        with self.assertRaisesRegex(AssetError, "voice_metadata"):
            import_bundle(self.config, self.destination)
        (voice / "transcript.txt").unlink()
        with self.assertRaises(FileNotFoundError):
            import_bundle(self.config, self.destination)

    def test_tts_configuration_rejects_invalid_language_and_sampling(self):
        file = self.root / "config.toml"
        for body in (
            'language="auto"',
            'clone_mode="speaker_embedding"',
            "top_k=true",
            "temperature=nan",
            "top_p=2",
            "python=4",
        ):
            file.write_text("[tts]\n" + body)
            with self.subTest(body=body), self.assertRaises(ConfigError):
                load_config(file)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="office protocol ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    def client(self, mode="normal", **timeouts):
        settings = dataclasses.replace(
            TTS(), **dict(startup_timeout=2, fragment_timeout=1, drain_timeout=0.5) | timeouts
        )
        client = TTSClient(
            [sys.executable, "-I", "-B", str(Path(__file__).with_name("fake_tts_worker.py")), mode],
            settings=settings,
            cache=self.root / mode,
        )
        self.clients.append(client)
        return client

    async def test_stream_preserves_low_consonants_and_empty_final_chunk(self):
        client = self.client()
        audio = b"".join([chunk async for chunk in client.stream("test")])
        self.assertEqual(struct.unpack("<5h", audio), (10, 11, 12, 13, 14))
        self.assertGreater(client.last_metrics["first_pcm_delivered_s"], 0)
        self.assertLess(client.last_metrics["first_pcm_delivered_s"], 0.09)

    async def test_cancel_drains_identity_then_next_request_uses_same_worker(self):
        client = self.client()
        cancel = asyncio.Event()
        delivered = []
        async with contextlib.aclosing(client.stream("first", cancel=cancel)) as stream:
            async for chunk in stream:
                delivered.append(chunk)
                cancel.set()
        pid = client.process.pid
        self.assertEqual(len(delivered), 1)
        self.assertGreater(client.last_drain_seconds, 0)
        second = b"".join([chunk async for chunk in client.stream("second")])
        self.assertEqual(struct.unpack("<5h", second), (20, 21, 22, 23, 24))
        self.assertEqual(client.process.pid, pid)
        self.assertEqual(client.restarts, 0)

    async def test_generator_close_drains_and_task_cancel_preserves_boundary(self):
        client = self.client()
        stream = client.stream("first")
        await anext(stream)
        await stream.aclose()
        self.assertGreater(client.last_drain_seconds, 0)
        stream = client.stream("second")
        await anext(stream)
        cancel_task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.005)
        cancel_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancel_task
        await stream.aclose()
        third = b"".join([chunk async for chunk in client.stream("third")])
        self.assertEqual(struct.unpack("<5h", third), (30, 31, 32, 33, 34))

    async def test_startup_and_fragment_timeouts(self):
        for mode in ("startup_timeout", "stalled"):
            with self.subTest(mode=mode), self.assertRaises(TTSError):
                [
                    chunk
                    async for chunk in self.client(
                        mode, startup_timeout=0.2, fragment_timeout=0.2
                    ).stream("test")
                ]

    async def test_stalled_cancel_kills_only_owned_worker_then_restarts(self):
        client = self.client("stalled")
        cancel = asyncio.Event()
        started = time.perf_counter()
        async for _ in client.stream("first", cancel=cancel):
            cancel.set()
        self.assertLess(time.perf_counter() - started, 2)
        self.assertIsNone(client.process)
        self.assertEqual(client.restarts, 1)
        client.command[-1] = "normal"
        self.assertTrue([chunk async for chunk in client.stream("second")])

    async def test_dead_large_truncated_wrong_id_and_empty_stream_fail(self):
        for mode in (
            "dead_start",
            "dead",
            "large",
            "truncated",
            "wrong_id",
            "empty",
            "empty_warmup",
            "bad_count",
        ):
            with self.subTest(mode=mode), self.assertRaises(TTSError):
                [chunk async for chunk in self.client(mode).stream("test")]

    async def test_stderr_is_drained_bounded_and_shutdown_reaps_worker(self):
        client = self.client("stderr")
        self.assertTrue([chunk async for chunk in client.stream("test")])
        process = client.process
        self.assertGreaterEqual(client.stderr_bytes, 200_000)
        self.assertLessEqual(len(client._stderr), 32_768)
        await client.close()
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(TTSError):
            [chunk async for chunk in client.stream("after close")]


class WorkerTests(unittest.TestCase):
    def test_frame_bound_and_empty_warmup_are_rejected(self):
        with self.assertRaises(TTSError):
            write_frame(io.BytesIO(), b"PCM!", 1, b"x" * (MAX_FRAME + 1))

        class EmptyEngine:
            sample_rate = 24000

            def __init__(self, config):
                pass

            def synthesize(self, *args):
                raise TTSError("empty_synthesis")

            def close(self):
                pass

        out = io.BytesIO()
        with patch("jarvis_office.tts_worker.Engine", EmptyEngine):
            self.assertEqual(serve(None, out, io.BytesIO()), 1)
        self.assertNotIn(b"RDY!", out.getvalue())
        self.assertIn(b"ERR!", out.getvalue())

    def test_network_guard_blocks_real_socket_attempts(self):
        code = """
from jarvis_office.tts_worker import install_network_guard
import socket
install_network_guard()
try:
    socket.create_connection(('127.0.0.1', 9), timeout=0.1)
except PermissionError:
    pass
else:
    raise AssertionError('network not denied')
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
