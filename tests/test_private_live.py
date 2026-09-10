import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import test_echo_live as fixtures

from jarvis_office.echo.audio import RemoteEchoEgress


class PrivateLiveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.LiveTests.asyncSetUp
    asyncTearDown = fixtures.LiveTests.asyncTearDown

    async def command(self, kind, **payload):
        await self.gateway.command(self.s, {"type": kind, "payload": payload}, time.monotonic_ns())

    async def test_command_ack_deduplicates_and_requested_never_claims_physical_capture(self):
        self.s.capabilities = {"private_state_v1"}
        self.gateway.store = SimpleNamespace(
            error=None,
            close_session=Mock(),
            record_event=Mock(),
            settings=AsyncMock(return_value={"budget": {"limit": 5, "used": 0}}),
        )
        await self.command("set_mode", mode="ACTIVE", command_id="activate")
        await asyncio.sleep(0)
        generation, stream = self.s.generation, self.s.up_stream
        await self.command("set_mode", mode="ACTIVE", command_id="activate")
        self.assertEqual((self.s.generation, self.s.up_stream), (generation, stream))
        state = self.gateway.product_state(self.s)
        self.assertEqual(state["confirmed_mode"], "ACTIVE")
        self.assertIsNone(state["physical"]["microphone"])
        self.assertEqual(self.s.command_acks["activate"]["status"], "applied")
        await self.command("set_mode", mode="OFF", command_id="stop")
        self.assertEqual(self.s.mode, "OFF")

    async def test_missing_budget_rejects_without_microphone_or_paid_request(self):
        self.gateway.store = SimpleNamespace(
            error=None,
            close_session=Mock(),
            record_event=Mock(),
            settings=AsyncMock(return_value={"budget": {"limit": None, "used": 0}}),
        )
        await self.command("set_mode", mode="PASSIVE", command_id="budget")
        self.assertEqual(self.s.mode, "OFF")
        self.assertEqual(self.s.command_acks["budget"]["error"], "NOMINAL_BUDGET_REQUIRED")
        self.assertFalse(self.voice.armed)
        self.assertEqual(self.requests, [])

    async def test_physical_state_expires_and_old_playback_is_ignored(self):
        self.s.mode, self.s.down_stream = "ACTIVE", 21
        await self.command(
            "client_state", microphone=False, playing=True, playback_frames=960, stream_id=21
        )
        state = self.gateway.product_state(self.s)
        self.assertEqual(state["activity"], "PLAYING")
        await self.command(
            "client_state", microphone=False, playing=True, playback_frames=9000, stream_id=20
        )
        self.assertEqual(self.s.playback_frames, 960)
        self.s.physical_at -= 4
        state = self.gateway.product_state(self.s)
        self.assertIsNone(state["physical"]["playing"])
        self.assertEqual(state["connection"], "DEGRADED")

    async def test_archive_uses_capture_generation_and_only_confirmed_prefix(self):
        record = Mock()
        self.gateway.store = SimpleNamespace(record_turn=record, close_session=Mock())
        self.gateway.record_owner = ("test", self.s.id, "turn", 3)
        self.gateway.record_turn(
            "turn", "Jarvis question", "futur entier", "préfixe", {"status": "CANCELLED"}
        )
        self.assertEqual(record.call_args.kwargs["generation"], 3)
        self.assertEqual(record.call_args.kwargs["status"], "interrupted")
        self.assertEqual(record.call_args.kwargs["delivered_text"], "préfixe")

    async def test_envelope_is_bounded_and_does_not_change_pcm(self):
        samples = np.concatenate((np.zeros(960, np.float32), np.full(1920, 0.1, np.float32)))
        outputs = []
        for animation in (False, True):
            self.s.mode, self.s.up_stream = "ACTIVE", 17
            self.s.capabilities = {"playback_envelope_v1"} if animation else set()
            output = RemoteEchoEgress(self.s, self.settings, "mouth", 24000)
            await output.start()
            self.endpoint.packets.clear()
            await output._output(samples, time.perf_counter())
            outputs.append([p.pcm for p in self.endpoint.packets])
            if animation:
                values = list(self.s.envelope_queue)
                self.assertEqual([x[2] for x in values], [0, 960, 1920])
                self.assertEqual(values[0][3], 0)
                self.assertGreater(values[1][3], 0)
            await output.abort()
            self.assertEqual(len(self.s.envelope_queue), 0)
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
