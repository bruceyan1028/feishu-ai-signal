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
        self.assertEqual(items[0]["published_raw"], "2026-07-10T00:00:00Z")

    @patch("src.scrape.requests.get")
    def test_recent_edit_does_not_make_old_model_new(self, mock_get: MagicMock):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "data": {
                "models": [
                    {
                        "id": "owner/old-model",
                        "created_at": "2024-01-01T00:00:00Z",
                        "last_modified": "2026-07-28T00:00:00Z",
                    }
                ]
            }
        }
        mock_get.return_value = response

        items = scrape._fetch_modelscope_items(
            {
                "id": "modelscope-home",
                "url": "https://modelscope.cn/home",
                "max_articles": 5,
                "extra_config": {"recent_days": 30},
            }
        )

        self.assertEqual(items, [])

    @patch("src.scrape.requests.get")
    def test_owner_feed_filters_quantized_variants(self, mock_get: MagicMock):
        list_resp = MagicMock()
        list_resp.raise_for_status = MagicMock()
        list_resp.json.return_value = {
            "data": {
                "models": [
                    {
                        "id": "ZhipuAI/GLM-5.2",
                        "display_name": "GLM-5.2",
                        "created_at": "2026-06-16T08:31:21Z",
                    },
                    {
                        "id": "ZhipuAI/GLM-5.2-FP8",
                        "display_name": "GLM-5.2-FP8",
                        "created_at": "2026-06-16T08:33:26Z",
                    },
                ]
            }
        }
        detail_resp = MagicMock()
        detail_resp.raise_for_status = MagicMock()
        detail_resp.json.return_value = {
            "data": {
                "display_name": "GLM-5.2",
                "description": "Official long-horizon model release with complete details.",
                "readme": "# GLM-5.2\nA one-million-token context model.",
            }
        }
        mock_get.side_effect = [list_resp, detail_resp]

        items = scrape._fetch_modelscope_items(
            {
                "id": "zhipu-modelscope",
                "url": "https://modelscope.cn/organization/ZhipuAI",
                "max_articles": 5,
                "extra_config": {
                    "modelscope_mode": "owner",
                    "modelscope_owner": "ZhipuAI",
                    "model_name_exclude_regex": r"(?:FP8|GGUF|NVFP4)$",
                    "recent_days": 365,
                },
            }
        )

        self.assertEqual([item["title"] for item in items], ["GLM-5.2"])
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["owner"], "ZhipuAI")
        self.assertEqual(mock_get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
