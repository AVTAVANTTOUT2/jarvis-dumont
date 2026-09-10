"""Real loopback health/lock contract, with the existing engine and cloud doubles."""

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_echo_live as fixtures

from jarvis_office import runtime
from jarvis_office.dashboard import DashboardServer
from jarvis_office.echo.context import PassiveContextBuffer
from jarvis_office.echo.gateway import Session
from jarvis_office.storage import OfficeStore


class PrivateHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.LiveTests.asyncSetUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(runtime, "private_root", return_value=self.root)
        self.root_patch.start()
        self.store = OfficeStore(self.root / "office.sqlite3")
        await self.store.start()
        self.gateway.store = self.store
        self.server = DashboardServer(
            self.store,
            snapshot=self.gateway.dashboard_state,
            command=self.gateway.dashboard_command,
            context=self.gateway.dashboard_context,
            port=0,
        )
        await self.server.start()
        self.instance = runtime.Instance()
        self.publish()

    def publish(self):
        self.instance.record.update(dashboard="private-v1", server_epoch=self.gateway.server_epoch)
        self.instance.publish(self.voice.session, self.server.port)

    async def asyncTearDown(self):
        await self.server.close()
        await fixtures.LiveTests.asyncTearDown(self)
        await self.store.close()
        self.instance.close()
        self.root_patch.stop()
        self.temp.cleanup()

    async def health(self):
        before = runtime.lock_path().read_bytes()
        result = await asyncio.to_thread(runtime.status)
        self.assertEqual(runtime.lock_path().read_bytes(), before)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.voice.armed)
        return result

    async def test_good_instance_matches_owned_epoch(self):
        result = await self.health()
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(result["owner_verified"])
        self.assertTrue(result["http_loopback"])
        self.assertEqual(result["microphone"], "closed")

    async def test_old_metadata_cannot_accept_even_matching_conversation(self):
        for epoch in (None, "", "not-an-epoch", 123):
            with self.subTest(epoch=epoch):
                self.instance.record["server_epoch"] = epoch
                self.instance.publish(self.voice.session, self.server.port)
                self.assertEqual((await self.health())["error"], "instance_epoch_missing")

    async def test_other_epoch_rejected_even_with_same_conversation(self):
        self.gateway.server_epoch = uuid.uuid4().hex
        self.assertEqual(self.instance.record["session"], self.voice.session)
        self.assertEqual((await self.health())["error"], "instance_epoch_mismatch")

    async def test_stale_process_identity_is_still_rejected(self):
        with patch.object(runtime, "identity", return_value={"started": "another process"}):
            self.assertEqual((await self.health())["error"], "instance_identity_mismatch")

    async def test_restart_requires_new_owned_epoch_metadata(self):
        old_epoch = self.gateway.server_epoch
        inode = runtime.lock_path().stat().st_ino
        self.gateway.server_epoch = uuid.uuid4().hex
        self.assertNotEqual(self.gateway.server_epoch, old_epoch)
        self.assertEqual((await self.health())["error"], "instance_epoch_mismatch")
        self.instance.close()
        self.instance = runtime.Instance()
        self.publish()
        self.assertEqual(runtime.lock_path().stat().st_ino, inode)
        self.assertEqual((await self.health())["status"], "PASS")

    async def test_echo_reconnect_does_not_change_server_identity(self):
        epoch, echo_id = self.gateway.server_epoch, self.s.id
        await self.gateway.drop(self.s)
        self.s = Session("test", self.endpoint, PassiveContextBuffer(), audio=self.endpoint)
        self.endpoint.session = self.s
        self.gateway.sessions["test"] = self.s
        self.assertNotEqual(self.s.id, echo_id)
        self.assertEqual(self.s.mode, "OFF")
        self.assertEqual(self.gateway.server_epoch, epoch)
        self.assertEqual((await self.health())["status"], "PASS")

    async def test_conversation_clear_does_not_change_server_identity(self):
        conversation, epoch = self.voice.session, self.gateway.server_epoch
        await self.voice.control("clear")
        self.assertNotEqual(self.voice.session, conversation)
        self.assertEqual(self.instance.record["session"], conversation)
        self.assertEqual(self.gateway.server_epoch, epoch)
        self.assertEqual((await self.health())["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
