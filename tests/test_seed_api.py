"""ByteDance Seed article API 适配（不打外网）。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src import scrape


def ms_days_ago(n: float) -> int:
    """相对当前时间造毫秒时间戳；写死的时间戳会随 recent_days 窗口滑动而失效。"""
    moment = datetime.now(timezone.utc) - timedelta(days=n)
    return int(moment.timestamp() * 1000)


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
                        "PublishDate": ms_days_ago(10),
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

    @patch("src.scrape.requests.get")
    def test_update_time_alone_is_not_publication(self, mock_get: MagicMock):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "sub_article_list": [
                {
                    "ArticleMeta": {
                        # 只有编辑时间、没有 PublishDate：编辑不等于发布
                        "ID": 1,
                        "ArticleID": 1,
                        "UpdateTime": ms_days_ago(1),
                    },
                    "ArticleSubContentEn": {
                        "Title": "Edited old article",
                        "TitleKey": "edited-old-article",
                    },
                }
            ]
        }
        mock_get.return_value = response

        items = scrape._fetch_seed_items(
            {
                "id": "bytedance-seed",
                "url": "https://seed.bytedance.com/en/",
                "max_articles": 5,
                "extra_config": {"seed_api": True, "recent_days": 60},
            }
        )

        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
