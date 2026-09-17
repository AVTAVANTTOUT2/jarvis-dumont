"""TLS LAN /tv/v1 listener, pairing, bounded command queue and owner controls."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
import ssl
import tomllib
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from jarvis_office.storage import OfficeStore
from jarvis_office.tv.protocol import (
    COMMANDS_WAIT_S,
    EVENT_DEDUP,
    HEARTBEAT_MS,
    MAX_BODY,
    MUTATING,
    PAIRING_TTL_MS,
    QUEUE_MAX,
    SCHEMA,
    SHA256_RE,
    TvProtocolError,
    action_supported,
    build_command,
    digest,
    is_uuid,
    normalize_apps,
    now_ms,
    parse_json,
    playback_fresh,
    require_schema,
    sha256_der_from_pem,
    validate_command_result,
    validate_content,
    validate_event,
)
from jarvis_office.tv.wake import normalize_mac, wake
from jarvis_office.tv.youtube import SmartTubeSearchError, metadata_smarttube, youtube_id_from_url

COMMAND_HISTORY_MAX = 128
PLAYLIST_MAX = 50
PLAYLIST_TRACK_MAX = 50
PLAYLIST_END_TOLERANCE_MS = 1000


def _registry_default() -> dict[str, Any]:
    return {
        "enabled": False,
        "default_video_app": "smarttube",
        "default_film_app": "avt",
        "devices": {},
        "playlists": [],
    }


def _clean_label(value: object, fallback: str, limit: int) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = "".join(char for char in value.strip() if char.isprintable())
    return cleaned[:limit] or fallback


def _normalize_playlists(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    playlists: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:PLAYLIST_MAX]:
        if not isinstance(item, dict) or not is_uuid(item.get("id")):
            continue
        playlist_id = str(item["id"])
        if playlist_id in seen:
            continue
        seen.add(playlist_id)
        tracks: list[dict[str, Any]] = []
        track_ids: set[str] = set()
        raw_tracks = item.get("tracks")
        if not isinstance(raw_tracks, list):
            raw_tracks = []
        for track in raw_tracks[:PLAYLIST_TRACK_MAX]:
            if not isinstance(track, dict) or not is_uuid(track.get("id")):
                continue
            track_id = str(track["id"])
            if track_id in track_ids:
                continue
            try:
                content = validate_content("smarttube", track.get("content"))
            except TvProtocolError:
                continue
            duration = track.get("duration_ms")
            duration_ms = duration if type(duration) is int and 0 < duration <= 86_400_000 else None
            track_ids.add(track_id)
            tracks.append(
                {
                    "id": track_id,
                    "title": _clean_label(track.get("title"), content["id"], 160),
                    "content": content,
                    "duration_ms": duration_ms,
                }
            )
        playlists.append(
            {
                "id": playlist_id,
                "name": _clean_label(item.get("name"), f"Playlist {len(playlists) + 1}", 120),
                "tracks": tracks,
            }
        )
    return playlists


def _playback_id_of(playback: object) -> str | None:
    if not isinstance(playback, dict):
        return None
    playback_id = playback.get("playback_id")
    return playback_id if isinstance(playback_id, str) and playback_id else None


def _playlist_reached_end(
    playback: object,
    *,
    duration_ms: object = None,
    high_position_ms: object = None,
) -> bool:
    if not isinstance(playback, dict):
        return False
    state = playback.get("state")
    if state == "paused":
        return False
    if state == "ended":
        return True
    duration = playback.get("duration_ms")
    if type(duration) is not int or duration <= 0:
        if state == "playing":
            return False
        duration = duration_ms if type(duration_ms) is int and duration_ms > 0 else None
    if type(duration) is not int or duration <= 0:
        return False
    position = playback.get("position_ms")
    if type(position) is not int:
        position = None
    if type(high_position_ms) is int:
        position = high_position_ms if position is None else max(position, high_position_ms)
    if type(position) is not int:
        return False
    if state == "stopped":
        return position >= max(0, duration - PLAYLIST_END_TOLERANCE_MS)
    live_position = playback.get("position_ms")
    return state == "playing" and type(live_position) is int and live_position >= duration


@dataclass(frozen=True)
class TvSettings:
    bind: str
    port: int
    cert: str
    key: str
    avt_allowed_cert_sha256: tuple[str, ...] = ()
    wake_mac: str | None = None
    wake_broadcast: str = "255.255.255.255"
    wake_timeout_s: float = 20.0

    def validate(self) -> None:
        address = ipaddress.ip_address(self.bind)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("EXPLICIT_TV_BIND_REQUIRED")
        if not (self.port == 0 or 1024 <= self.port <= 65535) or self.port == 8768:
            raise ValueError("INVALID_TV_PORT")
        if not self.cert or not self.key:
            raise ValueError("TLS_CERTIFICATE_REQUIRED")
        cert, key = Path(self.cert), Path(self.key)
        if not cert.is_file() or cert.is_symlink() or not key.is_file() or key.is_symlink():
            raise ValueError("TLS_CERTIFICATE_REQUIRED")
        if key.stat().st_mode & 0o077:
            raise ValueError("PRIVATE_CONFIG_PERMISSIONS_REQUIRED")
        for item in self.avt_allowed_cert_sha256:
            if not SHA256_RE.fullmatch(item):
                raise ValueError("INVALID_AVT_FINGERPRINT")
        if self.wake_mac is not None:
            normalize_mac(self.wake_mac)
        broadcast = ipaddress.ip_address(self.wake_broadcast)
        if broadcast.version != 4:
            raise ValueError("INVALID_TV_BROADCAST")
        if not 5.0 <= self.wake_timeout_s <= 60.0:
            raise ValueError("INVALID_TV_WAKE_TIMEOUT")

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        echo_bind: str,
        echo_port: int,
        echo_cert: str,
        echo_key: str,
    ) -> TvSettings | None:
        if path.stat().st_mode & 0o077:
            raise ValueError("PRIVATE_CONFIG_PERMISSIONS_REQUIRED")
        data = tomllib.loads(path.read_text())
        raw = data.get("tv")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("INVALID_TV_CONFIGURATION")
        allowed = {
            "bind",
            "port",
            "cert",
            "key",
            "avt_allowed_cert_sha256",
            "wake_mac",
            "wake_broadcast",
            "wake_timeout_s",
        }
        if set(raw) - allowed:
            raise ValueError("INVALID_TV_CONFIGURATION")
        if "port" not in raw:
            raise ValueError("INVALID_TV_PORT")
        fingerprints = raw.get("avt_allowed_cert_sha256", [])
        if not isinstance(fingerprints, list) or not all(
            isinstance(item, str) for item in fingerprints
        ):
            raise ValueError("INVALID_AVT_FINGERPRINT")
        settings = cls(
            bind=str(raw.get("bind") or echo_bind),
            port=int(raw["port"]),
            cert=str(raw.get("cert") or echo_cert),
            key=str(raw.get("key") or echo_key),
            avt_allowed_cert_sha256=tuple(
                item.replace(":", "").replace(" ", "").lower() for item in fingerprints
            ),
            wake_mac=(str(raw["wake_mac"]) if raw.get("wake_mac") else None),
            wake_broadcast=str(raw.get("wake_broadcast") or "255.255.255.255"),
            wake_timeout_s=float(raw.get("wake_timeout_s", 20.0)),
        )
        if settings.port == echo_port:
            raise ValueError("INVALID_TV_PORT")
        settings.validate()
        return settings


class _Device:
    def __init__(self, device_id: str, credential_sha256: str) -> None:
        self.device_id = device_id
        self.credential_sha256 = credential_sha256
        self.revoked = False
        self.server_epoch: str | None = None
        self.connection_id: str | None = None
        self.instance_id: str | None = None
        self.apk_version: str | None = None
        self.connected = False
        self.last_poll_ms = 0
        self.apps: dict[str, Any] = normalize_apps({})
        self.playback: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.queue: deque[dict[str, Any]] = deque()
        self.pending: dict[str, dict[str, Any]] = {}
        self.events: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.waiter: asyncio.Future[bool] | None = None
        self.changed = asyncio.Event()


class TvHub:
    def __init__(
        self,
        settings: TvSettings,
        store: OfficeStore,
        *,
        chat: Any = None,
        on_change: Callable[[], None] | None = None,
        now: Callable[[], int] | None = None,
    ) -> None:
        settings.validate()
        self.settings, self.store, self.chat = settings, store, chat
        self.on_change = on_change
        self.now = now or now_ms
        self._enabled_flag = False
        self._video_app = "smarttube"
        self._film_app = "avt"
        self.devices: dict[str, _Device] = {}
        self.pairing: dict[str, Any] | None = None
        self.candidates: list[dict[str, Any]] = []
        self.candidate_scope: tuple[str, str, str] | None = None
        self.playlists: list[dict[str, Any]] = []
        self._playlist_state: dict[str, Any] | None = None
        self._turns: set[str] = set()
        self.runner: web.AppRunner | None = None
        self.port = settings.port
        self.ca_pem = Path(settings.cert).read_text()
        if "BEGIN CERTIFICATE" not in self.ca_pem:
            raise ValueError("TLS_CERTIFICATE_REQUIRED")
        self.cert_sha256 = sha256_der_from_pem(self.ca_pem)
        self.app = web.Application(middlewares=[self._guard], client_max_size=MAX_BODY)
        self.app.add_routes(
            [
                web.post("/tv/v1/pair", self._pair),
                web.post("/tv/v1/session", self._session),
                web.get("/tv/v1/commands", self._commands),
                web.post("/tv/v1/events", self._events),
            ]
        )

    @property
    def endpoint(self) -> str:
        return f"https://{self.settings.bind}:{self.port}"

    async def start(self) -> None:
        await self._load_registry()
        if self.runner is not None:
            return
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(self.settings.cert, self.settings.key)
        self.runner = web.AppRunner(
            self.app,
            access_log=None,
            shutdown_timeout=10,
            keepalive_timeout=30,
            max_line_size=4096,
            max_field_size=4096,
            lingering_time=1,
        )
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, self.settings.bind, self.settings.port, ssl_context=tls)
            await site.start()
            self.port = self.runner.addresses[0][1]
        except Exception:
            await self.runner.cleanup()
            self.runner = None
            raise RuntimeError("TV_START_FAILED") from None

    async def close(self) -> None:
        self._playlist_state = None
        for device in self.devices.values():
            self._disconnect(device, replay=False)
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    def snapshot(self) -> dict[str, Any]:
        devices = []
        stamp = self.now()
        for device in self.devices.values():
            if device.revoked:
                continue
            stale = device.connected and stamp - device.last_poll_ms > HEARTBEAT_MS
            if stale:
                device.connected = False
            playback = playback_fresh(device.playback, stamp) if device.connected else None
            devices.append(
                {
                    "device_id": device.device_id,
                    "connection": "CONNECTED" if device.connected else "DISCONNECTED",
                    "apk_version": device.apk_version,
                    "capabilities": {"apps": device.apps},
                    "playback": playback,
                    "last_result": device.last_result,
                    "queue_depth": len(device.queue),
                }
            )
        return {
            "configured": True,
            "enabled": self._enabled(),
            "endpoint_configured": True,
            "bind": self.settings.bind,
            "port": self.port,
            "wake_configured": self.settings.wake_mac is not None,
            "cert_sha256": self.cert_sha256,
            "pairing_pending": self.pairing is not None and self.pairing["expires_at_ms"] > stamp,
            "defaults": {
                "video": self._defaults()["default_video_app"],
                "film": self._defaults()["default_film_app"],
            },
            "playlists": self.playlists,
            "playlist_state": self._public_playlist_state(),
            "devices": devices,
        }

    def cancel_turn(self, turn_id: str) -> None:
        self._turns.discard(turn_id)

    def clear_dialogue(self) -> None:
        self.candidates = []
        self.candidate_scope = None
        self._turns.clear()

    async def handle_addressed(self, question: str, turn_id: str) -> str | None:
        from jarvis_office.tv.intent import dispatch

        self._turns.add(turn_id)
        try:
            return await dispatch(self, question, turn_id)
        finally:
            self._turns.discard(turn_id)

    def turn_alive(self, turn_id: str) -> bool:
        return turn_id in self._turns

    async def owner_command(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        if isinstance(action, str) and action.startswith("playlist_"):
            return await self._playlist_command(body)
        if action == "enable":
            await self._set_enabled(True)
            return {"status": "applied", "error": None}
        if action == "disable":
            await self._set_enabled(False)
            self._playlist_state = None
            for device in self.devices.values():
                self._disconnect(device, replay=False)
            return {"status": "applied", "error": None}
        if action == "defaults":
            video, film = body.get("video"), body.get("film")
            if video not in {"smarttube", "avt"} or film not in {"smarttube", "avt"}:
                return {"status": "rejected", "error": "INVALID_REQUEST"}
            registry = await self._registry()
            registry["default_video_app"] = video
            registry["default_film_app"] = film
            await self._save_registry(registry)
            return {"status": "applied", "error": None}
        if action == "pair":
            if body.get("confirm") is not True:
                return {"status": "rejected", "error": "CONFIRMATION_REQUIRED"}
            if not self._enabled():
                return {"status": "rejected", "error": "TV_DISABLED"}
            document = self._create_pairing()
            return {"status": "applied", "error": None, "document": document}
        if action == "revoke":
            if body.get("confirm") is not True:
                return {"status": "rejected", "error": "CONFIRMATION_REQUIRED"}
            device_id = body.get("device_id")
            if not is_uuid(device_id):
                return {"status": "rejected", "error": "INVALID_REQUEST"}
            await self._revoke(str(device_id))
            return {"status": "applied", "error": None}
        return {"status": "rejected", "error": "INVALID_COMMAND"}

    async def _playlist_command(self, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("action")
        if action == "playlist_create":
            name = _clean_label(body.get("name"), "", 120)
            if not name:
                return {"status": "rejected", "error": "INVALID_REQUEST"}
            registry = await self._registry()
            if len(registry["playlists"]) >= PLAYLIST_MAX:
                return {"status": "rejected", "error": "PLAYLIST_LIMIT"}
            created = {"id": str(uuid.uuid4()), "name": name, "tracks": []}
            registry["playlists"].append(created)
            await self._save_registry(registry)
            return {"status": "applied", "error": None, "playlist": created}
        playlist_id = body.get("playlist_id")
        if not isinstance(playlist_id, str) or not is_uuid(playlist_id):
            return {"status": "rejected", "error": "PLAYLIST_NOT_FOUND"}
        playlist: dict[str, Any] | None = next(
            (item for item in self.playlists if item["id"] == playlist_id), None
        )
        if playlist is None:
            return {"status": "rejected", "error": "PLAYLIST_NOT_FOUND"}
        if action == "playlist_add":
            if len(playlist["tracks"]) >= PLAYLIST_TRACK_MAX:
                return {"status": "rejected", "error": "PLAYLIST_LIMIT"}
            video_id = youtube_id_from_url(body.get("url"))
            if video_id is None:
                return {"status": "rejected", "error": "INVALID_URL"}
            try:
                metadata = await metadata_smarttube(video_id)
            except SmartTubeSearchError:
                return {"status": "rejected", "error": "METADATA_UNAVAILABLE"}
            registry = await self._registry()
            target = next(
                (item for item in registry["playlists"] if item["id"] == playlist_id), None
            )
            if target is None:
                return {"status": "rejected", "error": "PLAYLIST_NOT_FOUND"}
            if len(target["tracks"]) >= PLAYLIST_TRACK_MAX:
                return {"status": "rejected", "error": "PLAYLIST_LIMIT"}
            track = {
                "id": str(uuid.uuid4()),
                "title": metadata["title"],
                "content": metadata["content"],
                "duration_ms": metadata["duration_ms"],
            }
            target["tracks"].append(track)
            await self._save_registry(registry)
            return {"status": "applied", "error": None, "track": track}
        if action == "playlist_remove":
            track_id = body.get("track_id")
            if not isinstance(track_id, str):
                return {"status": "rejected", "error": "INVALID_REQUEST"}
            remaining = [track for track in playlist["tracks"] if track["id"] != track_id]
            if len(remaining) == len(playlist["tracks"]):
                return {"status": "rejected", "error": "TRACK_NOT_FOUND"}
            playlist["tracks"] = remaining
            registry = await self._registry()
            target = next(
                (item for item in registry["playlists"] if item["id"] == playlist_id), None
            )
            if target is None:
                return {"status": "rejected", "error": "PLAYLIST_NOT_FOUND"}
            target["tracks"] = remaining
            await self._save_registry(registry)
            if self._playlist_state and self._playlist_state["playlist_id"] == playlist_id:
                self._playlist_state.update(status="interrupted", error="PLAYLIST_CHANGED")
                self._bump()
            return {"status": "applied", "error": None}
        if action == "playlist_delete":
            if body.get("confirm") is not True:
                return {"status": "rejected", "error": "CONFIRMATION_REQUIRED"}
            registry = await self._registry()
            registry["playlists"] = [
                item for item in registry["playlists"] if item["id"] != playlist_id
            ]
            await self._save_registry(registry)
            if self._playlist_state and self._playlist_state["playlist_id"] == playlist_id:
                self._playlist_state = None
                self._bump()
            return {"status": "applied", "error": None}
        if action == "playlist_play":
            return await self._start_playlist(playlist_id)
        return {"status": "rejected", "error": "INVALID_COMMAND"}

    async def start_playlist(self, index: int) -> dict[str, Any]:
        if type(index) is not int or index < 0 or index >= len(self.playlists):
            return {"status": "rejected", "error": "PLAYLIST_NOT_FOUND"}
        return await self._start_playlist(self.playlists[index]["id"])

    async def next_playlist(self) -> dict[str, Any]:
        state = self._playlist_state
        if state is None or state.get("status") != "playing":
            if state and state.get("status") == "completed":
                return {"status": "completed", "error": None}
            return {"status": "rejected", "error": "PLAYLIST_NOT_RUNNING"}
        device = self._connected_device()
        if device is None:
            return {"status": "rejected", "error": "DISCONNECTED"}
        command = await self._advance_playlist(device, manual=True)
        if command is None:
            if self._playlist_state and self._playlist_state.get("status") == "error":
                return {"status": "rejected", "error": self._playlist_state.get("error")}
            return {"status": "completed", "error": None}
        track = self._current_playlist_track()
        return {
            "status": "applied",
            "error": None,
            "command_id": command["command_id"],
            "title": track.get("title") if track else None,
        }

    async def _start_playlist(self, playlist_id: str) -> dict[str, Any]:
        playlist = next((item for item in self.playlists if item["id"] == playlist_id), None)
        if playlist is None:
            return {"status": "rejected", "error": "PLAYLIST_NOT_FOUND"}
        if not playlist["tracks"]:
            return {"status": "rejected", "error": "PLAYLIST_EMPTY"}
        device = self._connected_device()
        if device is None:
            return {"status": "rejected", "error": "DISCONNECTED"}
        if self._playlist_state is not None:
            self._retire_playlist_command(device, self._playlist_state, "REPLACED")
        state = {
            "playlist_id": playlist_id,
            "track_index": 0,
            "device_id": device.device_id,
            "status": "starting",
            "started": False,
            "command_id": None,
            "expected_playback_id": None,
            "ignored_playback_id": _playback_id_of(device.playback),
            "high_position_ms": 0,
        }
        self._playlist_state = state
        try:
            command = await self.issue(
                app="smarttube",
                action="play_content",
                args={"content": playlist["tracks"][0]["content"]},
                ttl_ms=30000,
            )
        except TvProtocolError as exc:
            state.update(status="error", error=exc.error_code)
            self._bump()
            return {"status": "rejected", "error": exc.error_code}
        state.update(status="playing", command_id=command["command_id"])
        self._bump()
        return {
            "status": "applied",
            "error": None,
            "command_id": command["command_id"],
            "playlist": playlist,
        }

    def _current_playlist_track(self) -> dict[str, Any] | None:
        state = self._playlist_state
        if state is None:
            return None
        playlist: dict[str, Any] | None = next(
            (item for item in self.playlists if item["id"] == state["playlist_id"]), None
        )
        index = state.get("track_index")
        if playlist is None or type(index) is not int or not 0 <= index < len(playlist["tracks"]):
            return None
        track = playlist["tracks"][index]
        return track if isinstance(track, dict) else None

    def _public_playlist_state(self) -> dict[str, Any] | None:
        state = self._playlist_state
        track = self._current_playlist_track()
        if state is None:
            return None
        playlist: dict[str, Any] | None = next(
            (item for item in self.playlists if item["id"] == state["playlist_id"]), None
        )
        if playlist is None:
            return None
        index = state.get("track_index", 0)
        return {
            "playlist_id": playlist["id"],
            "playlist_number": self.playlists.index(playlist) + 1,
            "track_number": index + 1,
            "track_total": len(playlist["tracks"]),
            "status": state.get("status"),
            "track": {
                "title": track["title"],
                "duration_ms": track["duration_ms"],
            }
            if track
            else None,
        }

    def _retire_playlist_command(self, device: _Device, state: dict[str, Any], reason: str) -> None:
        command_id = state.get("command_id")
        record = device.pending.get(command_id) if isinstance(command_id, str) else None
        if record is None or record["status"] != "accepted":
            return
        device.queue = deque(item for item in device.queue if item["command_id"] != command_id)
        record.update(status="unknown", error_code=reason)

    def _complete_playlist_command(
        self, device: _Device, state: dict[str, Any], playback_id: str
    ) -> None:
        command_id = state.get("command_id")
        record = device.pending.get(command_id) if isinstance(command_id, str) else None
        if record is None or record["status"] not in {"accepted", "dispatched"}:
            return
        result = record.get("result")
        result = dict(result) if isinstance(result, dict) else {}
        result["playback_id"] = playback_id
        record.update(status="completed", error_code=None, result=result)
        device.last_result = {
            "command_id": command_id,
            "action": record["command"]["action"],
            "app": record["command"]["app"],
            "status": "completed",
            "error_code": None,
        }

    def _bind_playlist_identity(self, state: dict[str, Any], playback: dict[str, Any]) -> None:
        if isinstance(state.get("expected_playback_id"), str) and state["expected_playback_id"]:
            return
        track = self._current_playlist_track()
        playback_id = _playback_id_of(playback)
        if (
            track
            and playback_id
            and playback.get("app") == "smarttube"
            and playback.get("content") == track["content"]
            and playback_id != state.get("ignored_playback_id")
        ):
            state["expected_playback_id"] = playback_id

    def _playlist_playback_matches(
        self, device: _Device, state: dict[str, Any], playback: dict[str, Any]
    ) -> bool:
        track = self._current_playlist_track()
        expected = state.get("expected_playback_id")
        return bool(
            track
            and isinstance(expected, str)
            and expected
            and playback.get("app") == "smarttube"
            and playback.get("content") == track["content"]
            and playback.get("playback_id") == expected
            and state.get("device_id") == device.device_id
        )

    def _playlist_track_ended(self, playback: object) -> bool:
        track = self._current_playlist_track()
        state = self._playlist_state or {}
        return _playlist_reached_end(
            playback,
            duration_ms=track.get("duration_ms") if track else None,
            high_position_ms=state.get("high_position_ms"),
        )

    async def _observe_playlist(self, device: _Device) -> None:
        # ponytail: needs a fresh playback_id plus ended/position; silent MediaSession
        # only moves forward via oral « suivante ». Wall-clock timer if that stays true.
        state = self._playlist_state
        if state is None or state.get("status") != "playing":
            return
        playback = playback_fresh(device.playback, self.now())
        if playback is None or state.get("device_id") != device.device_id:
            return
        self._bind_playlist_identity(state, playback)
        matches = self._playlist_playback_matches(device, state, playback)
        if not matches:
            if (
                state.get("started")
                and playback.get("app") == "smarttube"
                and isinstance(playback.get("content"), dict)
            ):
                state.update(status="interrupted", error="PLAYBACK_CHANGED")
                self._bump()
            return
        if not state.get("started"):
            state["started"] = True
            self._bump()
        position = playback.get("position_ms")
        if type(position) is int and position > (state.get("high_position_ms") or 0):
            state["high_position_ms"] = position
        if not self._playlist_track_ended(playback):
            return
        await self._advance_playlist(device)

    async def _advance_playlist(
        self, device: _Device, *, manual: bool = False
    ) -> dict[str, Any] | None:
        state = self._playlist_state
        if state is None or state.get("status") != "playing" or state.get("advancing"):
            return None
        state["advancing"] = True
        try:
            if state.get("device_id") != device.device_id:
                return None
            if not manual and not self._playlist_track_ended(device.playback):
                return None
            if manual:
                self._retire_playlist_command(device, state, "SKIPPED")
            else:
                playback_id = (
                    device.playback.get("playback_id")
                    if isinstance(device.playback, dict)
                    else None
                )
                if isinstance(playback_id, str):
                    self._complete_playlist_command(device, state, playback_id)
            next_index = state["track_index"] + 1
            playlist: dict[str, Any] | None = next(
                (item for item in self.playlists if item["id"] == state["playlist_id"]), None
            )
            if playlist is None or next_index >= len(playlist["tracks"]):
                state.update(status="completed", command_id=None)
                self._bump()
                return None
            track = playlist["tracks"][next_index]
            state.update(
                track_index=next_index,
                status="starting",
                started=False,
                command_id=None,
                expected_playback_id=None,
                ended_playback_id=None,
                ignored_playback_id=_playback_id_of(device.playback),
                high_position_ms=0,
            )
            self._bump()
            try:
                command = await self.issue(
                    app="smarttube",
                    action="play_content",
                    args={"content": track["content"]},
                    ttl_ms=30000,
                )
            except TvProtocolError as exc:
                state.update(status="error", error=exc.error_code)
                self._bump()
                return None
            state.update(status="playing", command_id=command["command_id"])
            self._bump()
            return command
        finally:
            state["advancing"] = False

    async def issue(
        self,
        *,
        app: str,
        action: str,
        args: dict[str, Any],
        ttl_ms: int = 10000,
    ) -> dict[str, Any]:
        device = self._connected_device()
        if device is None or device.server_epoch is None or device.connection_id is None:
            raise TvProtocolError("rejected", "DISCONNECTED")
        missing = action_supported(device.apps, app, action)
        if missing:
            raise TvProtocolError("rejected", missing)
        if app == "avt" and action == "play_content" and "profile_id" not in args:
            candidates = self.active_candidates()
            if (
                self.candidate_scope
                and self.candidate_scope[2]
                and any(item.get("content") == args.get("content") for item in candidates)
            ):
                args = dict(args, profile_id=self.candidate_scope[2])
        stamp = self.now()
        for record in device.pending.values():
            if record["status"] == "accepted" and record["command"]["expires_at_ms"] < stamp:
                record.update(status="expired", error_code="EXPIRED")
        device.queue = deque(
            item
            for item in device.queue
            if device.pending[item["command_id"]]["status"] == "accepted"
        )
        if sum(record["status"] == "accepted" for record in device.pending.values()) >= QUEUE_MAX:
            raise TvProtocolError("rejected", "BUSY")
        for command_id in list(device.pending):
            if len(device.pending) < COMMAND_HISTORY_MAX:
                break
            if device.pending[command_id]["status"] != "accepted":
                del device.pending[command_id]
        command = build_command(
            device_id=device.device_id,
            server_epoch=device.server_epoch,
            connection_id=device.connection_id,
            app=app,
            action=action,
            args=args,
            ttl_ms=ttl_ms,
            issued_at_ms=stamp,
        )
        if len(device.queue) >= QUEUE_MAX:
            raise TvProtocolError("rejected", "BUSY")
        if action in MUTATING and any(
            record["status"] == "accepted" and record["command"]["action"] in MUTATING
            for record in device.pending.values()
        ):
            raise TvProtocolError("rejected", "BUSY")
        device.pending[command["command_id"]] = {
            "command": command,
            "status": "accepted",
            "error_code": None,
            "taken": False,
        }
        device.queue.append(command)
        self._notify(device)
        self._bump()
        return command

    async def wait_result(self, command_id: str, turn_id: str, timeout_s: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            device = self._device_for_command(command_id)
            if not self.turn_alive(turn_id):
                self._abandon(command_id, "unknown", "DISCONNECTED")
                return {"status": "unknown", "error_code": "DISCONNECTED", "result": None}
            if device is None:
                return {"status": "unknown", "error_code": "DISCONNECTED", "result": None}
            record = device.pending.get(command_id)
            playback = playback_fresh(device.playback, self.now())
            if record is not None and record["command"]["action"] == "play_content":
                if not self._playback_matches(device, record["command"]):
                    playback = None
            if record is not None and record["status"] != "accepted":
                status = record["status"]
                play_waiting = (
                    status == "dispatched"
                    and record["command"]["action"] == "play_content"
                    and playback is None
                    and asyncio.get_running_loop().time() < deadline
                )
                if not play_waiting:
                    return {
                        "status": status,
                        "error_code": record.get("error_code"),
                        "result": record.get("result"),
                        "playback": playback,
                    }
            if asyncio.get_running_loop().time() >= deadline:
                if record is not None and record["status"] == "dispatched":
                    return {
                        "status": "dispatched",
                        "error_code": None,
                        "result": record.get("result"),
                        "playback": playback,
                    }
                self._abandon(command_id, "unknown", "EXPIRED")
                return {
                    "status": "unknown",
                    "error_code": "EXPIRED",
                    "result": None,
                    "playback": playback,
                }
            device.changed.clear()
            try:
                await asyncio.wait_for(
                    device.changed.wait(),
                    timeout=max(0.05, deadline - asyncio.get_running_loop().time()),
                )
            except TimeoutError:
                continue

    def defaults(self) -> dict[str, str]:
        return self._defaults()

    def connected(self) -> _Device | None:
        return self._connected_device()

    async def prepare(self, *, app: str, turn_id: str) -> None:
        device = self.connected()
        if device is None:
            if not self.settings.wake_mac:
                raise TvProtocolError("rejected", "WAKE_UNCONFIGURED")
            try:
                await wake(self.settings.wake_mac, self.settings.wake_broadcast)
            except (OSError, ValueError):
                raise TvProtocolError("rejected", "WAKE_UNAVAILABLE") from None
            deadline = asyncio.get_running_loop().time() + self.settings.wake_timeout_s
            while device is None and asyncio.get_running_loop().time() < deadline:
                if not self.turn_alive(turn_id):
                    raise TvProtocolError("unknown", "DISCONNECTED")
                await asyncio.sleep(0.25)
                device = self.connected()
            if device is None:
                raise TvProtocolError("rejected", "WAKE_UNAVAILABLE")

        state_missing = action_supported(device.apps, app, "get_state")
        if state_missing:
            raise TvProtocolError("rejected", state_missing)
        state_command = await self.issue(app=app, action="get_state", args={}, ttl_ms=5000)
        state = await self.wait_result(state_command["command_id"], turn_id, 6.0)
        if state.get("status") != "completed" or state.get("error_code"):
            raise TvProtocolError("rejected", state.get("error_code") or "TV_NOT_READY")

        home_missing = action_supported(device.apps, app, "home")
        if home_missing:
            raise TvProtocolError("rejected", home_missing)
        home_command = await self.issue(app=app, action="home", args={}, ttl_ms=5000)
        home = await self.wait_result(home_command["command_id"], turn_id, 6.0)
        if home.get("status") not in {"completed", "dispatched"} or home.get("error_code"):
            raise TvProtocolError("rejected", home.get("error_code") or "TV_HOME_UNAVAILABLE")

    def remember_candidates(self, items: list[dict[str, Any]], profile_id: str | None) -> None:
        device = self._connected_device()
        if device is None or device.server_epoch is None:
            self.candidates = []
            self.candidate_scope = None
            return
        self.candidates = items[:10]
        self.candidate_scope = (device.device_id, device.server_epoch, profile_id or "")

    def active_candidates(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        device = self._connected_device()
        if device is None or device.server_epoch is None or self.candidate_scope is None:
            self.candidates = []
            return []
        scoped_device, scoped_epoch, scoped_profile = self.candidate_scope
        if scoped_device != device.device_id or scoped_epoch != device.server_epoch:
            self.candidates = []
            self.candidate_scope = None
            return []
        if profile_id is not None and profile_id != scoped_profile:
            self.candidates = []
            return []
        return list(self.candidates)

    def invalidate_candidates(self) -> None:
        self.candidates = []
        self.candidate_scope = None

    @web.middleware
    async def _guard(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Any],
    ) -> web.StreamResponse:
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            host = request.headers.get("Host", "")
            expected = {f"{self.settings.bind}:{self.port}", self.settings.bind}
            if (
                not peer
                or host not in expected
                or any(
                    name in request.headers
                    for name in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")
                )
            ):
                return self._error("UNAUTHORIZED", 403)
            if request.content_length is not None and request.content_length > MAX_BODY:
                return self._error("UNAUTHORIZED", 413)
            async with asyncio.timeout(COMMANDS_WAIT_S + 5):
                result = await handler(request)
            if not isinstance(result, web.StreamResponse):
                return self._error("INTERNAL_ERROR", 500)
            return result
        except TvProtocolError as exc:
            return self._error(exc.error_code, exc.http)
        except TimeoutError:
            return self._error("DISCONNECTED", 504)
        except Exception:
            return self._error("INTERNAL_ERROR", 500)

    async def _pair(self, request: web.Request) -> web.Response:
        body = parse_json(await request.read())
        require_schema(body)
        pairing = self.pairing
        stamp = self.now()
        if (
            pairing is None
            or not self._enabled()
            or pairing["expires_at_ms"] <= stamp
            or body.get("pairing_id") != pairing["pairing_id"]
            or not isinstance(body.get("code"), str)
            or not hmac.compare_digest(digest(body["code"]), pairing["code_sha256"])
        ):
            raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
        self.pairing = None
        credential = secrets.token_urlsafe(32)
        device_id = str(uuid.uuid4())
        registry = await self._registry()
        if len([item for item in registry["devices"].values() if not item.get("revoked")]) >= 4:
            raise TvProtocolError("rejected", "BUSY")
        registry["devices"][device_id] = {
            "credential_sha256": digest(credential),
            "paired_at": stamp,
            "revoked": False,
        }
        await self._save_registry(registry)
        self.devices[device_id] = _Device(device_id, digest(credential))
        self._bump()
        return web.json_response(
            {"schema_version": SCHEMA, "device_id": device_id, "credential": credential}
        )

    async def _session(self, request: web.Request) -> web.Response:
        device = await self._authenticate(request)
        body = parse_json(await request.read())
        require_schema(body)
        if body.get("device_id") != device.device_id:
            raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
        instance = body.get("client_instance_id")
        version = body.get("apk_version")
        if (
            not is_uuid(instance)
            or not isinstance(version, str)
            or not version
            or len(version) > 64
        ):
            raise TvProtocolError("rejected", "UNSUPPORTED")
        self._disconnect(device, replay=False)
        device.server_epoch = str(uuid.uuid4())
        device.connection_id = str(uuid.uuid4())
        device.instance_id = instance
        device.apk_version = version
        device.connected = True
        device.last_poll_ms = self.now()
        device.apps = normalize_apps(body.get("capabilities"))
        device.playback = None
        self.invalidate_candidates()
        self._bump()
        return web.json_response(
            {
                "schema_version": SCHEMA,
                "device_id": device.device_id,
                "server_epoch": device.server_epoch,
                "connection_id": device.connection_id,
                "server_time_ms": self.now(),
                "heartbeat_timeout_ms": HEARTBEAT_MS,
            }
        )

    async def _commands(self, request: web.Request) -> web.Response:
        device = await self._authenticate(request)
        connection_id = request.query.get("connection_id", "")
        if any(key in request.query for key in ("credential", "token", "authorization", "code")):
            raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
        if connection_id != device.connection_id or not device.connected:
            raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
        if device.waiter is not None and not device.waiter.done():
            raise TvProtocolError("rejected", "BUSY")
        device.last_poll_ms = self.now()
        command = self._take(device)
        if command is None:
            loop = asyncio.get_running_loop()
            device.waiter = loop.create_future()
            try:
                await asyncio.wait_for(asyncio.shield(device.waiter), timeout=COMMANDS_WAIT_S)
            except TimeoutError:
                pass
            finally:
                if device.waiter is not None and not device.waiter.done():
                    device.waiter.cancel()
                device.waiter = None
            command = self._take(device)
        device.last_poll_ms = self.now()
        return web.json_response(
            {
                "schema_version": SCHEMA,
                "server_epoch": device.server_epoch,
                "connection_id": device.connection_id,
                "command": command,
            }
        )

    async def _events(self, request: web.Request) -> web.Response:
        device = await self._authenticate(request)
        body = parse_json(await request.read())
        if not device.connected or device.server_epoch is None or device.connection_id is None:
            raise TvProtocolError("rejected", "DISCONNECTED", 409)
        event = validate_event(
            body,
            device_id=device.device_id,
            server_epoch=device.server_epoch,
            connection_id=device.connection_id,
        )
        event_id = event["event_id"]
        if event_id in device.events:
            return web.json_response({"event_id": event_id})
        kind, payload = event["kind"], event["payload"]
        if kind == "command_result":
            result = validate_command_result(payload)
            record = device.pending.get(result["command_id"])
            if record is not None and record["status"] in {"accepted", "dispatched"}:
                if result["status"] == "completed" and record["command"]["action"] in MUTATING:
                    # A dispatched intent is not proof of playback.
                    if not self._playback_matches(device, record["command"], result.get("result")):
                        result = dict(result, status="dispatched")
                record.update(result)
                state = self._playlist_state
                if state and state.get("command_id") == result["command_id"]:
                    returned = result.get("result")
                    playback_id = (
                        returned.get("playback_id") if isinstance(returned, dict) else None
                    )
                    if isinstance(playback_id, str) and playback_id:
                        state["expected_playback_id"] = playback_id
                    if result["status"] in {"rejected", "failed", "unknown", "expired"}:
                        state.update(
                            status="error", error=result.get("error_code") or "INTERNAL_ERROR"
                        )
                device.last_result = {
                    "command_id": result["command_id"],
                    "action": record["command"]["action"],
                    "app": record["command"]["app"],
                    "status": result["status"],
                    "error_code": result["error_code"],
                }
        elif kind == "capabilities":
            device.apps = normalize_apps(payload)
        elif kind == "playback_state":
            previous = (
                device.playback.get("profile_id") if isinstance(device.playback, dict) else None
            )
            device.playback = payload
            current = payload.get("profile_id") if isinstance(payload, dict) else None
            if previous and current and previous != current:
                self.invalidate_candidates()
                for record in device.pending.values():
                    if record["status"] in {"accepted"} and record["command"]["action"] in MUTATING:
                        record.update(status="rejected", error_code="PROFILE_CHANGED")
            await self._observe_playlist(device)
        device.events[event_id] = {"kind": kind}
        while len(device.events) > EVENT_DEDUP:
            device.events.popitem(last=False)
        device.changed.set()
        self._bump()
        return web.json_response({"event_id": event_id})

    def _take(self, device: _Device) -> dict[str, Any] | None:
        stamp = self.now()
        while device.queue:
            command = device.queue.popleft()
            record = device.pending.get(command["command_id"])
            if record is None or record["status"] != "accepted":
                continue
            if command["expires_at_ms"] < stamp:
                record.update(status="expired", error_code="EXPIRED")
                device.changed.set()
                continue
            record["taken"] = True
            record["status"] = "accepted"
            return command
        return None

    def _notify(self, device: _Device) -> None:
        if device.waiter is not None and not device.waiter.done():
            device.waiter.set_result(True)
        device.changed.set()

    def _disconnect(self, device: _Device, *, replay: bool) -> None:
        del replay  # Reconnect never replays unread commands.
        if device.waiter is not None and not device.waiter.done():
            device.waiter.cancel()
        device.waiter = None
        while device.queue:
            command = device.queue.popleft()
            record = device.pending.get(command["command_id"])
            if record is not None and not record["taken"]:
                record.update(status="unknown", error_code="DISCONNECTED")
            elif record is not None and record["status"] == "accepted":
                record.update(status="unknown", error_code="DISCONNECTED")
        for record in device.pending.values():
            if record["taken"] and record["status"] == "accepted":
                record.update(status="unknown", error_code="DISCONNECTED")
        device.connected = False
        device.server_epoch = None
        device.connection_id = None
        device.playback = None
        if self._playlist_state and self._playlist_state.get("device_id") == device.device_id:
            self._playlist_state.update(status="error", error="DISCONNECTED")
        device.changed.set()

    def _abandon(self, command_id: str, status: str, error: str) -> None:
        device = self._device_for_command(command_id)
        if device is None:
            return
        record = device.pending.get(command_id)
        if record is None or record["status"] not in {"accepted"}:
            return
        if record["taken"]:
            record.update(status="unknown", error_code=error)
        else:
            device.queue = deque(item for item in device.queue if item["command_id"] != command_id)
            record.update(status=status, error_code=error)
        device.changed.set()

    def _playback_matches(
        self, device: _Device, command: dict[str, Any], result: object = None
    ) -> bool:
        playback = playback_fresh(device.playback, self.now())
        if playback is None or command["action"] != "play_content":
            return False
        wanted = command["args"].get("content")
        observed = playback.get("content")
        if result is None:
            result = device.pending.get(command["command_id"], {}).get("result")
        expected_id = result.get("playback_id") if isinstance(result, dict) else None
        expected_profile = command["args"].get("profile_id")
        return (
            playback.get("app") == command["app"]
            and isinstance(wanted, dict)
            and isinstance(observed, dict)
            and wanted == observed
            and isinstance(expected_id, str)
            and bool(expected_id)
            and playback.get("playback_id") == expected_id
            and (expected_profile is None or playback.get("profile_id") == expected_profile)
            and playback.get("state") == "playing"
        )

    def _create_pairing(self) -> dict[str, Any]:
        pairing_id = str(uuid.uuid4())
        code = secrets.token_urlsafe(24)
        expires = self.now() + PAIRING_TTL_MS
        self.pairing = {
            "pairing_id": pairing_id,
            "code_sha256": digest(code),
            "expires_at_ms": expires,
        }
        document = {
            "schema_version": SCHEMA,
            "endpoint": self.endpoint,
            "server_name": "Jarvis Office",
            "pairing_id": pairing_id,
            "code": code,
            "expires_at_ms": expires,
            "ca_pem": self.ca_pem,
            "cert_sha256": self.cert_sha256,
            "avt_allowed_cert_sha256": list(self.settings.avt_allowed_cert_sha256),
        }
        return document

    async def _authenticate(self, request: web.Request) -> _Device:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or "\n" in header:
            raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
        token = header[7:]
        if not token or len(token) > 256:
            raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
        hashed = digest(token)
        for device in self.devices.values():
            if device.revoked:
                continue
            if hmac.compare_digest(device.credential_sha256, hashed):
                if not self._enabled():
                    raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
                return device
        raise TvProtocolError("rejected", "UNAUTHORIZED", 401)

    def _connected_device(self) -> _Device | None:
        stamp = self.now()
        live = [
            device
            for device in self.devices.values()
            if device.connected
            and not device.revoked
            and device.server_epoch
            and stamp - device.last_poll_ms <= HEARTBEAT_MS
        ]
        return live[0] if len(live) == 1 else None

    def _device_for_command(self, command_id: str) -> _Device | None:
        for device in self.devices.values():
            if command_id in device.pending:
                return device
        return None

    def enabled(self) -> bool:
        return self._enabled_flag

    def _enabled(self) -> bool:
        return self.enabled()

    def _defaults(self) -> dict[str, str]:
        return {
            "default_video_app": self._video_app,
            "default_film_app": self._film_app,
        }

    async def _load_registry(self) -> None:
        self._apply_registry(await self._registry())

    async def _registry(self) -> dict[str, Any]:
        data = await self.store.tv_registry()
        merged = _registry_default()
        merged.update({key: data[key] for key in merged if key in data})
        if not isinstance(merged["devices"], dict):
            merged["devices"] = {}
        merged["playlists"] = _normalize_playlists(merged.get("playlists"))
        return merged

    async def _save_registry(self, registry: dict[str, Any]) -> None:
        await self.store.save_tv_registry(registry)
        self._apply_registry(registry)
        self._bump()

    def _apply_registry(self, registry: dict[str, Any]) -> None:
        self._enabled_flag = registry.get("enabled") is True
        self._video_app = (
            registry["default_video_app"]
            if registry.get("default_video_app") in {"smarttube", "avt"}
            else "smarttube"
        )
        self._film_app = (
            registry["default_film_app"]
            if registry.get("default_film_app") in {"smarttube", "avt"}
            else "avt"
        )
        self.playlists = _normalize_playlists(registry.get("playlists"))
        if self._playlist_state and not any(
            item["id"] == self._playlist_state.get("playlist_id") for item in self.playlists
        ):
            self._playlist_state = None
        seen: set[str] = set()
        for device_id, record in registry.get("devices", {}).items():
            if not is_uuid(device_id) or not isinstance(record, dict):
                continue
            hashed = record.get("credential_sha256")
            revoked = record.get("revoked") is True
            seen.add(device_id)
            if device_id in self.devices:
                device = self.devices[device_id]
                if isinstance(hashed, str) and SHA256_RE.fullmatch(hashed):
                    device.credential_sha256 = hashed
                device.revoked = revoked
                continue
            if not isinstance(hashed, str) or not SHA256_RE.fullmatch(hashed):
                continue
            device = _Device(device_id, hashed)
            device.revoked = revoked
            self.devices[device_id] = device
        for leftover in [key for key in self.devices if key not in seen]:
            self.devices.pop(leftover, None)

    async def _set_enabled(self, enabled: bool) -> None:
        registry = await self._registry()
        registry["enabled"] = enabled
        await self._save_registry(registry)

    async def _revoke(self, device_id: str) -> None:
        registry = await self._registry()
        record = registry["devices"].get(device_id)
        if isinstance(record, dict):
            record["revoked"] = True
            record.pop("credential_sha256", None)
        await self._save_registry(registry)
        device = self.devices.get(device_id)
        if device is not None:
            device.revoked = True
            device.credential_sha256 = "0" * 64
            self._disconnect(device, replay=False)

    def _bump(self) -> None:
        if self.on_change is not None:
            self.on_change()

    @staticmethod
    def _error(code: str, status: int) -> web.Response:
        return web.json_response({"error": code}, status=status)
