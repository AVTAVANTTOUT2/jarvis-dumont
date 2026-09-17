"""Resolve SmartTube titles to real YouTube ids without driving its UI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from jarvis_office.tv.protocol import QUERY_MAX, SEARCH_LIMIT_MAX, YOUTUBE_RE

SEARCH_TIMEOUT_S = 15.0
SEARCH_OUTPUT_MAX = 2 * 1024 * 1024


class SmartTubeSearchError(Exception):
    """A bounded, user-safe search failure."""

    def __init__(self, speech: str) -> None:
        self.speech = speech
        super().__init__(speech)


def _binary() -> str | None:
    candidates = [
        shutil.which("yt-dlp"),
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_entries(raw: bytes, *, limit: int) -> list[dict[str, Any]]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SmartTubeSearchError(
            "La recherche SmartTube a renvoyé une réponse invalide."
        ) from exc
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or YOUTUBE_RE.fullmatch(identifier) is None:
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        title = entry.get("title")
        channel = entry.get("channel") or entry.get("uploader")
        item: dict[str, Any] = {
            "title": title[:160] if isinstance(title, str) and title.strip() else "Sans titre",
            "content": {"kind": "youtube_video", "id": identifier},
        }
        if isinstance(channel, str) and channel.strip():
            item["channel"] = channel[:120]
        results.append(item)
        if len(results) >= limit:
            break
    return results


async def search_smarttube(query: str, limit: int = 5) -> list[dict[str, Any]]:
    query = query.strip()[:QUERY_MAX]
    limit = max(1, min(limit, SEARCH_LIMIT_MAX))
    if not query:
        return []
    binary = _binary()
    if binary is None:
        raise SmartTubeSearchError(
            "La recherche SmartTube est indisponible sur le serveur (yt-dlp absent)."
        )
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            "--no-call-home",
            "--skip-download",
            f"ytsearch{limit}:{query}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None
        raw = await asyncio.wait_for(process.stdout.read(SEARCH_OUTPUT_MAX + 1), SEARCH_TIMEOUT_S)
        if len(raw) > SEARCH_OUTPUT_MAX:
            raise SmartTubeSearchError("La réponse de recherche SmartTube est trop volumineuse.")
        returncode = await asyncio.wait_for(process.wait(), 1.0)
        if returncode != 0:
            raise SmartTubeSearchError("La recherche SmartTube a échoué. Réessayez.")
        return parse_entries(raw, limit=limit)
    except SmartTubeSearchError:
        raise
    except (FileNotFoundError, PermissionError):
        raise SmartTubeSearchError(
            "La recherche SmartTube est indisponible sur le serveur."
        ) from None
    except (TimeoutError, OSError):
        raise SmartTubeSearchError(
            "La recherche SmartTube a dépassé son délai. Réessayez."
        ) from None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
