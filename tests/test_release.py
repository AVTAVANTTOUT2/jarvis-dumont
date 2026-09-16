"""Offline lifecycle tests: no launchd mutation, engine, microphone or credentials."""

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_office import release, runtime, service
from jarvis_office.assets import atomic_json
from jarvis_office.config import ConfigError, load_config


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="office release spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        process_identity = patch.object(
            runtime, "identity", return_value={"test_identity": "stable"}
        )
        process_identity.start()
        self.addCleanup(process_identity.stop)
        for module in (release, runtime, service):
            patcher = patch.object(module, "private_root", return_value=self.root)
            patcher.start()
            self.addCleanup(patcher.stop)

    def candidate(self, number):
        name = f"0.1.0-{number:012x}"
        folder = release.target(name)
        folder.mkdir(parents=True)
        (folder / "app.txt").write_text("lightweight activation fixture, no engines")
        atomic_json(
            folder / "release.json",
            {
                "name": name,
                "sha": f"{number:040x}",
                "assets": {},
                "files": release.files_contract(folder),
            },
        )
        return name

    def test_lock_before_resources_second_instance_and_stale_inode(self):
        first = runtime.Instance()
        inode = runtime.lock_path().stat().st_ino
        with self.assertRaisesRegex(ConfigError, "already_running"):
            runtime.Instance()
        self.assertTrue(runtime.status()["alive"])
        first.close()
        self.assertEqual(runtime.status()["lock"], "stale_unlocked")
        second = runtime.Instance()
        self.assertEqual(runtime.lock_path().stat().st_ino, inode)
        second.close()

    def test_pid_reuse_does_not_claim_owner(self):
        instance = runtime.Instance()
        self.addCleanup(instance.close)
        with patch.object(runtime, "identity", return_value={"started": "different process"}):
            self.assertEqual(runtime.status()["error"], "instance_identity_mismatch")

    def test_crashed_owner_lock_released_by_kernel(self):
        program = (
            "import os; from pathlib import Path; from jarvis_office import runtime; "
            "runtime.private_root=lambda:Path(os.environ['OFFICE_TEST_ROOT']); "
            "runtime.Instance(); os._exit(0)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", program],
            env={**os.environ, "OFFICE_TEST_ROOT": str(self.root)},
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(runtime.status()["lock"], "stale_unlocked")

    def test_lock_symlink_refused_without_touching_target(self):
        runtime.lock_path().parent.mkdir(mode=0o700)
        victim = self.root / "keep"
        victim.write_text("unchanged")
        runtime.lock_path().symlink_to(victim)
        with self.assertRaises(OSError):
            runtime.Instance()
        self.assertEqual(victim.read_text(), "unchanged")

    def test_real_filesystem_activation_b_then_rollback_a(self):
        a, b = self.candidate(1), self.candidate(2)
        release.activate(a)
        release.activate(b)
        self.assertEqual(release.active_name(), b)
        release.rollback()
        self.assertEqual(release.active_name(), a)
        self.assertTrue(release.target(b).exists())

    def test_activation_refuses_sqlite_schema_above_manifest_max(self):
        name = self.candidate(1)
        (self.root / "data").mkdir()
        with sqlite3.connect(self.root / "data/office.sqlite3") as connection:
            connection.execute("PRAGMA user_version=2")
        path = release.target(name) / "release.json"
        data = json.loads(path.read_text())
        data["sqlite_schema_max"] = 1
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ConfigError, "release_database_schema_incompatible"):
            release.activate(name)
        self.assertIsNone(release.active_name())
        data["sqlite_schema_max"] = 2
        path.write_text(json.dumps(data))
        release.activate(name)
        self.assertEqual(release.active_name(), name)

    def test_invalid_candidate_never_changes_current(self):
        a, b = self.candidate(1), self.candidate(2)
        release.activate(a)
        (release.target(b) / "app.txt").write_text("corrupt")
        with self.assertRaisesRegex(ConfigError, "checksum"):
            release.activate(b)
        self.assertEqual(release.active_name(), a)

    def test_extra_file_and_escaping_target_fail(self):
        name = self.candidate(1)
        (release.target(name) / "unexpected").write_bytes(b"new")
        with self.assertRaises(ConfigError):
            release.verify(name)
        for bad in ("../current", "/tmp", "0.1.0-123", ""):
            with self.assertRaises(ConfigError):
                release.target(bad)

    def test_activation_refuses_running_instance(self):
        name = self.candidate(1)
        instance = runtime.Instance()
        try:
            with self.assertRaisesRegex(ConfigError, "already_running"):
                release.activate(name)
            self.assertIsNone(release.active_name())
        finally:
            instance.close()

    def test_rotating_logs_do_not_persist_text_or_network_exception(self):
        log = runtime.RuntimeLog(self.root / "logs")
        log.handler.maxBytes = 400
        for n in range(25):
            log.update(
                {
                    "session": "test",
                    "turn": str(n),
                    "state": "paused",
                    "error": "Authorization: supersecret /private/prompt",
                    "transcription": "private speech",
                    "response": "private answer",
                }
            )
        log.close()
        files = list((self.root / "logs").iterdir())
        self.assertLessEqual(len(files), 4)
        data = "".join(p.read_text() for p in files)
        for secret in ("supersecret", "private speech", "private answer", "/private/"):
            self.assertNotIn(secret, data)
        self.assertIn("unclassified_error", data)

    def test_alive_and_listening_are_not_inferred_from_port(self):
        state = runtime.health(
            {
                "state": "paused",
                "engines_ready": False,
                "output_verified": True,
                "microphone": "closed",
            }
        )
        self.assertTrue(state["alive"])
        self.assertFalse(state["audio_ready"])
        self.assertFalse(state["conversation_ready"])
        self.assertFalse(state["listening"])

    def test_launchagent_has_no_shell_secret_autorestart_or_arm(self):
        data = plistlib.loads(plistlib.dumps(service.specification()))
        self.assertFalse(data["RunAtLoad"])
        self.assertFalse(data["KeepAlive"])
        self.assertNotIn("--arm", data["ProgramArguments"])
        self.assertIn("current/main/bin/python", data["ProgramArguments"][0])
        self.assertNotIn("KEY", json.dumps(data))
        self.assertEqual(data["Label"], "com.jarvisoffice.voice")

    def test_uninstall_preserves_data_and_refuses_foreign_plist(self):
        file = self.root / "office.plist"
        with patch.object(service, "plist_path", return_value=file):
            file.write_bytes(plistlib.dumps({"Label": "someone.else"}))
            with self.assertRaisesRegex(ConfigError, "conflicts"):
                service.manage("uninstall")
            file.write_bytes(plistlib.dumps(service.specification()))
            result = subprocess.CompletedProcess([], 1, "", "")
            with patch.object(service, "launchctl", return_value=result):
                self.assertTrue(service.manage("uninstall")["private_data_retained"])
            self.assertFalse(file.exists())
            self.assertTrue(self.root.exists())

    def test_release_config_uses_sibling_envs_not_development(self):
        name = self.candidate(1)
        directory = release.target(name)
        config = self.root / "config.toml"
        config.write_text(
            '[tts]\npython="/development/tts/python"\n[speech]\npython="/development/stt/python"\n'
        )
        with patch("jarvis_office.config.sys.prefix", str(directory / "main")):
            settings = load_config(config)
        self.assertEqual(settings.tts.python, directory / "tts/bin/python")
        self.assertEqual(settings.speech.python, directory / "stt/bin/python")


if __name__ == "__main__":
    unittest.main()
