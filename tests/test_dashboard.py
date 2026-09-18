import json
import tempfile
import unittest
from pathlib import Path

import aiohttp

from jarvis_office.dashboard import DashboardServer
from jarvis_office.storage import OfficeStore


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OfficeStore(Path(self.temp.name) / "office.sqlite3")
        await self.store.start()
        self.state = {
            "schema_version": 1,
            "server_epoch": "epoch",
            "event_id": 1,
            "devices": [],
            "budget": {"diagnostic": {"used": 9, "limit": 10}},
        }
        self.commands = []

        async def snapshot():
            return dict(self.state)

        async def command(payload):
            self.commands.append(payload)
            return {"command_id": payload["command_id"], "status": "applied", "error": None}

        async def context(device):
            return {
                "entries": [],
                "limits": {"seconds": 1800, "chars": 20000, "utterances": 100},
                "next_turn_max_chars": 3500,
            }

        self.server = DashboardServer(
            self.store, snapshot=snapshot, command=command, context=context, port=0
        )
        await self.server.start()
        self.client = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True), headers={"Sec-Fetch-Site": "same-origin"}
        )
        async with self.client.get(self.server.url + "/api/bootstrap") as response:
            self.assertEqual(response.status, 200)
            self.csrf = (await response.json())["csrf_token"]
            cookie = response.headers["Set-Cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        await self.store.close()
        self.temp.cleanup()

    async def post(self, path, data, **headers):
        return await self.client.post(
            self.server.url + path,
            json=data,
            headers={"Origin": self.server.url, "X-CSRF-Token": self.csrf, **headers},
        )

    async def test_owner_auth_origin_host_and_echo_token_separation(self):
        async with aiohttp.ClientSession() as other:
            async with other.get(
                self.server.url + "/api/state", headers={"Sec-Fetch-Site": "same-origin"}
            ) as response:
                self.assertEqual(response.status, 401)
        for headers in (
            {"Host": "evil.invalid"},
            {"Origin": "https://evil.invalid"},
            {"Authorization": "Bearer echo-device-token"},
            {"Sec-Fetch-Site": "cross-site"},
            {"X-Forwarded-For": "127.0.0.1"},
        ):
            async with self.client.get(self.server.url + "/api/state", headers=headers) as response:
                self.assertEqual(response.status, 403, headers)
        async with self.client.get(self.server.url + "/api/state") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("microphone=()", response.headers["Permissions-Policy"])
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual((await response.json())["budget"]["diagnostic"]["used"], 9)
        self.assertEqual(self.commands, [])

    async def test_settings_csrf_validation_and_nominal_budget_gate(self):
        response = await self.post(
            "/api/settings", {"history_enabled": True}, **{"X-CSRF-Token": "bad"}
        )
        self.assertEqual(response.status, 403)
        await response.release()
        body = {"device_id": "echo", "action": "set_mode", "mode": "ACTIVE", "command_id": "a"}
        response = await self.post("/api/command", body)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"], "NOMINAL_BUDGET_REQUIRED")
        body["mode"] = "OFF"
        response = await self.post("/api/command", body)
        self.assertEqual((await response.json())["status"], "applied")
        response = await self.post("/api/settings", {"budget": {"limit": 5, "period": "day"}})
        self.assertEqual(response.status, 200)
        await response.release()
        body["mode"] = "ACTIVE"
        response = await self.post("/api/command", body)
        self.assertEqual((await response.json())["status"], "applied")
        body["mode"] = "COMMAND"
        response = await self.post("/api/command", body)
        self.assertEqual((await response.json())["status"], "applied")
        body["mode"] = "ALEXA"
        response = await self.post("/api/command", body)
        self.assertEqual(response.status, 400)
        await response.release()
        self.assertEqual(len(self.commands), 3)
        self.assertEqual(self.commands[-1]["mode"], "COMMAND")
        self.assertEqual((await self.store.settings())["budget"]["used"], 0)

        response = await self.post(
            "/api/command",
            {"device_id": "echo", "action": "preview_voice", "command_id": "local-preview"},
        )
        self.assertEqual((await response.json())["status"], "applied")
        self.assertEqual((await self.store.settings())["budget"]["used"], 0)

    async def test_orb_settings_and_local_assets_need_no_audio_command(self):
        response = await self.post("/api/settings", {"orb_style": "weaving"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["orb_style"], "weaving")
        self.assertEqual(self.store.orb_style, "weaving")
        response = await self.post("/api/settings", {"orb_style": "external-script"})
        self.assertEqual(response.status, 400)
        await response.release()
        response = await self.post(
            "/api/settings", {"orb_style": "working"}, **{"X-CSRF-Token": "bad"}
        )
        self.assertEqual(response.status, 403)
        await response.release()
        self.assertEqual(self.store.orb_style, "weaving")
        for name in ("orbs.js", "thinking-orbs.js", "THINKING_ORBS_LICENSE.txt"):
            async with self.client.get(self.server.url + "/dashboard/" + name) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.commands, [])

    async def test_logout_requires_owner_cookie_csrf_and_only_ends_that_session(self):
        before = set(self.server.sessions)
        for headers in ({"X-CSRF-Token": "bad"}, {"Authorization": "Bearer echo-token"}):
            response = await self.post("/api/logout", {}, **headers)
            self.assertEqual(response.status, 403)
            await response.release()
            self.assertEqual(set(self.server.sessions), before)
        async with aiohttp.ClientSession() as other:
            async with other.post(
                self.server.url + "/api/logout",
                json={},
                headers={"Sec-Fetch-Site": "same-origin", "Origin": self.server.url},
            ) as response:
                self.assertEqual(response.status, 401)
            async with other.get(
                self.server.url + "/api/bootstrap", headers={"Sec-Fetch-Site": "same-origin"}
            ) as response:
                self.assertEqual(response.status, 200)
        other_sessions = set(self.server.sessions) - before
        response = await self.post("/api/logout", {})
        self.assertEqual(response.status, 204)
        await response.release()
        self.assertEqual(set(self.server.sessions), other_sessions)
        async with self.client.get(self.server.url + "/api/state") as response:
            self.assertEqual(response.status, 401)
        self.assertEqual(self.commands, [])

    async def test_events_snapshot_epoch_and_bounded_reconnection(self):
        async with self.client.get(self.server.url + "/api/events?after=0&epoch=old") as response:
            result = await response.json()
            self.assertTrue(result["reset"])
            self.assertEqual(result["snapshot"]["event_id"], 1)
        self.state["event_id"] = 2
        async with self.client.get(self.server.url + "/api/events?after=1&epoch=epoch") as response:
            result = await response.json()
            self.assertFalse(result["reset"])
            self.assertEqual([e["event_id"] for e in result["events"]], [2])
        async with self.client.get(self.server.url + "/api/events?after=2&epoch=epoch") as response:
            self.assertEqual((await response.json())["events"], [])
        self.assertEqual(self.server.events.maxlen, 256)
        self.assertEqual(self.commands, [])

    async def test_data_routes_scope_confirmation_and_backup(self):
        async with self.client.get(self.server.url + "/api/data/catalog") as response:
            tables = (await response.json())["tables"]
            self.assertEqual(len(tables), 8)
            self.assertIn("conversation_memory", [table["name"] for table in tables])
        url = self.server.url + "/api/data/rows?table=sqlite_schema"
        async with self.client.get(url) as response:
            self.assertEqual(response.status, 400)
        filters = json.dumps([{"column": "generation", "op": "eq", "value": "bad"}])
        async with self.client.get(
            self.server.url + "/api/data/rows", params={"table": "turns", "filters": filters}
        ) as response:
            self.assertEqual(response.status, 400)
        response = await self.post("/api/data/export", {"table": "turns", "format": "csv"})
        self.assertEqual(response.status, 400)
        await response.release()
        response = await self.post(
            "/api/data/export", {"table": "turns", "format": "csv", "confirm": True}
        )
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        await response.read()
        response = await self.post("/api/data/backup", {"confirm": True})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.read()).startswith(b"SQLite format 3"))
        response = await self.post("/api/data/purge", {"scope": "conversations", "confirm": True})
        self.assertEqual((await response.json())["generation"], 1)
        response = await self.post(
            "/api/command", {"device_id": "echo", "action": "clear_context", "command_id": "clear"}
        )
        self.assertEqual(response.status, 400)
        await response.release()

    async def test_clear_memory_requires_confirm_and_memory_keeps_history(self):
        response = await self.post(
            "/api/command",
            {"device_id": "echo", "action": "clear_memory", "command_id": "memory"},
        )
        self.assertEqual(response.status, 400)
        await response.release()
        response = await self.post(
            "/api/command",
            {
                "device_id": "echo",
                "action": "clear_memory",
                "command_id": "memory",
                "confirm": True,
            },
        )
        self.assertEqual((await response.json())["status"], "applied")
        response = await self.post("/api/settings", {"memory_enabled": True})
        self.assertTrue((await response.json())["history_enabled"])
        response = await self.post("/api/settings", {"history_enabled": False})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "MEMORY_REQUIRES_HISTORY")

    async def test_lan_configuration_and_request_size_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "DASHBOARD_LOOPBACK_ONLY"):
            DashboardServer(
                self.store,
                snapshot=self.server.snapshot_callback,
                command=self.server.command_callback,
                context=self.server.context_callback,
                host="0.0.0.0",
            )
        response = await self.post("/api/settings", {"history_enabled": "x" * 40000})
        self.assertEqual(response.status, 413)
        await response.release()

    async def test_tv_route_owner_only_and_rejects_device_bearer(self):
        response = await self.post(
            "/api/tv", {"action": "enable"}, **{"Authorization": "Bearer tv"}
        )
        self.assertEqual(response.status, 403)
        await response.release()
        response = await self.post("/api/tv", {"action": "enable"}, **{"X-CSRF-Token": "bad"})
        self.assertEqual(response.status, 403)
        await response.release()
        response = await self.post("/api/tv", {"action": "enable"})
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"], "TV_NOT_CONFIGURED")
        captured: list[dict] = []

        async def tv_command(body: dict) -> dict:
            captured.append(body)
            return {"status": "applied", "error": None}

        self.server.tv_command = tv_command
        response = await self.post("/api/tv", {"action": "enable", "extra": True})
        self.assertEqual(response.status, 400)
        await response.release()
        response = await self.post("/api/tv", {"action": "pair", "confirm": True})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "applied")
        self.assertEqual(captured, [{"action": "pair", "confirm": True}])
        async with self.client.get(self.server.url + "/api/tv") as get:
            self.assertIn(get.status, {404, 405})
            await get.release()
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()
