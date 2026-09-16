import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_office.storage import OfficeStore, StorageError


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data" / "office.sqlite3"
        self.store = OfficeStore(self.path)
        await self.store.start()

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    def turn(self, turn="t1", generation=None, **kwargs):
        return self.store.record_turn(
            "echo",
            "session",
            turn,
            generation=self.store.generation if generation is None else generation,
            user_text="Jarvis, bonjour.",
            **kwargs,
        )

    @staticmethod
    def create_v1_database(path):
        with sqlite3.connect(path) as db:
            db.executescript(
                """
                PRAGMA user_version=1;
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES (1,'2026-01-01T00:00:00+00:00');
                CREATE TABLE devices (
                    device_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(device_id),
                    started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL);
                CREATE TABLE turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL REFERENCES devices(device_id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    user_text TEXT NOT NULL, assistant_text TEXT NOT NULL,
                    delivered_text TEXT NOT NULL, status TEXT NOT NULL,
                    metrics_json TEXT NOT NULL, generation INTEGER NOT NULL);
                CREATE TABLE events (
                    event_id INTEGER PRIMARY KEY,
                    device_id TEXT REFERENCES devices(device_id),
                    session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
                    turn_id TEXT REFERENCES turns(turn_id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, kind TEXT NOT NULL, details_json TEXT NOT NULL);
                CREATE TABLE preferences (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE passive_archives (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
                    session_id TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    text TEXT NOT NULL, source TEXT NOT NULL, generation INTEGER NOT NULL);
                INSERT INTO devices VALUES (
                    'echo','echo',datetime('now'),datetime('now'));
                INSERT INTO sessions VALUES (
                    'old-session','echo',datetime('now'),NULL,'open');
                INSERT INTO turns VALUES (
                    'old-turn','old-session','echo',datetime('now'),datetime('now'),
                    'Ancienne question.','Ancienne réponse.','Ancienne réponse.',
                    'complete','{}',0);
                """
            )

    async def test_diagnostic_saturation_is_visible_and_reserves_existing_write_capacity(self):
        capacity = self.store.queue.maxsize // 2
        for i in range(capacity):
            self.assertTrue(
                self.store.record_event("command", details={"generation": i}, diagnostic=True)
            )
        self.assertFalse(self.store.record_event("command", diagnostic=True))
        self.assertFalse(
            self.store.record_event("command", details={"oversize": "x" * 4097}, diagnostic=True)
        )
        self.assertEqual(self.store.status()["diagnostic_dropped"], 2)
        self.assertIsNone(self.store.error)
        self.assertTrue(self.store.record_event("ordinary_event"))
        await self.store.flush()
        self.assertEqual((await self.store.rows(table="events"))["total"], capacity + 1)
        self.assertIsNone(self.store.error)
        metrics = {"existing": "x" * 16000, "llm": {"timeline": {"padding": "y" * 1000}}}
        self.assertTrue(self.turn(metrics=metrics))
        self.assertEqual(self.store.status()["diagnostic_dropped"], 3)
        queued = self.store.queue.get_nowait()
        self.store.queue.task_done()
        saved_metrics = queued[1][-1]
        self.assertEqual(saved_metrics["existing"], metrics["existing"])
        self.assertTrue(saved_metrics["diagnostic_truncated"])
        self.assertNotIn("timeline", saved_metrics["llm"])
        self.assertIn("timeline", metrics["llm"])  # No mutation of the caller's metrics.
        self.assertLessEqual(len(json.dumps(saved_metrics)), 16384)
        self.assertIsNone(self.store.error)

    async def test_initial_schema_history_and_passive_are_explicit(self):
        settings = await self.store.settings()
        self.assertFalse(settings["history_enabled"])
        self.assertFalse(settings["memory_enabled"])
        self.assertFalse(settings["archive_passive"])
        self.assertIsNone(settings["history_started_at"])
        self.assertIsNone(settings["memory_started_at"])
        self.assertIsNone(settings["budget"]["limit"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.turn()
        self.store.record_passive("echo", "session", generation=0, text="Contexte")
        await self.store.flush()
        self.assertEqual((await self.store.rows(table="turns"))["total"], 0)
        self.assertEqual((await self.store.rows(table="passive_archives"))["total"], 0)
        catalog = await self.store.catalog()
        self.assertEqual(len(catalog["tables"]), 8)
        self.assertEqual(self.store.status()["journal_mode"], "delete")

    async def test_memory_activation_enables_history_without_backfill(self):
        self.turn("before", delivered_text="Ancienne réponse.", status="complete")
        await self.store.flush()

        settings = await self.store.update_settings({"memory_enabled": True})
        self.assertTrue(settings["memory_enabled"])
        self.assertTrue(settings["history_enabled"])
        self.assertIsNotNone(settings["memory_started_at"])
        self.turn("after", delivered_text="Nouvelle réponse.", status="complete")
        await self.store.flush()

        memory = await self.store.memory_context("echo")
        self.assertEqual([item["turn_id"] for item in memory["pending"]], ["after"])

    async def test_memory_commit_is_cas_and_survives_restart(self):
        await self.store.update_settings({"memory_enabled": True})
        self.turn("remembered", delivered_text="La couleur est turquoise.", status="complete")
        await self.store.flush()
        memory = await self.store.memory_context("echo")
        cursor = memory["pending"][-1]

        committed = await self.store.commit_memory(
            "echo",
            generation=memory["generation"],
            revision=memory["revision"],
            through_created_at=cursor["created_at"],
            through_turn_id=cursor["turn_id"],
            summary="La couleur préférée indiquée est turquoise.",
        )
        stale = await self.store.commit_memory(
            "echo",
            generation=memory["generation"],
            revision=memory["revision"],
            through_created_at=cursor["created_at"],
            through_turn_id=cursor["turn_id"],
            summary="Résumé périmé.",
        )
        self.assertTrue(committed)
        self.assertFalse(stale)

        await self.store.close()
        self.store = OfficeStore(self.path)
        await self.store.start()
        restored = await self.store.memory_context("echo")
        self.assertEqual(restored["summary"], "La couleur préférée indiquée est turquoise.")
        self.assertEqual(restored["pending"], [])

    async def test_memory_source_uses_only_confirmed_delivered_non_passive_turns(self):
        await self.store.update_settings({"memory_enabled": True})
        self.turn("complete", delivered_text="Livré.", status="complete")
        self.turn("interrupted", delivered_text="Préfixe livré.", status="interrupted")
        self.turn("partial", delivered_text="Non final.", status="partial")
        self.turn("empty", assistant_text="Généré seulement.", status="complete")
        self.turn(
            "passive",
            delivered_text="Réponse issue du contexte passif.",
            status="complete",
            metrics={
                "llm": {
                    "request_context": {
                        "payload_includes_passive_context": True,
                    }
                }
            },
        )
        await self.store.flush()

        memory = await self.store.memory_context("echo")
        self.assertEqual(
            [(item["turn_id"], item["delivered_text"]) for item in memory["pending"]],
            [("complete", "Livré."), ("interrupted", "Préfixe livré.")],
        )

    async def test_memory_reenable_keeps_summary_and_refuses_history_off(self):
        await self.store.update_settings({"memory_enabled": True})
        self.turn("kept", delivered_text="La couleur est turquoise.", status="complete")
        await self.store.flush()
        memory = await self.store.memory_context("echo")
        cursor = memory["pending"][-1]
        await self.store.commit_memory(
            "echo",
            generation=memory["generation"],
            revision=memory["revision"],
            through_created_at=cursor["created_at"],
            through_turn_id=cursor["turn_id"],
            summary="La couleur préférée indiquée est turquoise.",
        )
        with self.assertRaisesRegex(StorageError, "MEMORY_REQUIRES_HISTORY"):
            await self.store.update_settings({"history_enabled": False})
        await self.store.update_settings({"memory_enabled": False})
        restored = await self.store.update_settings({"memory_enabled": True})
        self.assertTrue(restored["history_enabled"])
        memory = await self.store.memory_context("echo")
        self.assertEqual(memory["summary"], "La couleur préférée indiquée est turquoise.")

    async def test_purge_conversations_clears_memory_without_deleting_exports(self):
        await self.store.update_settings({"memory_enabled": True})
        self.turn("gone", delivered_text="À oublier.", status="complete")
        await self.store.flush()
        memory = await self.store.memory_context("echo")
        cursor = memory["pending"][-1]
        await self.store.commit_memory(
            "echo",
            generation=memory["generation"],
            revision=memory["revision"],
            through_created_at=cursor["created_at"],
            through_turn_id=cursor["turn_id"],
            summary="Ne doit pas survivre à la purge.",
        )

        result = await self.store.purge("conversations")

        self.assertTrue(result["exported_copies_unchanged"])
        cleared = await self.store.memory_context("echo")
        self.assertEqual((cleared["summary"], cleared["pending"]), ("", []))
        self.assertEqual((await self.store.rows(table="turns"))["total"], 0)

    async def test_clear_memory_invalidates_stale_commit_without_deleting_turns(self):
        await self.store.update_settings({"memory_enabled": True})
        self.turn("kept", delivered_text="Réponse conservée.", status="complete")
        await self.store.flush()
        memory = await self.store.memory_context("echo")
        cursor = memory["pending"][-1]

        await self.store.clear_memory("echo")
        stale = await self.store.commit_memory(
            "echo",
            generation=memory["generation"],
            revision=memory["revision"],
            through_created_at=cursor["created_at"],
            through_turn_id=cursor["turn_id"],
            summary="Ne doit pas revenir.",
        )
        cleared = await self.store.memory_context("echo")

        self.assertFalse(stale)
        self.assertEqual((cleared["summary"], cleared["pending"]), ("", []))
        self.assertEqual((await self.store.rows(table="turns"))["total"], 1)

    async def test_schema_v1_migrates_atomically_without_backfilling_memory(self):
        await self.store.close()
        self.path.unlink()
        self.create_v1_database(self.path)
        self.store = OfficeStore(self.path)

        await self.store.start()

        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall(),
                [(1,), (2,)],
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM conversation_memory").fetchone()[0], 0
            )

    async def test_restore_accepts_v1_backup_and_migrates_the_isolated_copy(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        restored = Path(self.temp.name) / "restored-v1.sqlite3"
        self.create_v1_database(legacy)

        await asyncio.to_thread(OfficeStore.restore_isolated, legacy, restored)

        with sqlite3.connect(restored) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)

    async def test_business_write_failure_still_sets_global_storage_error(self):
        with patch.object(self.store, "_record_event", side_effect=sqlite3.OperationalError):
            self.assertTrue(self.store.record_event("business_event"))
            await self.store.flush()
        self.assertEqual(self.store.error, "STORAGE_OPERATION_FAILED")
        self.assertEqual(self.store.diagnostic_dropped, 0)

    async def test_partial_turn_and_safe_json_are_persisted_with_relations(self):
        enabled = await self.store.update_settings({"history_enabled": True})
        self.assertIsNotNone(enabled["history_started_at"])
        self.turn(
            assistant_text="Bonjour. Un texte futur.",
            delivered_text="Bonjour.",
            status="interrupted",
            metrics={"stt_ms": 12, "acoustic_ms": None, "api_key": "must not survive"},
        )
        await self.store.flush()
        row = (await self.store.row("turns", "t1"))["row"]
        self.assertEqual(row["status"], "interrupted")
        self.assertNotEqual(row["delivered_text"], row["assistant_text"])
        self.assertIsNone(row["metrics_json"]["acoustic_ms"])
        self.assertEqual(row["metrics_json"]["api_key"], "[EXPURGÉ]")
        self.assertEqual((await self.store.rows(table="sessions"))["total"], 1)
        self.assertEqual((await self.store.rows(table="devices"))["total"], 1)

    async def test_purge_prevents_late_result_resurrection(self):
        await self.store.update_settings({"history_enabled": True, "archive_passive": True})
        generation = self.store.generation
        self.turn()
        self.store.record_passive("echo", "session", generation=generation, text="Archive")
        await self.store.flush()
        await self.store.purge("conversations")
        self.turn("late", generation=generation)
        await self.store.flush()
        self.assertEqual((await self.store.rows(table="turns"))["total"], 0)
        self.assertEqual((await self.store.rows(table="passive_archives"))["total"], 1)
        await self.store.purge("passive_archives")
        self.assertEqual((await self.store.rows(table="passive_archives"))["total"], 0)

    async def test_filters_identifiers_pagination_and_injection(self):
        await self.store.update_settings({"history_enabled": True})
        self.turn("a", metrics={"stt_ms": 1})
        self.turn("b")
        await self.store.flush()
        result = await self.store.rows(table="turns", limit=1, sort="turn_id", direction="desc")
        self.assertEqual(result["rows"][0]["turn_id"], "b")
        self.assertTrue(result["truncated"])
        filtered = await self.store.rows(
            table="turns", filters=[{"column": "turn_id", "op": "eq", "value": "a"}]
        )
        self.assertEqual(filtered["total"], 1)
        for query in [
            {"table": "turns; DROP TABLE turns"},
            {"table": "sqlite_schema"},
            {"table": "turns", "sort": "turn_id;DELETE"},
            {"table": "turns", "limit": 101},
            {"table": "turns", "filters": [{"column": "generation", "op": "eq", "value": "0"}]},
            {"table": "turns", "filters": [{"column": "turn_id", "op": "like", "value": "%"}]},
        ]:
            with self.subTest(query=query), self.assertRaises(StorageError):
                await self.store.rows(**query)
        injected = await self.store.rows(table="turns", q="' OR 1=1 --")
        self.assertEqual(injected["total"], 0)
        self.assertEqual((await self.store.rows(table="turns", q="%"))["total"], 0)

    async def test_history_activation_invalidates_previous_generation(self):
        before = self.store.generation
        await self.store.update_settings({"history_enabled": True})
        self.turn("old", generation=before)
        self.turn("new")
        self.store.close_session("session", status="interrupted")
        await self.store.flush()
        self.assertEqual((await self.store.rows(table="turns"))["total"], 1)
        self.assertEqual(
            (await self.store.row("sessions", "session"))["row"]["status"], "interrupted"
        )
        await asyncio.gather(
            self.store.update_settings({"archive_passive": True}), self.store.purge("conversations")
        )
        self.assertEqual((await self.store.settings())["generation"], self.store.generation)
        self.turn("after")
        await self.store.flush()
        self.assertEqual((await self.store.rows(table="turns"))["total"], 1)

    async def test_large_results_fail_explicitly_and_complete_row_remains_available(self):
        await self.store.update_settings({"history_enabled": True})
        for index in range(15):
            self.store.record_turn(
                "echo",
                "session",
                f"t{index}",
                generation=self.store.generation,
                user_text="a" * 100000,
                assistant_text="b" * 100000,
                delivered_text="c" * 100000,
            )
        await self.store.flush()
        with self.assertRaisesRegex(StorageError, "QUERY_RESULT_TOO_LARGE"):
            await self.store.rows(table="turns")
        self.assertEqual(len((await self.store.row("turns", "t1"))["row"]["user_text"]), 100000)

    async def test_budget_is_atomic_persistent_and_cannot_be_reset_by_settings(self):
        with self.assertRaisesRegex(StorageError, "NOMINAL_BUDGET_REQUIRED"):
            await self.store.consume_budget()
        await self.store.update_settings({"budget": {"limit": 2, "period": "day"}})
        results = await asyncio.gather(
            *[self.store.consume_budget() for _ in range(3)], return_exceptions=True
        )
        self.assertEqual(sum(isinstance(x, StorageError) for x in results), 1)
        self.assertEqual((await self.store.settings())["budget"]["used"], 2)
        await self.store.update_settings({"budget": {"limit": 3, "period": "month"}})
        self.assertEqual((await self.store.settings())["budget"]["used"], 2)
        with self.assertRaises(StorageError):
            await self.store.update_settings({"budget": {"used": 0}})
        await self.store.close()
        self.store = OfficeStore(self.path)
        await self.store.start()
        self.assertEqual((await self.store.settings())["budget"]["used"], 2)

    async def test_export_is_bounded_scoped_and_formula_safe(self):
        await self.store.update_settings({"history_enabled": True})
        self.turn(assistant_text='=HYPERLINK("test")')
        await self.store.flush()
        body, mime = await self.store.export({"table": "turns", "limit": 1}, "csv")
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(mime, "text/csv")
        self.assertTrue(rows[0]["assistant_text"].startswith("'="))
        body, _ = await self.store.export({"table": "turns"}, "json")
        self.assertEqual(json.loads(body)["total"], 1)
        with self.assertRaises(StorageError):
            await self.store.export({"table": "turns", "limit": 1001}, "csv")

    async def test_backup_restore_isolated_and_retention(self):
        await self.store.update_settings({"history_enabled": True})
        self.turn()
        await self.store.flush()
        backup = await self.store.backup()
        isolated = Path(self.temp.name) / "restored.sqlite3"
        await asyncio.to_thread(OfficeStore.restore_isolated, backup, isolated)
        with sqlite3.connect(isolated) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(StorageError):
            OfficeStore.restore_isolated(backup, isolated)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE turns SET created_at='2001-01-01T00:00:00+00:00'")
        await self.store.catalog()
        self.assertEqual((await self.store.rows(table="turns"))["total"], 0)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)

    async def test_queue_overflow_is_visible_and_never_blocks_audio(self):
        store = OfficeStore(Path(self.temp.name) / "small.sqlite3", queue_size=1)
        await store.start()
        try:
            self.assertTrue(store.register_device("one"))
            self.assertFalse(store.register_device("two"))
            self.assertEqual(store.status()["error"], "STORAGE_QUEUE_FULL")
        finally:
            await store.close()


if __name__ == "__main__":
    unittest.main()
