"""TalkBridge occupancy, one call, bounded PCM relay. No hardware."""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace

from jarvis_office.local_ui import LocalUI
from jarvis_office.talk import (
    MAX_SESSIONS,
    SEATS,
    TALK_BATCH_BYTES,
    TALK_CAPACITY,
    TalkBridge,
    TalkError,
)
from jarvis_office.web_audio import FRAME_BYTES


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TalkBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.talk = TalkBridge(now=self.clock)

    def _pair(self) -> tuple[str, str]:
        elias = self.talk.register()
        aymen = self.talk.register()
        self.talk.claim(elias, "elias")
        self.talk.claim(aymen, "aymen")
        return elias, aymen

    def test_claim_is_exclusive_and_unknown_ids_fail(self) -> None:
        first, second = self.talk.register(), self.talk.register()
        self.talk.claim(first, "elias")
        with self.assertRaises(TalkError) as taken:
            self.talk.claim(second, "elias")
        self.assertEqual(str(taken.exception), "seat_taken")
        with self.assertRaises(TalkError) as unknown:
            self.talk.claim(first, "jarvis")
        self.assertEqual(str(unknown.exception), "unknown_seat")
        self.talk.claim(first, "aymen")
        self.talk.claim(second, "elias")
        self.assertEqual(self.talk.view(first)["me"], "aymen")
        self.assertEqual(self.talk.view(second)["me"], "elias")

    def test_sixth_session_is_refused_until_stale_seats_expire(self) -> None:
        tokens = [self.talk.register() for _ in range(MAX_SESSIONS)]
        self.assertEqual(len(tokens), 5)
        with self.assertRaises(TalkError) as limited:
            self.talk.register()
        self.assertEqual(str(limited.exception), "session_limit")
        self.clock.t = 5.1
        sixth = self.talk.register()
        self.assertEqual(len(self.talk.sessions), 1)
        self.assertIn(sixth, self.talk.sessions)

    def test_call_needs_an_online_peer_and_rejects_a_second_pair(self) -> None:
        elias, aymen = self._pair()
        with self.assertRaises(TalkError) as offline:
            self.talk.start_call(elias, "faiz")
        self.assertEqual(str(offline.exception), "peer_offline")
        with self.assertRaises(TalkError) as self_call:
            self.talk.start_call(elias, "elias")
        self.assertEqual(str(self_call.exception), "unknown_seat")
        self.talk.start_call(elias, "aymen")
        faiz = self.talk.register()
        self.talk.claim(faiz, "faiz")
        with self.assertRaises(TalkError) as busy:
            self.talk.start_call(faiz, "elias")
        self.assertEqual(str(busy.exception), "call_busy")

    def test_feed_reaches_peer_and_overflow_ends_call_and_empties_queue(self) -> None:
        elias, aymen = self._pair()
        self.talk.start_call(elias, "aymen")
        frame = b"\x01\x02" * (FRAME_BYTES // 2)
        self.talk.feed(elias, frame, self.talk.call_id)
        pulled = self.talk.pull(aymen, self.talk.call_id)
        self.assertEqual(pulled["rate"], 16000)
        self.assertEqual(base64.b64decode(pulled["pcm"]), frame)
        self.assertEqual(self.talk.pull(elias, self.talk.call_id)["pcm"], "")
        filled = TALK_CAPACITY // FRAME_BYTES
        for _ in range(filled):
            self.talk.feed(elias, frame, self.talk.call_id)
        with self.assertRaises(TalkError) as pressure:
            self.talk.feed(elias, frame, self.talk.call_id)
        self.assertEqual(str(pressure.exception), "remote_pcm_backpressure")
        self.assertIsNone(self.talk.call)
        self.assertEqual(self.talk.sessions[aymen].downlink, b"")
        self.assertEqual(self.talk.view(aymen)["error"], "audio_stalled")

    def test_expired_presence_hangs_up_and_lists_seats(self) -> None:
        elias, aymen = self._pair()
        self.talk.start_call(elias, "aymen")
        self.clock.t = 5.1
        view = self.talk.view(elias)
        self.assertIsNone(self.talk.call)
        self.assertIsNone(view["me"])
        self.assertEqual(set(view["seats"]), set(SEATS))  # type: ignore[arg-type]
        self.assertTrue(all(seat["online"] is False for seat in view["seats"].values()))  # type: ignore[union-attr,index]

    def test_release_frees_the_seat_and_hangs_up(self) -> None:
        elias, aymen = self._pair()
        self.talk.start_call(elias, "aymen")
        self.talk.release(elias)
        self.assertIsNone(self.talk.call)
        view = self.talk.view(aymen)
        self.assertIsNone(self.talk.view(elias)["me"])
        self.assertFalse(view["seats"]["elias"]["online"])  # type: ignore[index]
        other = self.talk.register()
        self.talk.claim(other, "elias")
        self.assertEqual(self.talk.view(other)["me"], "elias")

    def test_release_rejects_an_unknown_session(self) -> None:
        with self.assertRaises(TalkError) as unknown:
            self.talk.release("absent")
        self.assertEqual(str(unknown.exception), "unknown_session")


class LocalUITalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.voice = SimpleNamespace(
            snapshot=lambda: {
                "state": "paused",
                "session": "s1",
                "armed": False,
                "error": None,
                "microphone": "closed",
                "level": 0,
            },
            audio=None,
        )
        self.ui = LocalUI(self.voice, 8768)
        self.headers = {
            "host": self.ui.host,
            "origin": self.ui.origin,
            "sec-fetch-site": "same-origin",
            "x-jarvis-local": "1",
            "content-type": "application/json",
        }

    def _open(self) -> str:
        status, _, extra = self.ui.route("POST", "/bootstrap", self.headers, b"{}")
        self.assertEqual(status, 200)
        return extra["Set-Cookie"].split(";", 1)[0]

    def _auth(self, cookie: str) -> dict[str, str]:
        return {**self.headers, "cookie": cookie}

    def test_talk_routes_need_the_session_cookie(self) -> None:
        cookie = self._open()
        auth = self._auth(cookie)
        evil = {**auth, "origin": "https://evil.test"}
        self.assertEqual(self.ui.route("POST", "/crew/claim", evil, b'{"id":"elias"}')[0], 403)
        self.assertEqual(self.ui.route("POST", "/talk/call", evil, b'{"peer":"aymen"}')[0], 403)
        self.assertEqual(
            self.ui.route("POST", "/talk/hangup", {**auth, "cookie": ""}, b"{}")[0], 403
        )
        self.assertEqual(
            self.ui.route(
                "POST",
                "/talk/uplink",
                {**evil, "content-type": "application/octet-stream"},
                b"\x00" * FRAME_BYTES,
            )[0],
            403,
        )
        self.assertEqual(self.ui.route("POST", "/crew/claim", auth, b'{"id":"elias"}')[0], 200)
        self.assertEqual(self.ui.route("POST", "/crew/release", evil, b"{}")[0], 403)
        self.assertEqual(self.ui.route("POST", "/crew/release", auth, b'{"id":"elias"}')[0], 400)
        self.assertEqual(self.ui.route("POST", "/crew/release", auth, b"{}")[0], 200)
        status, payload, _ = self.ui.route("POST", "/snapshot", auth, b"{}")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(payload)["crew"]["me"])

    def test_snapshot_carries_crew_and_resume_is_conflict_during_a_call(self) -> None:
        elias = self._auth(self._open())
        aymen = self._auth(self._open())
        self.assertEqual(self.ui.route("POST", "/crew/claim", elias, b'{"id":"elias"}')[0], 200)
        self.assertEqual(self.ui.route("POST", "/crew/claim", aymen, b'{"id":"aymen"}')[0], 200)
        status, payload, _ = self.ui.route("POST", "/snapshot", elias, b"{}")
        self.assertEqual(status, 200)
        snap = json.loads(payload)
        self.assertEqual(snap["crew"]["me"], "elias")
        self.assertTrue(snap["crew"]["seats"]["aymen"]["online"])
        self.assertIsNone(snap["crew"]["call"])
        self.assertNotIn("<", payload.decode())
        self.assertEqual(self.ui.route("POST", "/talk/call", elias, b'{"peer":"aymen"}')[0], 200)
        self.assertEqual(self.ui.controls.get_nowait(), "pause")
        self.assertEqual(self.ui.route("POST", "/control", elias, b'{"action":"resume"}')[0], 409)
        jarvis = {**elias, "content-type": "application/octet-stream"}
        self.assertEqual(self.ui.route("POST", "/uplink", jarvis, b"\x00" * FRAME_BYTES)[0], 200)
        elias["x-jarvis-call"] = aymen["x-jarvis-call"] = self.ui.talk.call_id
        frame = b"\x03\x04" * (FRAME_BYTES // 2)
        self.assertEqual(
            self.ui.route(
                "POST",
                "/talk/uplink",
                {**elias, "content-type": "application/octet-stream"},
                frame,
            )[0],
            200,
        )
        status, body, _ = self.ui.route("POST", "/talk/pcm", aymen, b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(base64.b64decode(json.loads(body)["pcm"]), frame)
        self.assertEqual(self.ui.route("POST", "/talk/hangup", elias, b"{}")[0], 200)
        self.assertEqual(self.ui.route("POST", "/control", elias, b'{"action":"resume"}')[0], 202)

    def test_sixth_bootstrap_is_rejected(self) -> None:
        for _ in range(5):
            self.assertEqual(self._open().startswith(self.ui.cookie_name), True)
        status, _, extra = self.ui.route("POST", "/bootstrap", self.headers, b"{}")
        self.assertEqual(status, 429)
        self.assertNotIn("Set-Cookie", extra)

    def _connected_pair(self) -> tuple[dict[str, str], dict[str, str]]:
        first, second = self._auth(self._open()), self._auth(self._open())
        self.ui.route("POST", "/crew/claim", first, b'{"id":"elias"}')
        self.ui.route("POST", "/crew/claim", second, b'{"id":"aymen"}')
        self.assertEqual(self.ui.route("POST", "/talk/call", first, b'{"peer":"aymen"}')[0], 200)
        _, payload, _ = self.ui.route("POST", "/snapshot", first, b"{}")
        call = json.loads(payload)["crew"]["call"]
        return (
            {**first, "x-jarvis-call": call.get("id", "")},
            {**second, "x-jarvis-call": call.get("id", "")},
        )

    def test_microphone_batches_are_relayed_in_both_directions(self) -> None:
        first, second = self._connected_pair()
        frames = b"".join(bytes([n, 0]) * 320 for n in range(1, 6))
        for sender, receiver in ((first, second), (second, first)):
            status, _, _ = self.ui.route(
                "POST",
                "/talk/uplink",
                {**sender, "content-type": "application/octet-stream"},
                frames,
            )
            self.assertEqual(status, 200)
            status, payload, _ = self.ui.route("POST", "/talk/pcm", receiver, b"{}")
            self.assertEqual(status, 200)
            self.assertEqual(base64.b64decode(json.loads(payload)["pcm"]), frames)

    def test_old_call_requests_cannot_touch_the_next_call(self) -> None:
        first, second = self._connected_pair()
        self.assertEqual(self.ui.route("POST", "/talk/hangup", first, b"{}")[0], 200)
        self.assertEqual(self.ui.route("POST", "/talk/call", first, b'{"peer":"aymen"}')[0], 200)
        self.assertEqual(
            self.ui.route(
                "POST",
                "/talk/uplink",
                {**first, "content-type": "application/octet-stream"},
                b"\x01\x00" * 320,
            )[0],
            409,
        )
        self.assertEqual(self.ui.route("POST", "/talk/pcm", second, b"{}")[0], 409)
        self.assertEqual(self.ui.route("POST", "/talk/hangup", first, b"{}")[0], 409)
        self.assertIsNotNone(self.ui.talk.call)

    def test_stalled_audio_ends_call_instead_of_replaying_old_words(self) -> None:
        clock = Clock()
        self.ui.talk = TalkBridge(now=clock)
        first, second = self._connected_pair()
        self.assertEqual(
            self.ui.route(
                "POST",
                "/talk/uplink",
                {**first, "content-type": "application/octet-stream"},
                b"\x01\x00" * 320,
            )[0],
            200,
        )
        clock.t = 0.6
        status, payload, _ = self.ui.route("POST", "/talk/pcm", second, b"{}")
        self.assertEqual(status, 409)
        self.assertNotIn("pcm", json.loads(payload))
        self.assertIsNone(self.ui.talk.call)

    def test_bootstrap_reuses_active_cookie_without_losing_the_call(self) -> None:
        first, _ = self._connected_pair()
        status, _, extra = self.ui.route("POST", "/bootstrap", first, b"{}")
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", extra)
        self.assertEqual(len(self.ui.talk.sessions), 2)
        _, payload, _ = self.ui.route("POST", "/snapshot", first, b"{}")
        self.assertEqual(json.loads(payload)["crew"]["me"], "elias")

    def test_a_third_member_cannot_read_send_or_hang_up_the_call(self) -> None:
        first, second = self._connected_pair()
        outsider = {**self._auth(self._open()), "x-jarvis-call": first["x-jarvis-call"]}
        self.ui.route("POST", "/crew/claim", outsider, b'{"id":"faiz"}')
        for path, body, content_type in (
            ("/talk/pcm", b"{}", "application/json"),
            ("/talk/hangup", b"{}", "application/json"),
            ("/talk/uplink", b"\x01\x00" * 320, "application/octet-stream"),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.ui.route("POST", path, {**outsider, "content-type": content_type}, body)[
                        0
                    ],
                    409,
                )
        self.assertEqual(self.ui.talk.call_id, first["x-jarvis-call"])
        _, payload, _ = self.ui.route("POST", "/talk/pcm", second, b"{}")
        self.assertEqual(json.loads(payload)["pcm"], "")

    def test_hangup_clears_both_directions_before_the_next_call(self) -> None:
        first, second = self._connected_pair()
        for sender in (first, second):
            self.ui.route(
                "POST",
                "/talk/uplink",
                {
                    **sender,
                    "content-type": "application/octet-stream",
                },
                b"\x01\x00" * 320,
            )
        self.assertEqual(self.ui.route("POST", "/talk/hangup", second, b"{}")[0], 200)
        self.ui.route("POST", "/talk/call", first, b'{"peer":"aymen"}')
        for receiver in (first, second):
            _, payload, _ = self.ui.route(
                "POST",
                "/talk/pcm",
                {
                    **receiver,
                    "x-jarvis-call": self.ui.talk.call_id,
                },
                b"{}",
            )
            self.assertEqual(json.loads(payload)["pcm"], "")


class TalkHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ui = LocalUI(SimpleNamespace(audio=None), 8768)
        self.first, self.second = self.ui.talk.register(), self.ui.talk.register()
        self.ui.talk.claim(self.first, "elias")
        self.ui.talk.claim(self.second, "aymen")
        self.ui.talk.start_call(self.first, "aymen")

    async def _post(self, path: str, body: bytes, token: str) -> bytes:
        headers = {
            "Host": self.ui.host,
            "Origin": self.ui.origin,
            "Sec-Fetch-Site": "same-origin",
            "X-Jarvis-Local": "1",
            "X-Jarvis-Call": self.ui.talk.call_id,
            "Cookie": f"{self.ui.cookie_name}={token}",
            "Content-Type": "application/json"
            if path == "/talk/pcm"
            else "application/octet-stream",
            "Content-Length": str(len(body)),
        }
        raw = f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        reader = asyncio.StreamReader()
        reader.feed_data(raw.encode() + b"\r\n" + body)
        reader.feed_eof()

        class Writer:
            data = b""

            def write(self, data: bytes) -> None:
                self.data += data

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        writer = Writer()
        await self.ui.handle(reader, writer)  # type: ignore[arg-type]
        return writer.data

    async def test_parser_accepts_maximum_batch_and_preserves_duplex_pcm(self) -> None:
        for sender, receiver, value in ((self.first, self.second, 1), (self.second, self.first, 2)):
            batch = bytes([value, 0]) * (TALK_BATCH_BYTES // 2)
            self.assertTrue(
                (await self._post("/talk/uplink", batch, sender)).startswith(b"HTTP/1.1 200 ")
            )
            raw = await self._post("/talk/pcm", b"{}", receiver)
            self.assertTrue(raw.startswith(b"HTTP/1.1 200 "))
            data = json.loads(raw.split(b"\r\n\r\n", 1)[1])
            self.assertEqual(base64.b64decode(data["pcm"]), batch)
            self.assertEqual(data["call"], self.ui.talk.call_id)

    async def test_oversized_and_misaligned_pcm_are_rejected_without_relay(self) -> None:
        for path, amount in (
            ("/talk/uplink", TALK_BATCH_BYTES + FRAME_BYTES),
            ("/talk/uplink", FRAME_BYTES + 2),
            ("/talk/uplink", 0),
            ("/unrelated-uplink", FRAME_BYTES * 2),
        ):
            with self.subTest(path=path, amount=amount):
                raw = await self._post(path, b"\x00" * amount, self.first)
                self.assertFalse(raw.startswith(b"HTTP/1.1 200 "))
                self.assertEqual(self.ui.talk.sessions[self.second].downlink, b"")
