"""ByteDance Seed article API 适配（不打外网）。"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import scrape


class SeedApiTest(unittest.TestCase):
    def test_is_seed_feed(self):
        self.assertTrue(scrape._is_seed_feed({"id": "bytedance-seed", "url": ""}))
        self.assertTrue(
            scrape._is_seed_feed({"id": "x", "url": "https://seed.bytedance.com/en/"})
        )
        self.assertFalse(scrape._is_seed_feed({"id": "openai-news", "url": "https://openai.com"}))

    def test_ms_to_iso(self):
        self.assertTrue(scrape._ms_to_iso(1783440000000).startswith("2026-"))

    @patch("src.scrape.requests.get")
    def test_fetch_seed_items(self, mock_get: MagicMock):
        list_resp = MagicMock()
        list_resp.raise_for_status = MagicMock()
        list_resp.json.return_value = {
            "sub_article_list": [
                {
                    "ArticleMeta": {
                        "ID": 1,
                        "ArticleID": 1783417209913,
                        "PublishDate": 1783440000000,
                    },
                    "ArticleSubContentEn": {
                        "Title": "Introducing Seedream 5.0 Pro",
                        "Abstract": "A multimodal image generation model.",
                        "TitleKey": "introducing-seedream-5-0-pro",
                    },
                    "ArticleSubContentZh": {},
                }
            ]
        }
        detail_resp = MagicMock()
        detail_resp.raise_for_status = MagicMock()
        detail_resp.json.return_value = {
            "article": {
                "ArticleMeta": {"Title": "Introducing Seedream 5.0 Pro"},
                "Content": "<p>Full blog body with enough characters for the pipeline.</p>",
                "ContentZh": "<p>中文正文</p>",
            }
        }
        mock_get.side_effect = [list_resp, detail_resp]
        feed = {
            "id": "bytedance-seed",
            "url": "https://seed.bytedance.com/en/",
            "max_articles": 5,
            "extra_config": {"seed_api": True, "seed_locale": "en", "recent_days": 60},
        }
        items = scrape._fetch_seed_items(feed)
        self.assertEqual(len(items), 1)
        self.assertIn("Seedream", items[0]["title"])
        self.assertIn("/en/blog/introducing-seedream-5-0-pro", items[0]["url"])
        self.assertIn("Full blog body", items[0]["body"])


if __name__ == "__main__":
    unittest.main()
