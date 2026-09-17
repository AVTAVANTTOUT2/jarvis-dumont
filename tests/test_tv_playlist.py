from __future__ import annotations

import unittest
from types import SimpleNamespace

from jarvis_office.tv.hub import TvHub


def _track(track_id: str) -> dict:
    return {
        "id": track_id,
        "title": track_id,
        "content": {"kind": "youtube_video", "id": track_id},
        "duration_ms": 10_000,
    }


class PlaylistSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_track_waits_for_real_end_and_ignores_pause(self) -> None:
        hub = object.__new__(TvHub)
        tracks = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]
        hub.playlists = [{"id": "playlist-1", "name": "Test", "tracks": tracks}]
        hub._playlist_state = {
            "playlist_id": "playlist-1",
            "track_index": 0,
            "device_id": "device-1",
            "status": "playing",
            "started": True,
            "command_id": "old-command",
            "expected_playback_id": "playback-1",
        }
        hub.now = lambda: 1_700_000_000_000
        hub._bump = lambda: None
        issued: list[dict] = []

        async def issue(**kwargs: object) -> dict:
            issued.append(kwargs)
            return {"command_id": "next-command"}

        hub.issue = issue
        device = SimpleNamespace(
            device_id="device-1",
            playback={
                "app": "smarttube",
                "content": tracks[0]["content"],
                "playback_id": "playback-1",
                "state": "paused",
                "position_ms": 10_000,
                "duration_ms": 10_000,
                "observed_at_ms": hub.now(),
                "valid_for_ms": 5_000,
            },
            pending={},
            queue=[],
        )
        await hub._observe_playlist(device)
        self.assertEqual(issued, [])
        device.playback["state"] = "stopped"
        await hub._observe_playlist(device)
        self.assertEqual(issued[0]["args"]["content"], tracks[1]["content"])
        self.assertEqual(hub._playlist_state["track_index"], 1)


if __name__ == "__main__":
    unittest.main()
