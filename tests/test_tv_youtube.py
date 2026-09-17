from __future__ import annotations

import json
import unittest

from jarvis_office.tv.youtube import SmartTubeSearchError, parse_entries


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


if __name__ == "__main__":
    unittest.main()
