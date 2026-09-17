"""Resolve SmartTube titles to real YouTube ids without driving its UI."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from jarvis_office.tv.protocol import QUERY_MAX, SEARCH_LIMIT_MAX, YOUTUBE_RE

SEARCH_TIMEOUT_S = 15.0
SEARCH_OUTPUT_MAX = 2 * 1024 * 1024
METADATA_OUTPUT_MAX = 512 * 1024


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


def _label(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = "".join(char for char in value.strip() if char.isprintable())
    return cleaned[:160] or fallback


def youtube_id_from_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    identifier = ""
    if host == "youtu.be":
        identifier = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            identifier = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
                identifier = parts[1]
    return identifier if YOUTUBE_RE.fullmatch(identifier) else None


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
            "title": _label(title, "Sans titre"),
            "content": {"kind": "youtube_video", "id": identifier},
        }
        clean_channel = _label(channel, "")[:120]
        if clean_channel:
            item["channel"] = clean_channel
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
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def parse_metadata(raw: bytes, video_id: str) -> dict[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise SmartTubeSearchError("Les métadonnées SmartTube sont invalides.") from exc
    if not isinstance(document, dict):
        raise SmartTubeSearchError("Les métadonnées SmartTube sont invalides.")
    duration = document.get("duration")
    duration_ms = None
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        seconds = float(duration)
        if math.isfinite(seconds) and 0 < seconds <= 86_400:
            duration_ms = round(seconds * 1000)
    return {
        "title": _label(document.get("title"), "Sans titre"),
        "content": {"kind": "youtube_video", "id": video_id},
        "duration_ms": duration_ms,
    }


async def metadata_smarttube(video_id: str) -> dict[str, Any]:
    binary = _binary()
    if binary is None:
        raise SmartTubeSearchError(
            "Les métadonnées SmartTube sont indisponibles sur le serveur (yt-dlp absent)."
        )
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--dump-single-json",
            "--no-warnings",
            "--no-call-home",
            "--no-playlist",
            "--skip-download",
            f"https://www.youtube.com/watch?v={video_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None
        raw = await asyncio.wait_for(process.stdout.read(METADATA_OUTPUT_MAX + 1), SEARCH_TIMEOUT_S)
        if len(raw) > METADATA_OUTPUT_MAX:
            raise SmartTubeSearchError("Les métadonnées SmartTube sont trop volumineuses.")
        returncode = await asyncio.wait_for(process.wait(), 1.0)
        if returncode != 0:
            raise SmartTubeSearchError("Impossible de lire les métadonnées SmartTube.")
        return parse_metadata(raw, video_id)
    except SmartTubeSearchError:
        raise
    except (FileNotFoundError, PermissionError):
        raise SmartTubeSearchError("Les métadonnées SmartTube sont indisponibles.") from None
    except (TimeoutError, OSError):
        raise SmartTubeSearchError("Les métadonnées SmartTube ont dépassé leur délai.") from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
