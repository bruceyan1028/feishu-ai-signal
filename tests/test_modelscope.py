"""ModelScope OpenAPI 适配（不打外网）。"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import scrape


class ModelScopeTest(unittest.TestCase):
    def test_is_modelscope_feed(self):
        self.assertTrue(scrape._is_modelscope_feed({"id": "modelscope-home", "url": ""}))
        self.assertTrue(scrape._is_modelscope_feed({"id": "qwen-modelscope", "url": ""}))  # legacy id
        self.assertTrue(
            scrape._is_modelscope_feed({"id": "x", "url": "https://modelscope.cn/home"})
        )
        self.assertFalse(scrape._is_modelscope_feed({"id": "openai-news", "url": "https://openai.com"}))

    def test_model_page_url(self):
        self.assertEqual(
            scrape._modelscope_model_page_url("Wan-AI/Wan-Dancer-14B"),
            "https://www.modelscope.cn/models/Wan-AI/Wan-Dancer-14B",
        )

    @patch("src.scrape.requests.get")
    def test_fetch_modelscope_items_home(self, mock_get: MagicMock):
        list_resp = MagicMock()
        list_resp.raise_for_status = MagicMock()
        list_resp.json.return_value = {
            "success": True,
            "data": {
                "models": [
                    {
                        "id": "Wan-AI/Wan-Dancer-14B",
                        "display_name": "Wan-Dancer-14B",
                        "description": "short",
                        "last_modified": "2026-07-14T00:00:00Z",
                        "created_at": "2026-07-10T00:00:00Z",
                    }
                ]
            },
        }
        detail_resp = MagicMock()
        detail_resp.raise_for_status = MagicMock()
        detail_resp.json.return_value = {
            "success": True,
            "data": {
                "id": "Wan-AI/Wan-Dancer-14B",
                "display_name": "Wan-Dancer-14B",
                "description": "A dancing video model with enough detail text here.",
                "readme": "# Wan-Dancer\nMore details for the body.",
                "last_modified": "2026-07-14T00:00:00Z",
            },
        }
        mock_get.side_effect = [list_resp, detail_resp]

        feed = {
            "id": "modelscope-home",
            "url": "https://modelscope.cn/home",
            "max_articles": 5,
            "extra_config": {"modelscope_mode": "home", "recent_days": 30},
        }
        items = scrape._fetch_modelscope_items(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Wan-Dancer-14B")
        self.assertIn("/models/Wan-AI/Wan-Dancer-14B", items[0]["url"])
        self.assertIn("dancing video", items[0]["body"])


if __name__ == "__main__":
    unittest.main()
