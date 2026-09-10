"""Loopback-only owner dashboard in the existing gateway process; no audio owner."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from jarvis_office.storage import OfficeStore, StorageError

COOKIE = "jarvis_office_owner"
STATIC = Path(__file__).with_name("static") / "dashboard"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "microphone=(), camera=(), geolocation=()",
}


class DashboardServer:
    def __init__(
        self,
        store: OfficeStore,
        *,
        snapshot: Callable[[], Awaitable[dict[str, Any]]],
        command: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        context: Callable[[str], Awaitable[dict[str, Any]]],
        host: str = "127.0.0.1",
        port: int = 8768,
        reports: list[dict[str, Any]] | None = None,
    ) -> None:
        if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("DASHBOARD_LOOPBACK_ONLY")
        self.store, self.snapshot_callback = store, snapshot
        self.command_callback, self.context_callback = command, context
        self.host, self.port = host, port
        self.reports = reports or []
        self.runner: web.AppRunner | None = None
        self.sessions: dict[str, tuple[str, float]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=256)
        self.epoch: str | None = None
        self.last_event = -1
        self._requests = 0
        self.app = web.Application(middlewares=[self._security], client_max_size=32768)
        self.app.add_routes(
            [
                web.get("/", self._asset),
                web.get("/index.html", self._asset),
                web.get("/dashboard.css", self._asset),
                web.get("/dashboard.js", self._asset),
                web.get("/styles.css", self._asset),
                web.get("/app.js", self._asset),
                web.get("/dashboard/index.html", self._asset),
                web.get("/dashboard/dashboard.css", self._asset),
                web.get("/dashboard/dashboard.js", self._asset),
                web.get("/dashboard/api.js", self._asset),
                web.get("/api.js", self._asset),
                web.get("/api/bootstrap", self._bootstrap),
                web.get("/api/state", self._state),
                web.get("/api/events", self._events),
                web.post("/api/command", self._command),
                web.get("/api/context", self._context),
                web.get("/api/settings", self._settings),
                web.post("/api/settings", self._settings),
                web.get("/api/data/catalog", self._catalog),
                web.get("/api/data/rows", self._rows),
                web.get("/api/data/row", self._row),
                web.post("/api/data/export", self._export),
                web.post("/api/data/purge", self._purge),
                web.post("/api/data/backup", self._backup),
            ]
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        if self.runner is not None:
            return
        self.runner = web.AppRunner(
            self.app,
            access_log=None,
            shutdown_timeout=10,
            keepalive_timeout=15,
            max_line_size=4096,
            max_field_size=4096,
            lingering_time=1,
        )
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, self.host, self.port)
            await site.start()
            self.port = self.runner.addresses[0][1]
        except Exception:
            await self.runner.cleanup()
            self.runner = None
            raise RuntimeError("DASHBOARD_START_FAILED") from None

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        self.sessions.clear()

    @staticmethod
    def _error(code: str, status: int = 400) -> web.Response:
        return web.json_response({"error": code}, status=status)

    @web.middleware
    async def _security(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        response: web.StreamResponse
        expected_host = f"{self.host}:{self.port}"
        peer = request.transport.get_extra_info("peername") if request.transport else None
        api = request.path.startswith("/api/")
        origin = request.headers.get("Origin")
        site = request.headers.get("Sec-Fetch-Site")
        session = self.sessions.get(request.cookies.get(COOKIE, ""))
        if (
            not peer
            or peer[0] != "127.0.0.1"
            or request.headers.get("Host") != expected_host
            or "Authorization" in request.headers
            or any(
                k in request.headers for k in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")
            )
        ):
            response = self._error("ACCESS_DENIED", 403)
        elif api and (site != "same-origin" or (origin is not None and origin != self.url)):
            response = self._error("ORIGIN_DENIED", 403)
        elif (
            api
            and request.path != "/api/bootstrap"
            and (not session or session[1] <= time.monotonic())
        ):
            response = self._error("OWNER_AUTH_REQUIRED", 401)
        elif request.method not in ("GET", "HEAD") and (
            origin != self.url
            or not session
            or not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session[0])
            or request.content_type != "application/json"
        ):
            response = self._error("CSRF_DENIED", 403)
        elif self._requests >= 16:
            response = self._error("TOO_MANY_REQUESTS", 429)
        else:
            self._requests += 1
            try:
                async with asyncio.timeout(15):
                    response = await handler(request)
            except StorageError as exc:
                code = str(exc)
                response = self._error(code, 503 if code.startswith("STORAGE") else 400)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                response = self._error("INVALID_REQUEST")
            except TimeoutError:
                response = self._error("REQUEST_TIMEOUT", 504)
            except web.HTTPException as exc:
                response = self._error("HTTP_REQUEST_REJECTED", exc.status)
            except Exception:
                response = self._error("DASHBOARD_OPERATION_FAILED", 500)
            finally:
                self._requests -= 1
        response.headers.update(SECURITY_HEADERS)
        return response

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        name = "index.html" if request.path == "/" else request.path.rsplit("/", 1)[-1]
        path = STATIC / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def _bootstrap(self, request: web.Request) -> web.Response:
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value[1] > now}
        token = request.cookies.get(COOKIE, "")
        if token not in self.sessions:
            if len(self.sessions) >= 16:
                return self._error("OWNER_SESSION_LIMIT", 429)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (secrets.token_urlsafe(32), now + 12 * 3600)
        response = web.json_response({"schema_version": 1, "csrf_token": self.sessions[token][0]})
        response.set_cookie(
            COOKIE, token, httponly=True, samesite="Strict", path="/", max_age=12 * 3600
        )
        # HTTP is allowed only on exact IPv4 loopback; LAN TLS/admin is intentionally unavailable.
        return response

    async def _snapshot(self) -> dict[str, Any]:
        state = dict(await self.snapshot_callback())
        state["schema_version"] = 1
        settings = await self.store.settings()
        state["budget"] = {
            "nominal": settings["budget"],
            "diagnostic": state.get("budget", {}).get(
                "diagnostic",
                {
                    "used": None,
                    "limit": None,
                    "source": "Non mesuré",
                },
            ),
        }
        state["storage"] = dict(
            self.store.status(),
            history_enabled=settings["history_enabled"],
            history_started_at=settings["history_started_at"],
        )
        state.setdefault("alerts", [])
        if self.store.error:
            state["alerts"] = [*state["alerts"], self.store.error]
        epoch, event = state.get("server_epoch"), state.get("event_id")
        if not isinstance(epoch, str) or type(event) is not int:
            raise RuntimeError("INVALID_STATE_CONTRACT")
        if epoch != self.epoch:
            self.epoch, self.last_event = epoch, -1
            self.events.clear()
        if event > self.last_event:
            self.events.append(
                {"event_id": event, "server_epoch": epoch, "type": "snapshot", "snapshot": state}
            )
            self.last_event = event
        return state

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(await self._snapshot())

    async def _events(self, request: web.Request) -> web.Response:
        state = await self._snapshot()
        after = int(request.query.get("after", "-1"))
        if not -1 <= after <= 2**63 - 1:
            raise ValueError()
        reset = (
            request.query.get("epoch") != self.epoch
            or after > self.last_event
            or (bool(self.events) and after < self.events[0]["event_id"] - 1)
        )
        events = [] if reset else [e for e in self.events if e["event_id"] > after]
        return web.json_response(
            {
                "server_epoch": self.epoch,
                "event_id": self.last_event,
                "reset": reset,
                "events": events,
                "snapshot": state,
            }
        )

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError()
        return body

    async def _command(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if not body.keys() <= {"device_id", "action", "mode", "command_id", "confirm"}:
            raise ValueError()
        action, mode = body.get("action"), body.get("mode")
        if (
            action not in ("set_mode", "interrupt", "clear_context", "preview_voice")
            or (action == "set_mode" and mode not in ("OFF", "ACTIVE", "PASSIVE"))
            or not isinstance(body.get("device_id"), str)
            or len(body["device_id"]) > 128
            or not isinstance(body.get("command_id"), str)
            or not 1 <= len(body["command_id"]) <= 64
        ):
            raise ValueError()
        if action == "clear_context" and body.get("confirm") is not True:
            return self._error("CONFIRMATION_REQUIRED")
        if action == "set_mode" and mode != "OFF":
            settings = await self.store.settings()
            budget = settings["budget"]
            if budget["limit"] is None:
                return self._error("NOMINAL_BUDGET_REQUIRED", 409)
            if budget["used"] >= budget["limit"]:
                return self._error("NOMINAL_BUDGET_EXHAUSTED", 409)
        try:
            async with asyncio.timeout(5):
                result = await self.command_callback(body)
        except TimeoutError:
            result = {
                "command_id": body["command_id"],
                "status": "rejected",
                "error": "COMMAND_TIMEOUT",
            }
        return web.json_response(result)

    async def _context(self, request: web.Request) -> web.Response:
        device = request.query.get("device_id", "")
        if not device or len(device) > 128:
            raise ValueError()
        result = dict(await self.context_callback(device))
        result["archive_enabled"] = (await self.store.settings())["archive_passive"]
        return web.json_response(result)

    async def _settings(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            return web.json_response(await self.store.update_settings(await self._body(request)))
        return web.json_response(await self.store.settings())

    async def _catalog(self, request: web.Request) -> web.Response:
        data = await self.store.catalog()
        data["reports"] = self.reports
        return web.json_response(data)

    @staticmethod
    def _query(request: web.Request) -> dict[str, Any]:
        query: dict[str, Any] = dict(request.query)
        for key in ("limit", "offset"):
            if key in query:
                query[key] = int(query[key])
        if "filters" in query:
            query["filters"] = json.loads(query["filters"])
        return query

    async def _rows(self, request: web.Request) -> web.Response:
        return web.json_response(await self.store.rows(**self._query(request)))

    async def _row(self, request: web.Request) -> web.Response:
        return web.json_response(await self.store.row(request.query["table"], request.query["id"]))

    async def _export(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if body.pop("confirm", None) is not True:
            return self._error("CONFIRMATION_REQUIRED")
        format = body.pop("format", "")
        payload, content_type = await self.store.export(body, format)
        return web.Response(
            body=payload,
            content_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="office-{body["table"]}.{format}"',
                "X-Export-Limit": str(body.get("limit", 1000)),
            },
        )

    async def _purge(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if body.get("confirm") is not True:
            return self._error("CONFIRMATION_REQUIRED")
        if set(body) != {"scope", "confirm"}:
            raise ValueError()
        return web.json_response(await self.store.purge(body["scope"]))

    async def _backup(self, request: web.Request) -> web.StreamResponse:
        if await self._body(request) != {"confirm": True}:
            return self._error("CONFIRMATION_REQUIRED")
        path = await self.store.backup()
        return web.FileResponse(
            path,
            headers={
                "Content-Disposition": 'attachment; filename="office-backup.sqlite3"',
                "Content-Type": "application/vnd.sqlite3",
            },
        )
