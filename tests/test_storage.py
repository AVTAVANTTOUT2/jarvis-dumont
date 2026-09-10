import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
        return self.store.record_turn("echo", "session", turn,
                                      generation=self.store.generation if generation is None
                                      else generation, user_text="Jarvis, bonjour.", **kwargs)

    async def test_initial_schema_history_and_passive_are_explicit(self):
        settings = await self.store.settings()
        self.assertFalse(settings["history_enabled"])
        self.assertFalse(settings["archive_passive"])
        self.assertIsNone(settings["history_started_at"])
        self.assertIsNone(settings["budget"]["limit"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.turn()
        self.store.record_passive("echo", "session", generation=0, text="Contexte")
        await self.store.flush()
        self.assertEqual((await self.store.rows(table="turns"))["total"], 0)
        self.assertEqual((await self.store.rows(table="passive_archives"))["total"], 0)
        catalog = await self.store.catalog()
        self.assertEqual(len(catalog["tables"]), 7)
        self.assertEqual(self.store.status()["journal_mode"], "delete")

    async def test_partial_turn_and_safe_json_are_persisted_with_relations(self):
        enabled = await self.store.update_settings({"history_enabled": True})
        self.assertIsNotNone(enabled["history_started_at"])
        self.turn(assistant_text="Bonjour. Un texte futur.", delivered_text="Bonjour.",
                  status="interrupted", metrics={"stt_ms": 12, "acoustic_ms": None,
                                                  "api_key": "must not survive"})
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
        filtered = await self.store.rows(table="turns", filters=[
            {"column": "turn_id", "op": "eq", "value": "a"}])
        self.assertEqual(filtered["total"], 1)
        for query in [
            {"table": "turns; DROP TABLE turns"}, {"table": "sqlite_schema"},
            {"table": "turns", "sort": "turn_id;DELETE"}, {"table": "turns", "limit": 101},
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
        self.assertEqual((await self.store.row("sessions", "session"))["row"]["status"],
                         "interrupted")
        await asyncio.gather(self.store.update_settings({"archive_passive": True}),
                             self.store.purge("conversations"))
        self.assertEqual((await self.store.settings())["generation"], self.store.generation)
        self.turn("after")
        await self.store.flush()
        self.assertEqual((await self.store.rows(table="turns"))["total"], 1)

    async def test_large_results_fail_explicitly_and_complete_row_remains_available(self):
        await self.store.update_settings({"history_enabled": True})
        for index in range(15):
            self.store.record_turn("echo", "session", f"t{index}", generation=self.store.generation,
                                   user_text="a" * 100000, assistant_text="b" * 100000,
                                   delivered_text="c" * 100000)
        await self.store.flush()
        with self.assertRaisesRegex(StorageError, "QUERY_RESULT_TOO_LARGE"):
            await self.store.rows(table="turns")
        self.assertEqual(len((await self.store.row("turns", "t1"))["row"]["user_text"]), 100000)

    async def test_budget_is_atomic_persistent_and_cannot_be_reset_by_settings(self):
        with self.assertRaisesRegex(StorageError, "NOMINAL_BUDGET_REQUIRED"):
            await self.store.consume_budget()
        await self.store.update_settings({"budget": {"limit": 2, "period": "day"}})
        results = await asyncio.gather(*[self.store.consume_budget() for _ in range(3)],
                                       return_exceptions=True)
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
        self.turn(assistant_text="=HYPERLINK(\"test\")")
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
