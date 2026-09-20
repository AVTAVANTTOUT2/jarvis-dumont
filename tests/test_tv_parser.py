"""Local TV parser contract: MATCH/REJECT/AMBIGUOUS/NO_MATCH, no AI bypass."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from jarvis_office.tv.intent import dispatch, dispatch_command
from jarvis_office.tv.parser import decide_local
from tests.test_tv_voice import FakeHub, _device
from tests.tv_command_corpus import CASES, ORDINAL_INDEX, CorpusCase


class BoomChat:
    def __init__(self) -> None:
        self.calls = 0

    async def collect_text(self, *_a: object, **_k: object) -> str:
        self.calls += 1
        raise AssertionError("extract collect_text is forbidden")


class CountingChat:
    def __init__(self, payload: str) -> None:
        self.calls = 0
        self.payload = payload

    async def collect_text(self, *_a: object, **_k: object) -> str:
        self.calls += 1
        return self.payload


class ForbiddenHub(FakeHub):
    async def prepare(self, *, app: str, turn_id: str) -> None:
        raise AssertionError(f"prepare forbidden: {app}")

    async def issue(self, **kwargs: object) -> dict:
        raise AssertionError(f"issue forbidden: {kwargs.get('action')}")

    async def start_playlist(self, index: int) -> dict:
        raise AssertionError(f"start_playlist forbidden: {index}")

    async def next_playlist(self) -> dict:
        raise AssertionError("next_playlist forbidden")


def _candidates(count: int) -> list[dict]:
    items = []
    for index in range(count):
        ident = f"id{index:09d}"
        items.append(
            {
                "title": f"Item {index}",
                "content": {"kind": "youtube_video", "id": ident},
            }
        )
    return items


def _hub_for(case: CorpusCase, *, chat: object | None = None) -> FakeHub:
    hub: FakeHub = ForbiddenHub(_device(), chat=chat)
    hub.candidates = _candidates(case.candidates)
    return hub


class TvParserCorpusTests(unittest.TestCase):
    def test_corpus_decisions(self) -> None:
        self.assertGreaterEqual(len(CASES), 50)
        for case in CASES:
            with self.subTest(phrase=case.phrase, category=case.category):
                decision = decide_local(case.phrase, _hub_for(case))
                self.assertEqual(decision.kind.value, case.decision)
                self.assertEqual(decision.reason, case.reason)
                if case.decision != "match":
                    self.assertIsNone(decision.parsed)
                    continue
                assert decision.parsed is not None
                if case.parsed_kind == "pick":
                    self.assertEqual(decision.parsed["kind"], "pick")
                    expected = ORDINAL_INDEX[case.phrase]
                    self.assertEqual(decision.parsed["index"], expected)
                    continue
                self.assertEqual(decision.parsed["kind"], "command")
                self.assertEqual(decision.parsed["action"], case.action)
                if case.args is not None:
                    self.assertEqual(decision.parsed.get("args"), case.args)

    def test_corpus_case_count_is_not_the_method_count(self) -> None:
        methods = [name for name in dir(self) if name.startswith("test_")]
        self.assertEqual(len(methods), 2)
        self.assertGreater(len(CASES), len(methods))


class TvParserDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_local_does_not_call_extract(self) -> None:
        chat = BoomChat()
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
            ),
            chat=chat,
        )
        ok, speech = await dispatch_command(hub, "pause", "t")
        self.assertTrue(ok)
        self.assertEqual(speech, "")
        self.assertEqual(chat.calls, 0)
        self.assertEqual(hub.issued[-1]["action"], "pause")

    async def test_reject_skips_extract_and_tv_effects(self) -> None:
        chat = BoomChat()
        hub = _hub_for(
            CorpusCase("ne mets pas en pause", "reject", "negation", "REJECT_NEGATION", False),
            chat=chat,
        )
        with patch(
            "jarvis_office.tv.intent.search_smarttube",
            side_effect=AssertionError("search forbidden"),
        ):
            ok, speech = await dispatch_command(hub, "ne mets pas en pause", "t")
        self.assertFalse(ok)
        self.assertEqual(speech, "Commande inconnue.")
        self.assertEqual(chat.calls, 0)
        self.assertEqual(hub.issued, [])
        self.assertEqual(hub.prepared, [])

    async def test_ambiguous_skips_extract_and_tv_effects(self) -> None:
        chat = BoomChat()
        hub = _hub_for(
            CorpusCase(
                "mets la playlist", "ambiguous", "playlist", "AMBIGUOUS_PLAYLIST_INDEX", False
            ),
            chat=chat,
        )
        ok, speech = await dispatch_command(hub, "mets la playlist", "t")
        self.assertFalse(ok)
        self.assertIn("playlist", speech.lower())
        self.assertEqual(chat.calls, 0)
        self.assertEqual(hub.issued, [])
        self.assertEqual(hub.prepared, [])

    async def test_nomatch_eligible_calls_extract(self) -> None:
        payload = json.dumps({"kind": "clarify", "speech": "extract-hit"})
        chat = CountingChat(payload)
        hub = ForbiddenHub(_device(), chat=chat)
        ok, speech = await dispatch_command(hub, "cette chanson à la télé s'il te plaît", "t")
        self.assertFalse(ok)
        self.assertEqual(speech, "extract-hit")
        self.assertEqual(chat.calls, 1)

    async def test_reject_not_rescued_by_fake_llm_json(self) -> None:
        payload = json.dumps(
            {
                "kind": "command",
                "action": "pause",
                "app": "smarttube",
            }
        )
        chat = CountingChat(payload)
        hub = ForbiddenHub(
            _device(
                playback={
                    "app": "smarttube",
                    "playback_id": "p1",
                    "position_ms": 5000,
                    "observed_at_ms": 1_700_000_000_000,
                    "valid_for_ms": 5000,
                    "state": "playing",
                }
            ),
            chat=chat,
        )
        ok, speech = await dispatch_command(hub, "je fais une pause", "t")
        self.assertFalse(ok)
        self.assertEqual(chat.calls, 0)
        self.assertEqual(hub.issued, [])
        self.assertEqual(speech, "Commande inconnue.")

    async def test_dispatch_addressed_reject_is_none_without_extract(self) -> None:
        chat = BoomChat()
        hub = ForbiddenHub(_device(), chat=chat)
        self.assertIsNone(await dispatch(hub, "ne mets pas en pause", "t"))
        self.assertEqual(chat.calls, 0)

    async def test_playlist_huge_index_does_not_raise(self) -> None:
        decision = decide_local("playlist 999999", ForbiddenHub(_device()))
        self.assertEqual(decision.kind.value, "match")
        assert decision.parsed is not None
        self.assertEqual(decision.parsed["args"]["index"], 999998)

    async def test_youtube_identifier_keeps_case_and_separators(self) -> None:
        decision = decide_local("joue la video AbC_De-fgH1", ForbiddenHub(_device()))
        self.assertEqual(decision.kind.value, "match")
        assert decision.parsed is not None
        self.assertEqual(decision.parsed["args"]["content"]["id"], "AbC_De-fgH1")


if __name__ == "__main__":
    unittest.main()
