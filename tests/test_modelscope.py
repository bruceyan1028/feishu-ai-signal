"""ModelScope OpenAPI 适配（不打外网）。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src import scrape


def days_ago(n: float) -> str:
    """相对当前时间造时间戳。

    写死日期的夹具会随时间失效：recent_days 是拿 now 算年龄的，某天一过
    夹具就掉出窗口，测试在没人改代码的情况下自己变红，把流水线整条卡住。
    """
    moment = datetime.now(timezone.utc) - timedelta(days=n)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        created = days_ago(10)
        modified = days_ago(6)
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
                        "last_modified": modified,
                        "created_at": created,
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
                "last_modified": modified,
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
        # 发布时间取首次创建，不能被 last_modified 顶掉
        self.assertEqual(items[0]["published_raw"], created)

    @patch("src.scrape.requests.get")
    def test_recent_edit_does_not_make_old_model_new(self, mock_get: MagicMock):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "data": {
                "models": [
                    {
                        # 创建于窗口外，但昨天刚被编辑过
                        "id": "owner/old-model",
                        "created_at": days_ago(900),
                        "last_modified": days_ago(1),
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
                        "created_at": days_ago(20),
                    },
                    {
                        "id": "ZhipuAI/GLM-5.2-FP8",
                        "display_name": "GLM-5.2-FP8",
                        "created_at": days_ago(20),
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
