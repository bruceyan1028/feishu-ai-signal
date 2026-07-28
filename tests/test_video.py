from __future__ import annotations

import unittest
from unittest.mock import patch

from src import video


class VideoSourceTest(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(video.parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(video.parse_duration("PT12M5S"), 725)
        self.assertEqual(video.parse_duration("bad"), 0)

    @patch("src.video._top_comments")
    @patch("src.video._api_get")
    def test_fetch_source_builds_media_signal(self, api_get, top_comments):
        api_get.side_effect = [
            {
                "items": [
                    {
                        "contentDetails": {
                            "relatedPlaylists": {"uploads": "UU-test"}
                        }
                    }
                ]
            },
            {"items": [{"contentDetails": {"videoId": "abcdefghijk"}}]},
            {
                "items": [
                    {
                        "id": "abcdefghijk",
                        "status": {"privacyStatus": "public"},
                        "snippet": {
                            "title": "AI release",
                            "description": "New AI model details",
                            "channelTitle": "Example AI",
                            "publishedAt": "2026-07-28T00:00:00Z",
                            "liveBroadcastContent": "none",
                            "thumbnails": {
                                "high": {"url": "https://example.com/thumb.jpg"}
                            },
                        },
                        "contentDetails": {"duration": "PT12M5S"},
                        "statistics": {
                            "viewCount": "12000",
                            "commentCount": "30",
                        },
                    }
                ]
            },
        ]
        top_comments.return_value = [{"text": "Useful", "likes": 10}]
        feed = {
            "id": "youtube-example",
            "name": "Example AI",
            "extra_config": {
                "channel_id": "UC-test",
                "max_items": 1,
                "include_shorts": False,
                "include_live": False,
            },
        }

        result = video.fetch_source(feed, key="test-key")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metrics"]["duration_sec"], 725)
        self.assertEqual(result[0]["metrics"]["views"], 12000)
        self.assertEqual(
            result[0]["media_assets"]["videos"][0]["id"],
            "abcdefghijk",
        )
        self.assertEqual(result[0]["media_assets"]["topComments"][0]["likes"], 10)


if __name__ == "__main__":
    unittest.main()
