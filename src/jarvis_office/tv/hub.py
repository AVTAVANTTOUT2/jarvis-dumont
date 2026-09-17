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
    validate_event,
)

COMMAND_HISTORY_MAX = 128


def _registry_default() -> dict[str, Any]:
    return {
        "enabled": False,
        "default_video_app": "smarttube",
        "default_film_app": "avt",
        "devices": {},
    }


@dataclass(frozen=True)
class TvSettings:
    bind: str
    port: int
    cert: str
    key: str
    avt_allowed_cert_sha256: tuple[str, ...] = ()

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
        allowed = {"bind", "port", "cert", "key", "avt_allowed_cert_sha256"}
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
            "cert_sha256": self.cert_sha256,
            "pairing_pending": self.pairing is not None and self.pairing["expires_at_ms"] > stamp,
            "defaults": {
                "video": self._defaults()["default_video_app"],
                "film": self._defaults()["default_film_app"],
            },
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
        if action == "enable":
            await self._set_enabled(True)
            return {"status": "applied", "error": None}
        if action == "disable":
            await self._set_enabled(False)
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
