"""Private Office data, serialized away from the audio/event-loop thread."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TABLES = (
    "devices",
    "sessions",
    "turns",
    "conversation_memory",
    "events",
    "preferences",
    "passive_archives",
    "schema_migrations",
)
SCHEMA_VERSION = 2
MEMORY_SUMMARY_CHARS = 4000
MEMORY_CONTEXT_TURNS = 4
MEMORY_ROLLUP_TURNS = 4
MEMORY_ROLLUP_CHARS = 6000
MEMORY_TURN_CHARS = 4096
ORB_STYLES = (
    "auto",
    "working",
    "searching",
    "solving",
    "listening",
    "connecting",
    "weaving",
    "composing",
    "breathing",
    "shaping",
)
DEFAULTS: dict[str, Any] = {
    "orb_style": "auto",
    "history_enabled": False,
    "retention_days": 30,
    "history_started_at": None,
    "memory_enabled": False,
    "memory_started_at": None,
    "archive_passive": False,
    "passive_retention_days": 1,
    "backup_retention_days": 7,
    "generation": 0,
    "budget": {
        "limit": None,
        "period": "month",
        "used": 0,
        "period_started_at": None,
        "exhaustion": "block",
    },
}
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS schema_migrations (
 version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
 device_id TEXT PRIMARY KEY, name TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 session_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
 started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
 turn_id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
 device_id TEXT NOT NULL REFERENCES devices(device_id), created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, user_text TEXT NOT NULL, assistant_text TEXT NOT NULL,
 delivered_text TEXT NOT NULL, status TEXT NOT NULL, metrics_json TEXT NOT NULL,
 generation INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, created_at);
CREATE INDEX IF NOT EXISTS turns_created ON turns(created_at);
CREATE TABLE IF NOT EXISTS conversation_memory (
 device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
 summary_text TEXT NOT NULL, through_created_at TEXT, through_turn_id TEXT,
 generation INTEGER NOT NULL, revision INTEGER NOT NULL,
 started_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 event_id INTEGER PRIMARY KEY, device_id TEXT REFERENCES devices(device_id),
 session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
 turn_id TEXT REFERENCES turns(turn_id) ON DELETE CASCADE, created_at TEXT NOT NULL,
 kind TEXT NOT NULL, details_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_created ON events(created_at);
CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passive_archives (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
 session_id TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 text TEXT NOT NULL, source TEXT NOT NULL, generation INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS passive_expiry ON passive_archives(expires_at);
INSERT OR IGNORE INTO schema_migrations
 VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
PRAGMA user_version=2;
COMMIT;
"""


