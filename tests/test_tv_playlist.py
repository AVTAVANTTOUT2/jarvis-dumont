from __future__ import annotations

import unittest
from types import SimpleNamespace

from jarvis_office.tv.hub import TvHub


def _track(track_id: str, duration_ms: int | None = 10_000) -> dict:
    return {
        "id": track_id,
        "title": track_id,
        "content": {"kind": "youtube_video", "id": track_id},
        "duration_ms": duration_ms,
    }


class PlaylistSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def _hub(
        self,
        tracks: list[dict],
        *,
        expected: str | None = None,
        ignored: str | None = None,
        started: bool = False,
    ) -> tuple[TvHub, list[dict], SimpleNamespace]:
        hub = object.__new__(TvHub)
        hub.playlists = [{"id": "playlist-1", "name": "Test", "tracks": tracks}]
        hub._playlist_state = {
            "playlist_id": "playlist-1",
            "track_index": 0,
            "device_id": "device-1",
            "status": "playing",
            "started": started,
            "command_id": "old-command",
            "expected_playback_id": expected,
            "ignored_playback_id": ignored,
        }
        hub.now = lambda: 1_700_000_000_000
        hub._bump = lambda: None
        issued: list[dict] = []

        async def issue(**kwargs: object) -> dict:
            issued.append(kwargs)
            return {"command_id": "next-command"}

        hub.issue = issue  # type: ignore[method-assign]
        device = SimpleNamespace(device_id="device-1", playback=None, pending={}, queue=[])
        hub._connected_device = lambda: device  # type: ignore[method-assign]
        return hub, issued, device

    def _playback(
        self,
        hub: TvHub,
        track: dict,
        *,
        playback_id: str,
        state: str,
        position_ms: int | None,
        duration_ms: int | None,
    ) -> dict:
        return {
            "app": "smarttube",
            "content": track["content"],
            "playback_id": playback_id,
            "state": state,
            "position_ms": position_ms,
            "duration_ms": duration_ms,
            "observed_at_ms": hub.now(),
            "valid_for_ms": 5_000,
        }

    def _at(
        self,
        hub: TvHub,
        device: SimpleNamespace,
        track: dict,
        *,
        playback_id: str,
        state: str,
        position_ms: int | None,
        duration_ms: int | None = 10_000,
    ) -> None:
        device.playback = self._playback(
            hub,
            track,
            playback_id=playback_id,
            state=state,
            position_ms=position_ms,
            duration_ms=duration_ms,
        )

    async def test_next_track_waits_for_real_end_and_ignores_pause(self) -> None:
        tracks = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]
        hub, issued, device = self._hub(tracks, expected="playback-1", started=True)
        self._at(
            hub, device, tracks[0], playback_id="playback-1", state="paused", position_ms=10_000
        )
        await hub._observe_playlist(device)
        self.assertEqual(issued, [])
        device.playback["state"] = "stopped"
        await hub._observe_playlist(device)
        self.assertEqual(issued[0]["args"]["content"], tracks[1]["content"])
        self.assertEqual(hub._playlist_state["track_index"], 1)

    async def test_smarttube_binds_fresh_identity_without_command_playback_id(self) -> None:
        tracks = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]
        hub, issued, device = self._hub(tracks, expected=None, ignored="stale-id", started=False)
        self._at(hub, device, tracks[0], playback_id="stale-id", state="playing", position_ms=9_900)
        await hub._observe_playlist(device)
        self.assertEqual(issued, [])
        self.assertIsNone(hub._playlist_state["expected_playback_id"])
        self._at(hub, device, tracks[0], playback_id="fresh-id", state="playing", position_ms=1_000)
        await hub._observe_playlist(device)
        self.assertEqual(issued, [])
        self.assertEqual(hub._playlist_state["expected_playback_id"], "fresh-id")
        self.assertTrue(hub._playlist_state["started"])
        self._at(
            hub, device, tracks[0], playback_id="fresh-id", state="stopped", position_ms=10_000
        )
        await hub._observe_playlist(device)
        self.assertEqual(issued[0]["args"]["content"], tracks[1]["content"])
        self.assertEqual(hub._playlist_state["track_index"], 1)

    async def test_uses_track_duration_when_smarttube_omits_duration(self) -> None:
        tracks = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]
        hub, issued, device = self._hub(tracks, expected="playback-1", started=True)
        self._at(
            hub,
            device,
            tracks[0],
            playback_id="playback-1",
            state="stopped",
            position_ms=9_500,
            duration_ms=None,
        )
        await hub._observe_playlist(device)
        self.assertEqual(issued[0]["args"]["content"], tracks[1]["content"])

    async def test_stopped_after_near_end_advances_even_if_position_resets(self) -> None:
        tracks = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]
        hub, issued, device = self._hub(tracks, expected="playback-1", started=True)
        self._at(
            hub, device, tracks[0], playback_id="playback-1", state="playing", position_ms=9_600
        )
        await hub._observe_playlist(device)
        self.assertEqual(issued, [])
        self._at(hub, device, tracks[0], playback_id="playback-1", state="stopped", position_ms=0)
        await hub._observe_playlist(device)
        self.assertEqual(issued[0]["args"]["content"], tracks[1]["content"])

    async def test_oral_next_skips_without_waiting_for_end(self) -> None:
        tracks = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]
        hub, issued, device = self._hub(tracks, expected="playback-1", started=True)
        self._at(
            hub, device, tracks[0], playback_id="playback-1", state="playing", position_ms=1_000
        )
        result = await hub.next_playlist()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(issued[0]["args"]["content"], tracks[1]["content"])
        self.assertEqual(hub._playlist_state["track_index"], 1)


if __name__ == "__main__":
    unittest.main()
