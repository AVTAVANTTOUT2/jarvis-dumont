from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_office.tv.hub import _playlist_reached_end
from jarvis_office.tv.youtube import (
    SmartTubeSearchError,
    metadata_smarttube,
    parse_entries,
    parse_metadata,
    youtube_id_from_url,
)


class SmartTubeSearchTests(unittest.TestCase):
    def test_parse_entries_keeps_first_unique_canonical_videos(self) -> None:
        raw = json.dumps(
            {
                "entries": [
                    {"id": "firstVideo1", "title": "First", "channel": "A"},
                    {"id": "firstVideo1", "title": "Duplicate"},
                    {"id": "too-short", "title": "Ignore"},
                    {"id": "secondVid02", "title": "Second", "uploader": "B"},
                ]
            }
        ).encode()
        items = parse_entries(raw, limit=5)
        self.assertEqual([item["content"]["id"] for item in items], ["firstVideo1", "secondVid02"])
        self.assertEqual(items[1]["channel"], "B")

    def test_parse_entries_rejects_invalid_json(self) -> None:
        with self.assertRaises(SmartTubeSearchError):
            parse_entries(b"not-json", limit=5)

    def test_parse_entries_respects_limit(self) -> None:
        raw = json.dumps(
            {
                "entries": [
                    {"id": "abcdefghijk", "title": "One"},
                    {"id": "12345678901", "title": "Two"},
                ]
            }
        ).encode()
        self.assertEqual(len(parse_entries(raw, limit=1)), 1)

    def test_smarttube_url_and_duration_are_canonicalized(self) -> None:
        video_id = "abcdefghijk"
        self.assertEqual(
            youtube_id_from_url(f"https://www.youtube.com/watch?v={video_id}&t=4"), video_id
        )
        self.assertEqual(youtube_id_from_url(f"https://youtu.be/{video_id}"), video_id)
        self.assertIsNone(youtube_id_from_url("http://www.youtube.com/watch?v=abcdefghijk"))
        metadata = parse_metadata(
            json.dumps({"title": "  Track  ", "duration": 123.4}).encode(), video_id
        )
        self.assertEqual(metadata["title"], "Track")
        self.assertEqual(metadata["duration_ms"], 123400)

    def test_playlist_end_uses_fresh_player_position_shape(self) -> None:
        base = {"duration_ms": 10000, "position_ms": 9500, "state": "stopped"}
        self.assertTrue(_playlist_reached_end(base))
        self.assertFalse(_playlist_reached_end({**base, "state": "paused"}))
        self.assertFalse(_playlist_reached_end({**base, "position_ms": 8000}))

    def test_metadata_asks_yt_dlp_for_a_compact_payload(self) -> None:
        video_id = "abcdefghijk"
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "yt-dlp"
            binary.write_text(
                "#!/bin/sh\n"
                'for arg in "$@"; do\n'
                '  if [ "$arg" = "--dump-single-json" ]; then exit 3; fi\n'
                "done\n"
                'printf \'%s\\n\' \'{"title": "Track", "duration": 163}\'\n'
            )
            binary.chmod(0o755)
            with patch("jarvis_office.tv.youtube._binary", return_value=str(binary)):
                metadata = asyncio.run(metadata_smarttube(video_id))
        self.assertEqual(metadata["title"], "Track")
        self.assertEqual(metadata["duration_ms"], 163000)
        self.assertEqual(metadata["content"], {"kind": "youtube_video", "id": video_id})


if __name__ == "__main__":
    unittest.main()
