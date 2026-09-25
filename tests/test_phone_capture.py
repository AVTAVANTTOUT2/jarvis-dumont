"""Real phone HTTP routes: one sender and one ordered acquisition, no engines."""

import json
import unittest
from types import SimpleNamespace

from jarvis_office.local_ui import LocalUI
from jarvis_office.web_audio import FRAME_BYTES, WebPhoneAudio


class PhoneRoutesTests(unittest.TestCase):
    def setUp(self):
        self.frames = []
        worker = SimpleNamespace(
            event=lambda item: None,
            feed_remote=lambda token, pcm, received: self.frames.append((token, pcm)),
        )
        self.phone = WebPhoneAudio(worker)
        self.state = {"state": "paused", "armed": False, "microphone": "closed"}
        self.voice = SimpleNamespace(audio=self.phone, snapshot=lambda: dict(self.state))
        self.ui = LocalUI(self.voice, 8768)
        self.headers = {
            "host": self.ui.host,
            "origin": self.ui.origin,
            "sec-fetch-site": "same-origin",
            "x-jarvis-local": "1",
            "content-type": "application/json",
        }
        self.first = self.open("elias", "1" * 32)
        self.second = self.open("aymen", "2" * 32)

    def open(self, seat, client):
        status, _, extra = self.ui.route("POST", "/bootstrap", self.headers, b"{}")
        self.assertEqual(status, 200)
        auth = {
            **self.headers,
            "cookie": extra["Set-Cookie"].split(";", 1)[0],
            "x-jarvis-client": client,
        }
        self.assertEqual(self.request("/crew/claim", auth, {"id": seat})[0], 200)
        return auth

    def request(self, path, auth, data):
        return self.ui.route("POST", path, auth, json.dumps(data).encode())

    def listen(self):
        self.assertEqual(self.request("/control", self.first, {"action": "resume"})[0], 202)
        self.ui.controls.get_nowait()
        self.state.update(state="listening", armed=True, microphone="open")
        self.state["input"] = {"ingress_token": "a" * 32, "sample_rate": 16000}
        self.phone._on_event(
            {"event": "listening", "data": {"device": {"ingress_token": "a" * 32}}}
        )

    def upload(self, auth, *, capture="a" * 32, sequence="0", body=None):
        return self.ui.route(
            "POST",
            "/uplink",
            {
                **auth,
                "content-type": "application/octet-stream",
                "x-jarvis-capture": capture,
                "x-jarvis-sequence": sequence,
            },
            body if body is not None else b"\x01" * FRAME_BYTES,
        )[0]

    def test_second_microphone_and_same_cookie_other_tab_cannot_feed(self):
        self.listen()
        self.assertEqual(self.upload(self.second), 409)
        other_tab = {**self.first, "x-jarvis-client": "3" * 32}
        self.assertEqual(self.upload(other_tab), 409)
        self.assertEqual(self.frames, [])
        self.assertEqual(self.upload(self.first), 200)
        self.assertEqual(self.frames, [("a" * 32, b"\x01" * FRAME_BYTES)])

    def test_capture_identifier_is_only_disclosed_to_owner(self):
        self.listen()
        owner = json.loads(self.request("/snapshot", self.first, {})[1])["phone_audio"]
        guest = json.loads(self.request("/snapshot", self.second, {})[1])["phone_audio"]
        self.assertEqual(owner["capture"], "a" * 32)
        self.assertTrue(owner["owned"])
        self.assertEqual(guest["capture"], "")
        self.assertFalse(guest["owned"])
        self.assertNotIn(b"ingress_token", self.request("/snapshot", self.second, {})[1])

    def test_pending_resume_cannot_be_stolen(self):
        self.assertEqual(self.request("/control", self.first, {"action": "resume"})[0], 202)
        self.assertEqual(self.request("/control", self.second, {"action": "resume"})[0], 409)

    def test_second_tab_bootstrap_cannot_release_or_change_active_microphone_owner(self):
        self.listen()
        other_tab = {**self.first, "x-jarvis-client": "3" * 32}
        self.assertEqual(self.request("/crew/release", other_tab, {})[0], 409)
        self.assertEqual(self.request("/crew/claim", other_tab, {"id": "faiz"})[0], 409)
        self.assertTrue(self.ui.controls.empty())
        self.assertEqual(self.upload(self.first), 200)

    def test_old_capture_rejected_without_stopping_current_take(self):
        self.listen()
        self.assertEqual(self.upload(self.first, capture="b" * 32), 409)
        self.assertTrue(self.ui.controls.empty())
        self.assertEqual(self.upload(self.first), 200)

    def test_gap_stops_capture_instead_of_transcribing_spliced_audio(self):
        self.listen()
        self.assertEqual(self.upload(self.first, sequence="1"), 409)
        self.assertEqual(self.frames, [])
        self.assertEqual(self.phone.token, "")
        self.assertEqual(self.ui.controls.get_nowait(), "pause")

    def test_ordered_batch_reaches_worker_as_distinct_frames(self):
        self.listen()
        pcm = b"\x01" * FRAME_BYTES + b"\x02" * FRAME_BYTES
        self.assertEqual(self.upload(self.first, body=pcm), 200)
        self.assertEqual(self.upload(self.first, sequence="2"), 200)
        self.assertEqual([pcm[0] for _, pcm in self.frames], [1, 2, 1])

    def test_other_browser_cannot_consume_or_acknowledge_response(self):
        self.listen()
        self.phone.sink.begin("reply-1", 16000)
        self.phone.sink.feed("reply-1", b"\x01" * FRAME_BYTES)
        self.assertEqual(self.request("/pcm", self.second, {})[0], 409)
        self.assertEqual(
            self.request("/heard", self.second, {"bytes": FRAME_BYTES, "done": False})[0], 409
        )
        self.assertEqual(self.phone.sink.pulled, 0)
        self.assertEqual(self.phone.sink.acked, 0)
        self.assertEqual(self.request("/pcm", self.first, {})[0], 200)
        self.assertEqual(self.phone.sink.pulled, FRAME_BYTES)

    def test_pause_revokes_capture_immediately(self):
        self.listen()
        self.assertEqual(self.request("/control", self.first, {"action": "pause"})[0], 202)
        self.assertEqual(self.upload(self.first), 409)
        self.assertEqual(self.frames, [])

    def test_expired_owner_stops_capture_before_another_session_can_feed(self):
        self.listen()
        owner = self.ui.phone_owner
        self.assertIsNotNone(owner)
        del self.ui.talk.sessions[owner[0]]
        self.assertEqual(self.upload(self.second), 409)
        self.assertEqual(self.phone.token, "")
        self.assertEqual(self.ui.controls.get_nowait(), "pause")
        self.assertEqual(self.frames, [])

    def test_no_profile_or_missing_tab_identity_cannot_start_capture(self):
        missing = {**self.first, "x-jarvis-client": ""}
        self.assertEqual(self.request("/control", missing, {"action": "resume"})[0], 409)
        self.assertEqual(self.request("/crew/release", self.first, {})[0], 200)
        self.assertEqual(self.request("/control", self.first, {"action": "resume"})[0], 409)
        self.assertTrue(self.ui.controls.empty())

    def test_owner_release_and_intercom_start_invalidate_jarvis_capture(self):
        self.listen()
        self.assertEqual(self.request("/talk/call", self.second, {"peer": "elias"})[0], 200)
        self.assertEqual(self.phone.token, "")
        self.assertIsNone(self.ui.phone_owner)
        self.assertEqual(self.ui.controls.get_nowait(), "pause")


if __name__ == "__main__":
    unittest.main()
