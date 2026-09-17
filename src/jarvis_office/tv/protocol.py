"""Shared TV v1 envelopes. No secrets, no raw OS exceptions."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from typing import Any

SCHEMA = 1
MAX_BODY = 65536
HEARTBEAT_MS = 45000
COMMANDS_WAIT_S = 20.0
DEFAULT_TTL_MS = 10000
MAX_TTL_MS = 30000
QUEUE_MAX = 8
EVENT_DEDUP = 64
PLAYBACK_VALID_MS = 5000
POSITION_MAX_MS = 86_400_000
QUERY_MAX = 120
SEARCH_LIMIT_MAX = 10
PAIRING_TTL_MS = 10 * 60 * 1000
APPS = frozenset({"smarttube", "avt"})
ACTIONS = frozenset({"get_state", "search", "play_content", "pause", "resume", "stop", "seek"})
MUTATING = frozenset({"play_content", "pause", "resume", "stop", "seek"})
RESULT_STATUSES = frozenset(
    {"accepted", "dispatched", "completed", "rejected", "expired", "failed", "unknown"}
)
ERROR_CODES = frozenset(
    {
        "UNSUPPORTED",
        "APP_NOT_INSTALLED",
        "PERMISSION_REQUIRED",
        "PROFILE_REQUIRED",
        "PROFILE_CHANGED",
        "AMBIGUOUS_CONTENT",
        "CONTENT_UNAVAILABLE",
        "STALE_TARGET",
        "EXPIRED",
        "BUSY",
        "UNAUTHORIZED",
        "DISCONNECTED",
        "INTERNAL_ERROR",
    }
)
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
YOUTUBE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class TvProtocolError(Exception):
    def __init__(self, status: str, error_code: str, http: int = 400) -> None:
        if error_code not in ERROR_CODES:
            error_code = "INTERNAL_ERROR"
        self.status, self.error_code, self.http = status, error_code, http
        super().__init__(error_code)


def now_ms() -> int:
    return int(time.time() * 1000)


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def require_uuid(value: object, code: str = "UNAUTHORIZED") -> str:
    if not is_uuid(value):
        raise TvProtocolError("rejected", code, 401 if code == "UNAUTHORIZED" else 400)
    return str(value)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_der_from_pem(pem: str) -> str:
    body = "".join(line.strip() for line in pem.splitlines() if "-----" not in line)
    der = base64.b64decode(body)
    if not der:
        raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
    return hashlib.sha256(der).hexdigest()


def parse_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY:
        raise TvProtocolError("rejected", "UNAUTHORIZED", 413)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise TvProtocolError("rejected", "UNSUPPORTED") from None
    if not isinstance(data, dict):
        raise TvProtocolError("rejected", "UNSUPPORTED")
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BODY:
        raise TvProtocolError("rejected", "UNAUTHORIZED", 413)
    return data


def require_schema(data: dict[str, Any]) -> None:
    if data.get("schema_version") != SCHEMA:
        raise TvProtocolError("rejected", "UNSUPPORTED")


def empty_app(player_kind: str = "unknown") -> dict[str, Any]:
    return {
        "installed": False,
        "app_version": None,
        "player_kind": player_kind,
        "actions": [],
        "targeted_control": False,
        "state_observable": False,
        "missing_permissions": [],
    }


def normalize_app_entry(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return empty_app()
    actions = raw.get("actions")
    missing = raw.get("missing_permissions")
    version = raw.get("app_version")
    player = raw.get("player_kind")
    return {
        "installed": raw.get("installed") is True,
        "app_version": version if isinstance(version, str) and len(version) <= 64 else None,
        "player_kind": player if player in {"native", "web", "unknown"} else "unknown",
        "actions": [
            item
            for item in actions
            if isinstance(item, str)
            and (item in ACTIONS or item in {"open_search_ui", "play_search_first"})
        ]
        if isinstance(actions, list)
        else [],
        "targeted_control": raw.get("targeted_control") is True,
        "state_observable": raw.get("state_observable") is True,
        "missing_permissions": [
            item for item in missing if isinstance(item, str) and item in ERROR_CODES
        ]
        if isinstance(missing, list)
        else [],
    }


def normalize_apps(raw: object) -> dict[str, Any]:
    source = raw.get("apps") if isinstance(raw, dict) else None
    if not isinstance(source, dict):
        source = raw if isinstance(raw, dict) else {}
    return {
        "smarttube": normalize_app_entry(source.get("smarttube")),
        "avt": normalize_app_entry(source.get("avt")),
    }


def validate_content(app: str, content: object) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise TvProtocolError("rejected", "UNSUPPORTED")
    kind, identifier = content.get("kind"), content.get("id")
    if not isinstance(kind, str) or not isinstance(identifier, str):
        raise TvProtocolError("rejected", "UNSUPPORTED")
    if app == "smarttube":
        if kind != "youtube_video" or not YOUTUBE_RE.fullmatch(identifier):
            raise TvProtocolError("rejected", "UNSUPPORTED")
        return {"kind": kind, "id": identifier}
    if kind not in {"movie", "episode"} or not identifier or len(identifier) > 200:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    result: dict[str, Any] = {"kind": kind, "id": identifier}
    if kind == "episode":
        season, episode = content.get("season"), content.get("episode")
        if type(season) is not int or type(episode) is not int or season < 0 or episode < 0:
            raise TvProtocolError("rejected", "UNSUPPORTED")
        result["season"], result["episode"] = season, episode
    return result


def validate_position(args: dict[str, Any]) -> int:
    if "position_ms" not in args or args["position_ms"] is None:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    position = args["position_ms"]
    if type(position) is not int or not 0 <= position <= POSITION_MAX_MS:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    return position


def validate_args(app: str, action: str, args: object) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise TvProtocolError("rejected", "UNSUPPORTED")
    if action == "get_state":
        extra = [key for key in args if not str(key).startswith("x_")]
        if extra:
            raise TvProtocolError("rejected", "UNSUPPORTED")
        return {}
    if action == "search":
        query = args.get("query")
        if not isinstance(query, str):
            raise TvProtocolError("rejected", "UNSUPPORTED")
        query = query.strip()
        if not query or len(query) > QUERY_MAX:
            raise TvProtocolError("rejected", "UNSUPPORTED")
        limit = args.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= SEARCH_LIMIT_MAX:
            raise TvProtocolError("rejected", "UNSUPPORTED")
        result: dict[str, Any] = {"query": query, "limit": limit}
        if "profile_id" in args:
            profile = args.get("profile_id")
            if not isinstance(profile, str) or not profile:
                raise TvProtocolError("rejected", "PROFILE_REQUIRED")
            result["profile_id"] = profile[:128]
        return result
    if action == "play_content":
        result = {"content": validate_content(app, args.get("content"))}
        if "position_ms" in args:
            result["position_ms"] = validate_position(args)
        for key in ("profile_id", "version"):
            value = args.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value or len(value) > 128:
                raise TvProtocolError("rejected", "UNSUPPORTED")
            result[key] = value
        return result
    playback_id = args.get("playback_id")
    if not isinstance(playback_id, str) or not playback_id:
        raise TvProtocolError("rejected", "STALE_TARGET")
    result = {"playback_id": playback_id[:128]}
    profile = args.get("profile_id")
    if isinstance(profile, str) and profile:
        result["profile_id"] = profile[:128]
    if action == "seek":
        result["position_ms"] = validate_position(args)
    return result


def validate_command(
    command: dict[str, Any],
    *,
    device_id: str,
    server_epoch: str,
    connection_id: str,
    server_now: int,
) -> dict[str, Any]:
    require_schema(command)
    command_id = require_uuid(command.get("command_id"))
    if command.get("device_id") != device_id:
        raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
    if command.get("server_epoch") != server_epoch:
        raise TvProtocolError("expired", "EXPIRED")
    if command.get("connection_id") != connection_id:
        raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
    app = command.get("app")
    action = command.get("action")
    if app not in APPS or action not in ACTIONS:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    issued = command.get("issued_at_ms")
    expires = command.get("expires_at_ms")
    if type(issued) is not int or type(expires) is not int or issued < 0 or expires < 0:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    if expires - issued > MAX_TTL_MS or expires < server_now:
        raise TvProtocolError("expired", "EXPIRED")
    args = validate_args(app, action, command.get("args"))
    return {
        "schema_version": SCHEMA,
        "command_id": command_id,
        "device_id": device_id,
        "server_epoch": server_epoch,
        "connection_id": connection_id,
        "app": app,
        "action": action,
        "args": args,
        "issued_at_ms": issued,
        "expires_at_ms": expires,
    }


def build_command(
    *,
    device_id: str,
    server_epoch: str,
    connection_id: str,
    app: str,
    action: str,
    args: dict[str, Any],
    ttl_ms: int = DEFAULT_TTL_MS,
    issued_at_ms: int | None = None,
) -> dict[str, Any]:
    if app not in APPS or action not in ACTIONS:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    if type(ttl_ms) is not int or not 1 <= ttl_ms <= MAX_TTL_MS:
        raise TvProtocolError("rejected", "EXPIRED")
    issued = now_ms() if issued_at_ms is None else issued_at_ms
    command = {
        "schema_version": SCHEMA,
        "command_id": str(uuid.uuid4()),
        "device_id": device_id,
        "server_epoch": server_epoch,
        "connection_id": connection_id,
        "app": app,
        "action": action,
        "args": args,
        "issued_at_ms": issued,
        "expires_at_ms": issued + ttl_ms,
    }
    return validate_command(
        command,
        device_id=device_id,
        server_epoch=server_epoch,
        connection_id=connection_id,
        server_now=issued,
    )


def validate_event(
    body: dict[str, Any], *, device_id: str, server_epoch: str, connection_id: str
) -> dict[str, Any]:
    require_schema(body)
    event_id = require_uuid(body.get("event_id"))
    if body.get("device_id") != device_id:
        raise TvProtocolError("rejected", "UNAUTHORIZED", 401)
    if body.get("server_epoch") != server_epoch or body.get("connection_id") != connection_id:
        raise TvProtocolError("rejected", "DISCONNECTED", 409)
    sequence = body.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    kind = body.get("kind")
    payload = body.get("payload")
    if kind not in {"command_result", "playback_state", "capabilities"} or not isinstance(
        payload, dict
    ):
        raise TvProtocolError("rejected", "UNSUPPORTED")
    return {
        "schema_version": SCHEMA,
        "event_id": event_id,
        "device_id": device_id,
        "server_epoch": server_epoch,
        "connection_id": connection_id,
        "sequence": sequence,
        "kind": kind,
        "payload": payload,
    }


def validate_command_result(payload: dict[str, Any]) -> dict[str, Any]:
    command_id = require_uuid(payload.get("command_id"), "UNSUPPORTED")
    status = payload.get("status")
    if status not in RESULT_STATUSES:
        raise TvProtocolError("rejected", "UNSUPPORTED")
    error = payload.get("error_code")
    if error is not None and error not in ERROR_CODES:
        error = "INTERNAL_ERROR"
    result = payload.get("result")
    if result is not None and not isinstance(result, dict):
        result = None
    return {
        "command_id": command_id,
        "status": status,
        "error_code": error,
        "result": result,
    }


def playback_fresh(playback: object, server_now: int) -> dict[str, Any] | None:
    if not isinstance(playback, dict):
        return None
    observed = playback.get("observed_at_ms")
    valid = playback.get("valid_for_ms", PLAYBACK_VALID_MS)
    if type(observed) is not int or type(valid) is not int:
        return None
    if valid > PLAYBACK_VALID_MS:
        valid = PLAYBACK_VALID_MS
    if observed <= 0 or server_now - observed > valid:
        return None
    return playback


def action_supported(apps: dict[str, Any], app: str, action: str) -> str | None:
    entry = normalize_app_entry(apps.get(app) if isinstance(apps, dict) else None)
    if not entry["installed"]:
        return "APP_NOT_INSTALLED"
    if action not in entry["actions"]:
        return "UNSUPPORTED"
    return None
