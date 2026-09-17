"""Addressed TV dispatcher: honest speech, no tool execution, no invented ids."""

from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from jarvis_office.config import Chat, Config
from jarvis_office.deepseek import ChatError, DeepSeek
from jarvis_office.tv.intent import dispatch, parse_local
from jarvis_office.tv.protocol import empty_app
from jarvis_office.voice import VoiceLoop
from tests.test_chat import Wire, event
from tests.test_voice import FakeAudio, FakeTTS, SSEStream


def _app(installed: bool, actions: list[str]) -> dict:
    entry = empty_app("native")
    entry.update(
        installed=installed,
        actions=list(actions),
        targeted_control=True,
        state_observable=True,
    )
    return entry


class FakeHub:
    def __init__(self, device: SimpleNamespace | None = None, chat: object | None = None) -> None:
        self._on = True
        self.chat = chat
        self._device = device
        self._now = 1_700_000_000_000
        self.candidates: list[dict] = []
        self.candidate_scope: tuple[str, str, str] | None = None
        self.issued: list[dict] = []
        self.outcomes: list[dict] = []
        self._turns: set[str] = set()
        self.prepared: list[dict[str, str]] = []

    def enabled(self) -> bool:
        return self._on

    def defaults(self) -> dict[str, str]:
        return {"default_video_app": "smarttube", "default_film_app": "avt"}

    def connected(self) -> SimpleNamespace | None:
        return self._device

    async def prepare(self, *, app: str, turn_id: str) -> None:
        self.prepared.append({"app": app, "turn_id": turn_id})

    def now(self) -> int:
        return self._now

    def active_candidates(self, profile_id: str | None = None) -> list[dict]:
        del profile_id
        return list(self.candidates)

    def remember_candidates(self, items: list[dict], profile_id: str | None) -> None:
        self.candidates = items
        device = self._device
        self.candidate_scope = (
            getattr(device, "device_id", ""),
            getattr(device, "server_epoch", "") or "",
            profile_id or "",
        )

    def invalidate_candidates(self) -> None:
        self.candidates = []
        self.candidate_scope = None

    async def issue(self, **kwargs: object) -> dict:
        command = {
            "command_id": str(uuid.uuid4()),
            "expires_at_ms": self._now + 10_000,
            **kwargs,
        }
        self.issued.append(command)
        return command

    async def wait_result(self, command_id: str, turn_id: str, timeout_s: float) -> dict:
        del command_id, timeout_s
        if turn_id not in self._turns and self._turns:
            return {"status": "unknown", "error_code": "DISCONNECTED", "result": None}
        if self.outcomes:
            return self.outcomes.pop(0)
        return {"status": "dispatched", "error_code": None, "result": None, "playback": None}


def _device(**kwargs: object) -> SimpleNamespace:
    playback = kwargs.pop("playback", None)
    return SimpleNamespace(
        device_id="11111111-1111-4111-8111-111111111111",
        server_epoch="22222222-2222-4222-8222-222222222222",
        apps={
            "smarttube": _app(
                True, ["play_content", "get_state", "pause", "resume", "stop", "seek"]
            ),
            "avt": _app(False, []),
        },
        playback=playback,
        **kwargs,
    )


class TvVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_command_prepares_tv_before_dispatch(self) -> None:
        hub = FakeHub(_device())
        speech = await dispatch(hub, "joue la video aqz-KE-bpKQ", "turn1")
        self.assertIn("sans confirmation", speech or "")
        self.assertEqual(hub.prepared, [{"app": "smarttube", "turn_id": "turn1"}])

    async def test_unaddressed_local_parser_does_not_invent_search(self) -> None:
        self.assertIsNone(parse_local("mets la table", FakeHub()))
        self.assertEqual(parse_local("pause", FakeHub())["action"], "pause")
        self.assertEqual(parse_local("lance interstellar", FakeHub())["action"], "search")
        self.assertEqual(
            parse_local("mets la playliste numéro 1", FakeHub()),
            {
                "kind": "command",
                "app": "smarttube",
                "action": "playlist_play",
                "args": {"index": 0},
            },
        )
        self.assertEqual(parse_local("suivante", FakeHub())["action"], "playlist_next")
        self.assertEqual(parse_local("mets la playlist une", FakeHub())["args"]["index"], 0)
        self.assertNotEqual(
            (parse_local("semaine suivante", FakeHub()) or {}).get("action"), "playlist_next"
        )
        for phrase in (
            "je mette Petunia de Werenoi sur la télé",
            "mets-moi Petunia de Werenoi sur SmartTube",
            "trouve Petunia de Werenoi sur YouTube",
            "je veux regarder Petunia de Werenoi sur SmartTube",
            "peux-tu lancer Petunia de Werenoi sur ma TV",
        ):
            parsed = parse_local(phrase, FakeHub())
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["args"]["query"], "petunia de werenoi")

    async def test_disabled_or_disconnected_or_unsupported(self) -> None:
        hub = FakeHub(_device())
        hub._on = False
        self.assertIsNone(await dispatch(hub, "pause", "turn1"))
        hub._on = True
        hub._device = None
        self.assertIn("pas connectée", await dispatch(hub, "pause", "turn1") or "")
        hub._device = _device()
        with patch("jarvis_office.tv.intent.search_smarttube", new=AsyncMock(return_value=[])):
            speech = await dispatch(hub, "cherche interstellar sur youtube", "turn1")
        self.assertIn("Aucun titre", speech or "")
        self.assertEqual(hub.issued, [])
        speech = await dispatch(hub, "cherche Dune sur avt", "turn1")
        self.assertIn("AVT n'est pas lié", speech or "")

    async def test_stale_target_ambiguous_unique_search_and_youtube_id(self) -> None:
        hub = FakeHub(_device())
        self.assertIn("lecture fraîche", await dispatch(hub, "pause", "t") or "")
        hub._device = _device(
            playback={
                "app": "smarttube",
                "playback_id": "p1",
                "position_ms": 5000,
                "observed_at_ms": hub.now(),
                "valid_for_ms": 5000,
                "state": "playing",
            }
        )
        hub._device.apps["smarttube"]["actions"] = ["pause", "play_content", "get_state"]
        self.assertEqual(await dispatch(hub, "pause", "t"), "")
        hub._device.apps["smarttube"]["actions"] = ["play_content"]
        hub.outcomes = [
            {"status": "dispatched", "error_code": None, "result": None, "playback": None},
            {"status": "dispatched", "error_code": None, "result": None, "playback": None},
        ]
        search = AsyncMock(
            side_effect=[
                [
                    {
                        "title": "Un",
                        "content": {"kind": "youtube_video", "id": "aaaaaaaaaaa"},
                    },
                    {
                        "title": "Deux",
                        "content": {"kind": "youtube_video", "id": "bbbbbbbbbbb"},
                    },
                ],
                [
                    {
                        "title": "Seul",
                        "content": {"kind": "youtube_video", "id": "ccccccccccc"},
                    }
                ],
            ]
        )
        with patch("jarvis_office.tv.intent.search_smarttube", new=search):
            speech = await dispatch(hub, "cherche un film sur youtube", "t")
        self.assertIn("sans confirmation", speech or "")
        self.assertEqual(len(hub.candidates), 2)
        self.assertEqual(hub.issued[-1]["args"]["content"]["id"], "aaaaaaaaaaa")
        with patch("jarvis_office.tv.intent.search_smarttube", new=search):
            speech = await dispatch(hub, "cherche Seul sur youtube", "t")
        self.assertIn("sans confirmation", speech or "")
        self.assertEqual(hub.issued[-1]["action"], "play_content")
        self.assertEqual(hub.issued[-1]["args"]["content"]["id"], "ccccccccccc")
        speech = await dispatch(hub, "joue la video aqz-KE-bpKQ", "t")
        self.assertEqual(hub.issued[-1]["args"]["content"]["id"], "aqz-KE-bpKQ")
        self.assertIn("sans confirmation", speech or "")

    async def test_extract_refuses_invented_id_and_cancel_is_unknown(self) -> None:
        class Chat:
            async def collect_text(self, *_a: object, **_k: object) -> str:
                return json.dumps(
                    {
                        "kind": "command",
                        "action": "play_content",
                        "app": "smarttube",
                        "content": {"kind": "youtube_video", "id": "invented111"},
                    }
                )

        hub = FakeHub(_device(), chat=Chat())
        speech = await dispatch(hub, "cette chanson à la télé s'il te plaît", "t")
        self.assertIn("identifiant YouTube inventé", speech or "")
        self.assertEqual(hub.issued, [])

        class SlowHub(FakeHub):
            async def wait_result(self, command_id: str, turn_id: str, timeout_s: float) -> dict:
                del command_id, timeout_s
                deadline = asyncio.get_running_loop().time() + 0.3
                while asyncio.get_running_loop().time() < deadline:
                    if turn_id not in self._turns:
                        return {"status": "unknown", "error_code": "DISCONNECTED", "result": None}
                    await asyncio.sleep(0.01)
                return {"status": "dispatched", "error_code": None, "result": None}

        slow = SlowHub(
            _device(
                playback={
                    "app": "smarttube",
                    "playback_id": "p1",
                    "position_ms": 0,
                    "observed_at_ms": 1_700_000_000_000,
                    "valid_for_ms": 5000,
                    "state": "playing",
                }
            )
        )
        slow._turns.add("live")
        task = asyncio.create_task(dispatch(slow, "pause", "live"))
        await asyncio.sleep(0.02)
        slow._turns.discard("live")
        self.assertIn("pas de confirmation", await task or "")

    async def test_short_transport_commands_are_silent(self) -> None:
        hub = FakeHub(
            _device(
                playback={
                    "app": "smarttube",
                    "playback_id": "p1",
                    "position_ms": 5000,
                    "observed_at_ms": 1_700_000_000_000,
                    "valid_for_ms": 5000,
                    "state": "playing",
                }
            )
        )
        for phrase in ("pause", "reprends", "arrete", "avance de 30 secondes"):
            with self.subTest(phrase=phrase):
                self.assertEqual(await dispatch(hub, phrase, "turn1"), "")
        stale = FakeHub(_device())
        self.assertIn("lecture fraîche", await dispatch(stale, "pause", "turn1") or "")

    async def test_voice_loop_silent_tv_command_skips_llm_and_tts(self) -> None:
        streams: list[SSEStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            stream = SSEStream(["Réponse conversation. "])
            streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        audio, tts = FakeAudio(), FakeTTS()
        chat = DeepSeek("test_key_only", Chat(), transport=httpx.MockTransport(handler))
        voice = VoiceLoop(Config(), Path("unused.toml"), chat, audio=audio, tts=tts)
        await voice.start()

        class SilentTv:
            async def handle_addressed(self, question: str, turn_id: str) -> str | None:
                del turn_id
                return "" if "pause" in question.lower() else None

            def cancel_turn(self, turn_id: str) -> None:
                del turn_id

            def clear_dialogue(self) -> None:
                return None

        voice.tv = SilentTv()
        try:
            await voice.test_text("Jarvis, mets pause", no_play=True)
            self.assertEqual(tts.texts, [])
            self.assertEqual(streams, [])
            self.assertEqual(voice.answer, "")
            self.assertEqual(voice.metrics["status"], "PASS")
            self.assertEqual(voice.metrics["llm"], {"path": "tv_dispatcher", "spoken": False})
            self.assertIsNone(voice.error)
            self.assertEqual(chat.history, [])
        finally:
            await voice.control("stop")

    async def test_voice_loop_tv_intercept_skips_llm_and_unaddressed_never_hits_tv(self) -> None:
        streams: list[SSEStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            stream = SSEStream(["Réponse conversation. "])
            streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        chat = DeepSeek("test_key_only", Chat(), transport=httpx.MockTransport(handler))
        audio, tts = FakeAudio(), FakeTTS()
        voice = VoiceLoop(Config(), Path("unused.toml"), chat, audio=audio, tts=tts)
        await voice.start()
        seen: list[str] = []

        class Tv:
            async def handle_addressed(self, question: str, turn_id: str) -> str | None:
                del turn_id
                seen.append(question)
                if "pause" in question.lower():
                    return "Pause demandée."
                return None

            def cancel_turn(self, turn_id: str) -> None:
                del turn_id

            def clear_dialogue(self) -> None:
                return None

        voice.tv = Tv()
        try:
            await voice._respond("Je cite Jarvis.", "test")
            self.assertEqual(seen, [])
            self.assertEqual(streams, [])
            await voice.test_text("Jarvis, mets pause", no_play=True)
            self.assertEqual(voice.answer, "Pause demandée.")
            self.assertEqual(len(streams), 0)
            self.assertEqual(chat.history, [])
            await voice.test_text("Jarvis, explique la suite.", no_play=True)
            self.assertEqual(len(streams), 1)
            self.assertIn("conversation", voice.answer.lower())
        finally:
            await voice.control("stop")

    async def test_collect_text_still_rejects_tool_calls(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Wire([event(delta={"tool_calls": [{"id": "x"}]})]),
            )

        chat = DeepSeek("test_key_only", Chat(), transport=httpx.MockTransport(handler))
        with self.assertRaises(ChatError) as raised:
            await chat.collect_text(
                "pause",
                turn_id="tv-test",
                prepared_messages=[
                    {"role": "system", "content": "json"},
                    {"role": "user", "content": "pause"},
                ],
            )
        self.assertEqual(str(raised.exception), "unexpected_tool_call")


if __name__ == "__main__":
    unittest.main()