class StorageError(Exception):
    """Only constant, safe codes cross API boundaries."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise StorageError("INVALID_IDENTIFIER")
    return value


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise StorageError("INVALID_TIMESTAMP")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StorageError("INVALID_TIMESTAMP") from None
    return value


def redact(value: Any) -> Any:
    """Redact credential-shaped values, including accidental content in exports."""
    if isinstance(value, dict):
        return {
            str(k): (
                "[EXPURGÉ]"
                if re.search(
                    r"secret|token|password|authorization|private.?key|api.?key|pin.?hash",
                    str(k),
                    re.I,
                )
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[EXPURGÉ]", value)
        value = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [EXPURGÉ]", value)
        return re.sub(
            r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+",
            r"\1=[EXPURGÉ]",
            value,
        )
    return value


class OfficeStore:
    def __init__(self, path: Path, *, queue_size: int = 128) -> None:
        self.path = Path(path)
        self.orb_style = "auto"
        self.queue: asyncio.Queue[
            tuple[Callable[..., Any], tuple[Any, ...], asyncio.Future[Any] | None, bool]
        ] = asyncio.Queue(queue_size)
        self.worker: asyncio.Task[None] | None = None
        self.generation = 0
        self.error: str | None = None
        self.ready = False
        self._closing = False
        self._last_retention = 0.0
        self.diagnostic_dropped = 0
        self._mutation_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.worker is not None:
            return
        try:
            self.generation = await asyncio.to_thread(self._initialize)
        except (OSError, sqlite3.Error, ValueError):
            self.error = "STORAGE_UNAVAILABLE"
            raise StorageError(self.error) from None
        self.ready = True
        self.worker = asyncio.create_task(self._work(), name="office-storage")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=0.5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        return db

    def _initialize(self) -> int:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise StorageError("UNSAFE_DATABASE_PATH")
        # Create privately before SQLite can create the file using the process umask.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        db = self._connect()
        try:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise StorageError("SCHEMA_TOO_NEW")
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript(SCHEMA)
            with db:
                for key, value in DEFAULTS.items():
                    db.execute(
                        "INSERT OR IGNORE INTO preferences VALUES (?,?)", (key, _json(value))
                    )
            self._retain(db)
            self.orb_style = self._settings(db)["orb_style"]
            return int(self._settings(db)["generation"])
        finally:
            db.close()

    async def _work(self) -> None:
        while True:
            fn, args, future, diagnostic = await self.queue.get()
            try:
                result = await asyncio.to_thread(self._execute, fn, args)
                if future is not None and not future.done():
                    future.set_result(result)
            except Exception as exc:
                if diagnostic:
                    self.diagnostic_dropped = min(2**63 - 1, self.diagnostic_dropped + 1)
                    continue
                code = str(exc) if isinstance(exc, StorageError) else "STORAGE_OPERATION_FAILED"
                if not isinstance(exc, StorageError) or code.startswith("STORAGE"):
                    self.error = code
                if future is not None and not future.done():
                    future.set_exception(StorageError(code))
            finally:
                self.queue.task_done()

    def _execute(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        db = self._connect()
        try:
            if time.monotonic() - self._last_retention > 3600:
                self._retain(db)
            return fn(db, *args)
        finally:
            db.close()

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        if not self.ready or self._closing:
            raise StorageError("STORAGE_UNAVAILABLE")
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((fn, args, future, False))
        except asyncio.QueueFull:
            self.error = "STORAGE_QUEUE_FULL"
            raise StorageError(self.error) from None
        # Cancellation of an HTTP caller must not cancel a committed write or orphan its future.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            raise

    def _enqueue(self, fn: Callable[..., Any], *args: Any, diagnostic: bool = False) -> bool:
        if not self.ready or self._closing:
            self.error = "STORAGE_UNAVAILABLE"
            return False
        try:
            self.queue.put_nowait((fn, args, None, diagnostic))
            return True
        except asyncio.QueueFull:
            self.error = "STORAGE_QUEUE_FULL"
            return False

    async def flush(self) -> None:
        await self.queue.join()

    async def close(self) -> None:
        self._closing = True
        await self.flush()
        if self.worker is not None:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            self.worker = None
        self.ready = False

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "error": self.error,
            "queue_depth": self.queue.qsize(),
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": "delete",
            "schema_version": SCHEMA_VERSION,
            "generation": self.generation,
            "diagnostic_dropped": self.diagnostic_dropped,
        }

    @staticmethod
    def _settings(db: sqlite3.Connection) -> dict[str, Any]:
        return {
            r["key"]: json.loads(r["value_json"])
            for r in db.execute("SELECT key,value_json FROM preferences")
            if r["key"] in DEFAULTS
        }

    async def settings(self) -> dict[str, Any]:
        return dict(await self._call(self._current_settings))

    def _current_settings(self, db: sqlite3.Connection) -> dict[str, Any]:
        settings = self._settings(db)
        self._roll_budget(settings["budget"])
        return settings

    @staticmethod
    def _roll_budget(budget: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if budget["period"] == "month":
            start = start.replace(day=1)
        stamp = start.isoformat()
        if budget["period_started_at"] != stamp:
            budget.update(used=0, period_started_at=stamp)

    async def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        async with self._mutation_lock:
            return dict(await self._call(self._update_settings, changes))

    def _update_settings(self, db: sqlite3.Connection, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "orb_style",
            "history_enabled",
            "memory_enabled",
            "retention_days",
            "archive_passive",
            "passive_retention_days",
            "backup_retention_days",
            "budget",
        }
        if not changes or not changes.keys() <= allowed:
            raise StorageError("INVALID_SETTINGS")
        settings = self._current_settings(db)
        for key, value in changes.items():
            if key == "orb_style":
                if not isinstance(value, str) or value not in ORB_STYLES:
                    raise StorageError("INVALID_SETTINGS")
            elif key in ("history_enabled", "memory_enabled", "archive_passive"):
                if type(value) is not bool:
                    raise StorageError("INVALID_SETTINGS")
            elif key.endswith("days"):
                if type(value) is not int or not 1 <= value <= 365:
                    raise StorageError("INVALID_SETTINGS")
            elif key == "budget":
                if not isinstance(value, dict) or not value.keys() <= {
                    "limit",
                    "period",
                    "exhaustion",
                }:
                    raise StorageError("INVALID_BUDGET")
                merged = dict(settings["budget"], **value)
                if merged["limit"] is not None and (
                    type(merged["limit"]) is not int or not 1 <= merged["limit"] <= 100000
                ):
                    raise StorageError("INVALID_BUDGET")
                if merged["period"] not in ("day", "month") or merged["exhaustion"] != "block":
                    raise StorageError("INVALID_BUDGET")
                # A setting edit never erases consumed usage within the current period.
                if merged["period"] != settings["budget"]["period"]:
                    merged["period_started_at"] = None
                used = settings["budget"]["used"]
                self._roll_budget(merged)
                merged["used"] = max(used, merged["used"])
                value = merged
            settings[key] = value
        if settings["memory_enabled"]:
            if changes.get("history_enabled") is False:
                raise StorageError("MEMORY_REQUIRES_HISTORY")
            settings["history_enabled"] = True
        if settings["history_enabled"] and settings["history_started_at"] is None:
            settings["history_started_at"] = utc_now()
        if settings["memory_enabled"] and settings["memory_started_at"] is None:
            settings["memory_started_at"] = utc_now()
        previous = self._settings(db)
        if any(
            settings[key] != previous[key]
            for key in ("history_enabled", "memory_enabled", "archive_passive")
        ):
            self.generation += 1
            settings["generation"] = self.generation
        with db:
            for key, value in settings.items():
                db.execute("UPDATE preferences SET value_json=? WHERE key=?", (_json(value), key))
            if settings["generation"] != previous["generation"]:
                db.execute(
                    "UPDATE conversation_memory SET generation=?,revision=revision+1,updated_at=?",
                    (settings["generation"], utc_now()),
                )
        self._retain(db)
        self.orb_style = settings["orb_style"]
        return settings

    async def consume_budget(self) -> dict[str, Any]:
        return dict(await self._call(self._consume_budget))

    def _consume_budget(self, db: sqlite3.Connection) -> dict[str, Any]:
        with db:
            db.execute("BEGIN IMMEDIATE")
            budget = self._current_settings(db)["budget"]
            if budget["limit"] is None:
                raise StorageError("NOMINAL_BUDGET_REQUIRED")
            if budget["used"] >= budget["limit"]:
                raise StorageError("NOMINAL_BUDGET_EXHAUSTED")
            budget["used"] += 1
            db.execute("UPDATE preferences SET value_json=? WHERE key='budget'", (_json(budget),))
        return dict(budget)

    @staticmethod
    def _device(db: sqlite3.Connection, device_id: str, stamp: str) -> None:
        db.execute(
            "INSERT INTO devices VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
            "last_seen_at=excluded.last_seen_at",
            (device_id, device_id, stamp, stamp),
        )

    def register_device(self, device_id: str) -> bool:
        return self._enqueue(self._register_device, _identifier(device_id))

    def _register_device(self, db: sqlite3.Connection, device_id: str) -> None:
        with db:
            self._device(db, device_id, utc_now())

    def close_session(self, session_id: str, *, status: str = "closed") -> bool:
        if status not in ("closed", "interrupted", "error"):
            raise StorageError("INVALID_SESSION_STATUS")
        return self._enqueue(self._close_session, _identifier(session_id), status)

    @staticmethod
    def _close_session(db: sqlite3.Connection, session: str, status: str) -> None:
        with db:
            db.execute(
                "UPDATE sessions SET ended_at=?,status=? WHERE session_id=?",
                (utc_now(), status, session),
            )

    def record_turn(
        self,
        device_id: str,
        session_id: str,
        turn_id: str,
        *,
        generation: int,
        user_text: str,
        assistant_text: str = "",
        delivered_text: str = "",
        status: str = "partial",
        metrics: dict[str, Any] | None = None,
    ) -> bool:
        if status not in ("complete", "partial", "interrupted", "error"):
            raise StorageError("INVALID_TURN_STATUS")
        ids = tuple(_identifier(v) for v in (device_id, session_id, turn_id))
        texts = tuple(redact(v[:100000]) for v in (user_text, assistant_text, delivered_text))
        safe_metrics = redact(metrics or {})
        if any(len(value) > 100000 for value in (user_text, assistant_text, delivered_text)):
            safe_metrics["storage_text_truncated"] = True
        if len(_json(safe_metrics)) > 16384:
            # New optional diagnostics cannot make a previously storable turn fail.
            llm = safe_metrics.get("llm", {})
            removed = 0
            for key in ("timeline", "request_context"):
                if isinstance(llm, dict) and key in llm:
                    del llm[key]
                    removed += 1
            for key in (
                "capture_opened",
                "first_block_consumed",
                "last_block_consumed",
                "input_closed",
                "segmentation_reason",
                "input_blocks",
                "input_audio_s",
                "normalized_samples",
                "utterance_samples",
            ):
                if key in safe_metrics:
                    del safe_metrics[key]
                    removed += 1
            if removed:
                self.diagnostic_dropped = min(2**63 - 1, self.diagnostic_dropped + 1)
                safe_metrics["diagnostic_truncated"] = True
                if len(_json(safe_metrics)) > 16384:
                    del safe_metrics["diagnostic_truncated"]
        if len(_json(safe_metrics)) > 16384:
            raise StorageError("METRICS_TOO_LARGE")
        return self._enqueue(self._record_turn, *ids, generation, *texts, status, safe_metrics)

    def _record_turn(
        self,
        db: sqlite3.Connection,
        device: str,
        session: str,
        turn: str,
        generation: int,
        user: str,
        assistant: str,
        delivered: str,
        status: str,
        metrics: dict[str, Any],
    ) -> None:
        settings = self._settings(db)
        if generation != self.generation or generation != settings["generation"]:
            return
        if not settings["history_enabled"]:
            return
        stamp = utc_now()
        with db:
            self._device(db, device, stamp)
            db.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?,?,?,NULL,'open')",
                (session, device, stamp),
            )
            db.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(turn_id) DO UPDATE SET updated_at=excluded.updated_at,"
                "assistant_text=excluded.assistant_text,delivered_text=excluded.delivered_text,"
                "status=excluded.status,metrics_json=excluded.metrics_json",
                (
                    turn,
                    session,
                    device,
                    stamp,
                    stamp,
                    user,
                    assistant,
                    delivered,
                    status,
                    _json(metrics),
                    generation,
                ),
            )
        self._retain(db)

    async def memory_context(self, device_id: str) -> dict[str, Any]:
        """Load one bounded persistent summary and bounded unsummarized turns."""
        return dict(await self._call(self._memory_context, _identifier(device_id)))

    def _memory_context(self, db: sqlite3.Connection, device: str) -> dict[str, Any]:
        settings = self._settings(db)
        empty = {
            "enabled": False,
            "generation": settings["generation"],
            "revision": 0,
            "summary": "",
            "pending": [],
            "rollup": [],
            "pending_count": 0,
            "pending_chars": 0,
            "should_summarize": False,
            "updated_at": None,
        }
        if not settings["memory_enabled"]:
            return empty
        stamp = utc_now()
        with db:
            self._device(db, device, stamp)
            db.execute(
                "INSERT OR IGNORE INTO conversation_memory VALUES (?,?,NULL,NULL,?,?,?,?)",
                (
                    device,
                    "",
                    settings["generation"],
                    0,
                    settings["memory_started_at"] or stamp,
                    stamp,
                ),
            )
        memory = db.execute(
            "SELECT * FROM conversation_memory WHERE device_id=?", (device,)
        ).fetchone()
        if memory is None:
            raise StorageError("MEMORY_UNAVAILABLE")
        where = (
            "device_id=? AND created_at>=? AND status IN ('complete','interrupted') "
            "AND delivered_text<>'' AND "
            "COALESCE(json_extract(metrics_json,"
            "'$.llm.request_context.payload_includes_passive_context'),0)<>1"
        )
        values: list[Any] = [device, memory["started_at"]]
        if memory["through_created_at"] is not None:
            where += " AND (created_at>? OR (created_at=? AND turn_id>?))"
            values.extend(
                [
                    memory["through_created_at"],
                    memory["through_created_at"],
                    memory["through_turn_id"],
                ]
            )
        count, chars = db.execute(
            f"SELECT COUNT(*),COALESCE(SUM(length(user_text)+length(delivered_text)),0) "
            f"FROM turns WHERE {where}",
            values,
        ).fetchone()
        columns = (
            "turn_id,created_at,status,"
            f"substr(user_text,1,{MEMORY_TURN_CHARS}) AS user_text,"
            f"substr(delivered_text,1,{MEMORY_TURN_CHARS}) AS delivered_text"
        )
        recent = list(
            db.execute(
                f"SELECT {columns} FROM turns WHERE {where} "
                "ORDER BY created_at DESC,turn_id DESC LIMIT ?",
                [*values, MEMORY_CONTEXT_TURNS],
            )
        )
        recent.reverse()
        should_summarize = count >= MEMORY_ROLLUP_TURNS or chars >= MEMORY_ROLLUP_CHARS
        rollup_piece = MEMORY_ROLLUP_CHARS // (MEMORY_ROLLUP_TURNS * 2)
        rollup = (
            list(
                db.execute(
                    "SELECT turn_id,created_at,status,"
                    f"substr(user_text,1,{rollup_piece}) AS user_text,"
                    f"substr(delivered_text,1,{rollup_piece}) AS delivered_text "
                    f"FROM turns WHERE {where} "
                    "ORDER BY created_at,turn_id LIMIT ?",
                    [*values, MEMORY_ROLLUP_TURNS],
                )
            )
            if should_summarize
            else []
        )
        return {
            "enabled": True,
            "generation": memory["generation"],
            "revision": memory["revision"],
            "summary": memory["summary_text"],
            "pending": [dict(row) for row in recent],
            "rollup": [dict(row) for row in rollup],
            "pending_count": count,
            "pending_chars": chars,
            "should_summarize": should_summarize,
            "updated_at": memory["updated_at"],
        }

    async def commit_memory(
        self,
        device_id: str,
        *,
        generation: int,
        revision: int,
        through_created_at: str,
        through_turn_id: str,
        summary: str,
    ) -> bool:
        """Commit a summary only if its source generation and revision are current."""
        if (
            type(generation) is not int
            or generation < 0
            or type(revision) is not int
            or revision < 0
            or not isinstance(summary, str)
            or len(summary) > MEMORY_SUMMARY_CHARS
        ):
            raise StorageError("INVALID_MEMORY")
        safe_summary = redact(summary.strip())
        if not isinstance(safe_summary, str):
            raise StorageError("INVALID_MEMORY")
        return bool(
            await self._call(
                self._commit_memory,
                _identifier(device_id),
                generation,
                revision,
                _timestamp(through_created_at),
                _identifier(through_turn_id),
                safe_summary,
            )
        )

    def _commit_memory(
        self,
        db: sqlite3.Connection,
        device: str,
        generation: int,
        revision: int,
        created_at: str,
        turn: str,
        summary: str,
    ) -> bool:
        settings = self._settings(db)
        memory = db.execute(
            "SELECT * FROM conversation_memory WHERE device_id=?", (device,)
        ).fetchone()
        if (
            not settings["memory_enabled"]
            or generation != settings["generation"]
            or memory is None
            or memory["generation"] != generation
            or memory["revision"] != revision
        ):
            return False
        cursor_values: list[Any] = [memory["started_at"]]
        cursor = ""
        if memory["through_created_at"] is not None:
            cursor = " AND (created_at>? OR (created_at=? AND turn_id>?))"
            cursor_values.extend(
                [
                    memory["through_created_at"],
                    memory["through_created_at"],
                    memory["through_turn_id"],
                ]
            )
        eligible = db.execute(
            "SELECT 1 FROM turns WHERE device_id=? AND turn_id=? AND created_at=? "
            "AND created_at>=? AND status IN ('complete','interrupted') "
            "AND delivered_text<>'' AND "
            "COALESCE(json_extract(metrics_json,"
            "'$.llm.request_context.payload_includes_passive_context'),0)<>1" + cursor,
            [device, turn, created_at, *cursor_values],
        ).fetchone()
        if eligible is None:
            return False
        with db:
            changed = db.execute(
                "UPDATE conversation_memory SET summary_text=?,through_created_at=?,"
                "through_turn_id=?,revision=revision+1,updated_at=? "
                "WHERE device_id=? AND generation=? AND revision=?",
                (summary, created_at, turn, utc_now(), device, generation, revision),
            ).rowcount
        return changed == 1

    async def clear_memory(self, device_id: str) -> dict[str, Any]:
        """Forget one device's persistent summary without deleting conversation archives."""
        async with self._mutation_lock:
            return dict(await self._call(self._clear_memory, _identifier(device_id)))

    def _clear_memory(self, db: sqlite3.Connection, device: str) -> dict[str, Any]:
        settings = self._settings(db)
        stamp = utc_now()
        with db:
            self._device(db, device, stamp)
            db.execute(
                "INSERT INTO conversation_memory VALUES (?,?,NULL,NULL,?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET summary_text='',"
                "through_created_at=NULL,through_turn_id=NULL,"
                "generation=excluded.generation,revision=conversation_memory.revision+1,"
                "started_at=excluded.started_at,updated_at=excluded.updated_at",
                (device, "", settings["generation"], 0, stamp, stamp),
            )
        row = db.execute(
            "SELECT revision FROM conversation_memory WHERE device_id=?", (device,)
        ).fetchone()
        return {"device_id": device, "revision": row["revision"], "cleared_at": stamp}

    async def remember_echo_mode(self, device_id: str, mode: str) -> None:
        """Persist the last owner-selected Echo mode until an explicit change."""
        await self._call(self._remember_echo_mode, _identifier(device_id), mode)

    def _remember_echo_mode(self, db: sqlite3.Connection, device: str, mode: str) -> None:
        if mode not in {"OFF", "ACTIVE", "PASSIVE"}:
            raise StorageError("INVALID_MODE")
        row = db.execute("SELECT value_json FROM preferences WHERE key='echo_modes'").fetchone()
        modes = json.loads(row["value_json"]) if row else {}
        if not isinstance(modes, dict):
            modes = {}
        modes[device] = mode
        with db:
            db.execute(
                "INSERT INTO preferences VALUES ('echo_modes', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (_json(modes),),
            )

    async def echo_mode(self, device_id: str) -> str:
        return str(await self._call(self._echo_mode, _identifier(device_id)))

    async def tv_registry(self) -> dict[str, Any]:
        raw = await self._call(self._tv_registry)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
        devices = data.get("devices")
        playlists = data.get("playlists")
        return {
            "enabled": data.get("enabled") is True,
            "default_video_app": data["default_video_app"]
            if data.get("default_video_app") in {"smarttube", "avt"}
            else "smarttube",
            "default_film_app": data["default_film_app"]
            if data.get("default_film_app") in {"smarttube", "avt"}
            else "avt",
            "devices": devices if isinstance(devices, dict) else {},
            "playlists": playlists if isinstance(playlists, list) else [],
        }

    def _tv_registry(self, db: sqlite3.Connection) -> str:
        row = db.execute("SELECT value_json FROM preferences WHERE key='tv_registry'").fetchone()
        return str(row["value_json"]) if row else ""

    async def save_tv_registry(self, registry: dict[str, Any]) -> None:
        if not isinstance(registry, dict) or not registry.keys() <= {
            "enabled",
            "default_video_app",
            "default_film_app",
            "devices",
            "playlists",
        }:
            raise StorageError("INVALID_TV_REGISTRY")
        await self._call(self._save_tv_registry, _json(registry))

    def _save_tv_registry(self, db: sqlite3.Connection, payload: str) -> None:
        with db:
            db.execute(
                "INSERT INTO preferences VALUES ('tv_registry', ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (payload,),
            )

    def _echo_mode(self, db: sqlite3.Connection, device: str) -> str:
        row = db.execute("SELECT value_json FROM preferences WHERE key='echo_modes'").fetchone()
        if row is None:
            return "OFF"
        modes = json.loads(row["value_json"])
        mode = modes.get(device, "OFF") if isinstance(modes, dict) else "OFF"
        return mode if mode in {"OFF", "ACTIVE", "PASSIVE"} else "OFF"

    def record_passive(
        self,
        device_id: str,
        session_id: str,
        *,
        generation: int,
        text: str,
        source: str = "echo",
    ) -> bool:
        return self._enqueue(
            self._record_passive,
            _identifier(device_id),
            _identifier(session_id),
            generation,
            redact(text[:20000]),
            _identifier(source),
        )

    def _record_passive(
        self,
        db: sqlite3.Connection,
        device: str,
        session: str,
        generation: int,
        text: str,
        source: str,
    ) -> None:
        settings = self._settings(db)
        if (
            generation != self.generation
            or generation != settings["generation"]
            or not settings["archive_passive"]
        ):
            return
        stamp = utc_now()
        expires = (
            datetime.now(UTC) + timedelta(days=settings["passive_retention_days"])
        ).isoformat()
        with db:
            self._device(db, device, stamp)
            db.execute(
                "INSERT INTO passive_archives VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), device, session, stamp, expires, text, source, generation),
            )
        self._retain(db)

    def record_event(
        self,
        kind: str,
        *,
        device_id: str | None = None,
        details: dict[str, Any] | None = None,
        diagnostic: bool = False,
    ) -> bool:
        payload = _json(redact(details or {}))
        # Best-effort metadata must not set the storage error that gates the product.
        # Leave half the existing queue for ordinary writes; never wait for diagnostic space.
        if diagnostic and (
            not self.ready
            or self._closing
            or len(payload) > 4096
            or self.queue.qsize() >= max(1, self.queue.maxsize // 2)
        ):
            self.diagnostic_dropped = min(2**63 - 1, self.diagnostic_dropped + 1)
            return False
        if len(payload) > 4096:
            raise StorageError("EVENT_TOO_LARGE")
        return self._enqueue(
            self._record_event,
            _identifier(kind),
            _identifier(device_id) if device_id else None,
            payload,
            diagnostic=diagnostic,
        )

    def _record_event(
        self,
        db: sqlite3.Connection,
        kind: str,
        device: str | None,
        data: str,
    ) -> None:
        stamp = utc_now()
        with db:
            if device:
                self._device(db, device, stamp)
            db.execute(
                "INSERT INTO events(device_id,created_at,kind,details_json) VALUES (?,?,?,?)",
                (device, stamp, kind, data),
            )
        self._retain(db)

    def _retain(self, db: sqlite3.Connection) -> None:
        settings = self._settings(db)
        now = datetime.now(UTC)
        oldest = (now - timedelta(days=settings["retention_days"])).isoformat()
        with db:
            db.execute("DELETE FROM turns WHERE created_at < ?", (oldest,))
            db.execute(
                "DELETE FROM sessions WHERE NOT EXISTS "
                "(SELECT 1 FROM turns WHERE turns.session_id=sessions.session_id)"
            )
            db.execute("DELETE FROM events WHERE created_at < ?", (oldest,))
            db.execute(
                "DELETE FROM events WHERE event_id NOT IN "
                "(SELECT event_id FROM events ORDER BY event_id DESC LIMIT 10000)"
            )
            passive_oldest = (now - timedelta(days=settings["passive_retention_days"])).isoformat()
            db.execute(
                "DELETE FROM passive_archives WHERE expires_at < ? OR created_at < ?",
                (now.isoformat(), passive_oldest),
            )
            db.execute(
                "DELETE FROM passive_archives WHERE id NOT IN "
                "(SELECT id FROM passive_archives ORDER BY created_at DESC LIMIT 10000)"
            )
        backups = self.path.parent / "backups"
        if backups.is_dir() and not backups.is_symlink():
            cutoff = time.time() - settings["backup_retention_days"] * 86400
            for item in backups.glob("office-*.sqlite3"):
                if not item.is_symlink() and item.stat().st_mtime < cutoff:
                    item.unlink()
        self._last_retention = time.monotonic()

    async def purge(self, scope: str) -> dict[str, Any]:
        if scope not in ("conversations", "passive_archives"):
            raise StorageError("INVALID_PURGE_SCOPE")
        async with self._mutation_lock:
            self.generation += 1  # invalidate in-flight results before waiting for the write queue
            return dict(await self._call(self._purge, scope, self.generation))

    def _purge(self, db: sqlite3.Connection, scope: str, generation: int) -> dict[str, Any]:
        with db:
            db.execute(
                "UPDATE preferences SET value_json=? WHERE key='generation'", (_json(generation),)
            )
            if scope == "conversations":
                db.execute("DELETE FROM conversation_memory")
            cursor = db.execute(
                "DELETE FROM sessions"
                if scope == "conversations"
                else "DELETE FROM passive_archives"
            )
        return {
            "scope": scope,
            "deleted": cursor.rowcount,
            "generation": generation,
            "exported_copies_unchanged": True,
        }

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise StorageError("UNKNOWN_TABLE")
        return [
            {
                "name": r["name"],
                "type": r["type"],
                "nullable": not bool(r["notnull"] or r["pk"]),
                "primary_key": bool(r["pk"]),
            }
            for r in db.execute(f'PRAGMA table_info("{table}")')
        ]

    @staticmethod
    def _deadline(db: sqlite3.Connection) -> None:
        deadline = time.monotonic() + 0.5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

    async def catalog(self) -> dict[str, Any]:
        return dict(await self._call(self._catalog))

    def _catalog(self, db: sqlite3.Connection) -> dict[str, Any]:
        self._retain(db)
        self._deadline(db)
        stamp = utc_now()
        tables = [
            {
                "name": table,
                "columns": self._columns(db, table),
                "count": db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
                "counted_at": stamp,
            }
            for table in TABLES
        ]
        return {
            "tables": tables,
            "restricted": [
                {"name": "sqlite_schema", "reason": "Métadonnées système"},
                {"name": "credentials", "reason": "Secrets conservés hors base métier"},
            ],
            "reports": [],
        }

    async def rows(self, **query: Any) -> dict[str, Any]:
        return dict(await self._call(self._rows, query, 100))

    def _rows(self, db: sqlite3.Connection, query: dict[str, Any], maximum: int) -> dict[str, Any]:
        allowed = {"table", "limit", "offset", "sort", "direction", "q", "filters"}
        if not query.keys() <= allowed:
            raise StorageError("INVALID_QUERY")
        table = query.get("table", "")
        columns = self._columns(db, table)
        names = {c["name"]: c for c in columns}
        sort = query.get("sort") or next(c["name"] for c in columns if c["primary_key"])
        direction = query.get("direction", "asc")
        limit, offset = query.get("limit", 50), query.get("offset", 0)
        if (
            type(limit) is not int
            or type(offset) is not int
            or not 1 <= limit <= maximum
            or not 0 <= offset <= 1000000
            or sort not in names
            or direction not in ("asc", "desc")
        ):
            raise StorageError("INVALID_QUERY")
        filters = query.get("filters", [])
        if not isinstance(filters, list) or len(filters) > 8:
            raise StorageError("INVALID_FILTER")
        clauses: list[str] = []
        values: list[Any] = []
        ops = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
        for item in filters:
            if not isinstance(item, dict) or not item.keys() <= {"column", "op", "value"}:
                raise StorageError("INVALID_FILTER")
            col, op, value = item.get("column"), item.get("op"), item.get("value")
            if col not in names:
                raise StorageError("INVALID_FILTER")
            if op == "isnull":
                if type(value) is not bool:
                    raise StorageError("INVALID_FILTER")
                clauses.append(f'"{col}" IS ' + ("NULL" if value else "NOT NULL"))
                continue
            if (names[col]["type"] == "INTEGER" and type(value) is not int) or (
                names[col]["type"] == "TEXT" and not isinstance(value, str)
            ):
                raise StorageError("INVALID_FILTER_TYPE")
            if isinstance(value, str) and len(value) > 512:
                raise StorageError("INVALID_FILTER")
            if op == "contains" and names[col]["type"] == "TEXT":
                clauses.append(f"\"{col}\" LIKE ? ESCAPE '\\'")
                values.append("%" + self._like(str(value)) + "%")
            elif op in ops:
                clauses.append(f'"{col}" {ops[op]} ?')
                values.append(value)
            else:
                raise StorageError("INVALID_FILTER")
        search = query.get("q", "")
        if not isinstance(search, str) or len(search) > 256:
            raise StorageError("INVALID_QUERY")
        if search:
            text_names = [c["name"] for c in columns if c["type"] == "TEXT"]
            clauses.append(
                "(" + " OR ".join(f"\"{c}\" LIKE ? ESCAPE '\\'" for c in text_names) + ")"
            )
            values.extend(["%" + self._like(search) + "%"] * len(text_names))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        self._deadline(db)
        total = db.execute(f'SELECT COUNT(*) FROM "{table}"{where}', values).fetchone()[0]
        records = db.execute(
            f'SELECT * FROM "{table}"{where} ORDER BY "{sort}" {direction} LIMIT ? OFFSET ?',
            [*values, limit, offset],
        )
        rows = []
        size = 0
        for record in records:
            row = self._row_json(record)
            size += len(_json(row).encode())
            if size > 4 * 1024 * 1024:
                raise StorageError("QUERY_RESULT_TOO_LARGE_REDUCE_PAGE")
            rows.append(row)
        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": offset + len(rows) < total,
            "measured_at": utc_now(),
        }

    @staticmethod
    def _like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _row_json(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: json.loads(row[key])
            if key.endswith("_json") and row[key] is not None
            else row[key]
            for key in row.keys()
        }

    async def row(self, table: str, identifier: str) -> dict[str, Any]:
        return dict(await self._call(self._row, table, identifier))

    def _row(self, db: sqlite3.Connection, table: str, identifier: str) -> dict[str, Any]:
        columns = self._columns(db, table)
        pk = next(c["name"] for c in columns if c["primary_key"])
        if not isinstance(identifier, str) or len(identifier) > 256:
            raise StorageError("INVALID_IDENTIFIER")
        self._deadline(db)
        result = db.execute(f'SELECT * FROM "{table}" WHERE "{pk}"=?', (identifier,)).fetchone()
        return {"table": table, "row": self._row_json(result) if result else None}

    async def export(self, query: dict[str, Any], format: str) -> tuple[bytes, str]:
        if format not in ("csv", "json"):
            raise StorageError("INVALID_EXPORT_FORMAT")
        selected = dict(query)
        selected.setdefault("limit", 1000)
        data = redact(await self._call(self._rows, selected, 1000))
        if format == "json":
            return _json(data).encode(), "application/json"
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        names = [c["name"] for c in data["columns"]]
        writer.writerow(names)
        for row in data["rows"]:
            cells = []
            for name in names:
                value = row[name]
                cell = (
                    _json(value)
                    if isinstance(value, dict | list)
                    else ("NULL" if value is None else str(value))
                )
                if cell.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                    cell = "'" + cell
                cells.append(cell)
            writer.writerow(cells)
        return output.getvalue().encode("utf-8-sig"), "text/csv"

    async def backup(self) -> Path:
        return Path(await self._call(self._backup))

    def _backup(self, db: sqlite3.Connection) -> Path:
        directory = self.path.parent / "backups"
        if directory.is_symlink():
            raise StorageError("UNSAFE_BACKUP_PATH")
        directory.mkdir(mode=0o700, exist_ok=True)
        name = f"office-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.sqlite3"
        target = directory / name
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        dest = sqlite3.connect(target)
        try:
            deadline = time.monotonic() + 10

            def progress(status: int, remaining: int, total: int) -> None:
                if time.monotonic() > deadline:
                    raise StorageError("BACKUP_TIMEOUT")

            db.backup(dest, pages=256, progress=progress, sleep=0.01)
            self._validate_backup(dest)
        except Exception:
            dest.close()
            target.unlink(missing_ok=True)
            raise
        finally:
            dest.close()
        self._retain(db)
        return target

    @staticmethod
    def _validate_backup(
        db: sqlite3.Connection, *, versions: tuple[int, ...] = (SCHEMA_VERSION,)
    ) -> None:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchone() is not None
            or db.execute("PRAGMA user_version").fetchone()[0] not in versions
        ):
            raise StorageError("BACKUP_INVALID")

    @staticmethod
    def restore_isolated(source: Path, destination: Path) -> None:
        """Operator-only, never overwrite an existing database or restore through HTTP."""
        if source.is_symlink() or destination.exists() or destination.is_symlink():
            raise StorageError("UNSAFE_RESTORE_PATH")
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(destination)
        try:
            OfficeStore._validate_backup(src, versions=(1, SCHEMA_VERSION))
            source_version = int(src.execute("PRAGMA user_version").fetchone()[0])
            src.backup(dst)
        except Exception:
            dst.close()
            destination.unlink(missing_ok=True)
            raise
        finally:
            src.close()
            dst.close()
        try:
            if source_version < SCHEMA_VERSION:
                OfficeStore(destination)._initialize()
            with sqlite3.connect(destination) as restored:
                OfficeStore._validate_backup(restored)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
