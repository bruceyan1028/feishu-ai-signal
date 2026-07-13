from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src import config, daily, main, notify, process, publish, sources


class PipelineTests(unittest.TestCase):
    def test_only_active_rss_sources_are_mapped(self) -> None:
        records = [
            {"fields": {"source_id": "rss", "name": "RSS", "status": "active", "fetch_method": "RSS", "endpoint": "https://example.com/rss"}},
            {"fields": {"source_id": "scrape", "status": "active", "fetch_method": "Scrape", "endpoint": "https://example.com"}},
            {"fields": {"source_id": "paused", "status": "paused", "fetch_method": "RSS", "endpoint": "https://example.com/paused"}},
        ]
        self.assertEqual([item["id"] for item in sources.map_feed_sources(records)], ["rss"])

    def test_arxiv_is_capped_across_feeds(self) -> None:
        raw = []
        for index in range(config.MAX_ARXIV_ITEMS + 3):
            raw.append(
                {
                    "title": f"Paper {index}",
                    "url": f"https://arxiv.org/abs/2607.{index:05d}",
                    "body": "A" * 250,
                    "published_raw": datetime.now(timezone.utc).isoformat(),
                    "feed": {"id": f"arxiv-{index % 2}", "name": "arXiv", "fetch_method": "RSS", "lookback_hours": 168},
                }
            )
        cleaned = process.process_and_clean(raw)
        self.assertGreater(len(cleaned), config.MAX_ARXIV_ITEMS)
        self.assertEqual(len(main.filter_new_items(cleaned, set())), config.MAX_ARXIV_ITEMS)

    def test_feishu_field_mapping_matches_real_schema(self) -> None:
        fields = process.format_for_feishu(
            {
                "title": "Title",
                "url": "https://example.com",
                "source": "Source",
                "source_type": "Company Blog",
                "fetch_method": "RSS",
                "category": "前沿模型公司",
                "tier": "L1 一级官方",
                "published_ms": 1,
                "collected_ms": 2,
                "duplicate_key": "key",
            }
        )
        self.assertEqual(fields["路由来源"], "RSS")
        self.assertNotIn("取值来源", fields)


class DailyTests(unittest.TestCase):
    def test_candidate_selection_respects_rss_set_and_arxiv_cap(self) -> None:
        now = datetime.now(timezone.utc)
        stamp = int(now.timestamp() * 1000)
        records = [
            {"record_id": "official", "fields": {"source_id": "official-rss", "发布时间": stamp, "链接": {"link": "https://example.com"}}},
            *[
                {
                    "record_id": f"a{i}",
                    "fields": {"source_id": "arxiv-cl", "发布时间": stamp - i, "链接": {"link": f"https://arxiv.org/abs/{i}"}},
                }
                for i in range(config.MAX_ARXIV_ITEMS + 2)
            ],
            {"record_id": "scrape", "fields": {"source_id": "scrape", "发布时间": stamp}},
        ]
        selected = daily.select_candidates(
            records,
            {"official-rss": "P0", "arxiv-cl": "P1"},
            {"official-rss", "arxiv-cl"},
            now=now,
            limit=50,
        )
        self.assertEqual(selected[0]["record_id"], "official")
        self.assertEqual(len([item for item in selected if item["source_id"].startswith("arxiv-")]), config.MAX_ARXIV_ITEMS)
        self.assertNotIn("scrape", [item["record_id"] for item in selected])


class DeliveryTests(unittest.TestCase):
    def sample_brief(self) -> dict:
        return {
            "date": "2026-07-13",
            "title": "AI Signal 每日情报 · 2026-07-13",
            "intro": "今日真实情报。",
            "bullets": [{"text": "一条要点", "refs": [1]}],
            "signals": [
                {
                    "recordId": "rec1",
                    "title": "真实标题",
                    "source": "OpenAI",
                    "url": "https://example.com/news",
                    "category": "前沿模型公司",
                    "publishedDate": "2026-07-13",
                    "summary": "真实摘要",
                    "why": "值得关注",
                    "impact": 90,
                    "novelty": 80,
                    "actionability": 70,
                    "urgency": "高",
                    "tags": ["AI"],
                }
            ],
            "briefRecordId": "brief1",
            "briefTableId": "table1",
        }

    def test_static_site_contract_and_card_url(self) -> None:
        brief = self.sample_brief()
        with tempfile.TemporaryDirectory() as directory:
            site = publish.build_site([brief], directory)
            latest = json.loads((site / "data" / "brief-latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["signals"][0]["title"], "真实标题")
            self.assertTrue((site / "index.html").exists())
        url = notify.detail_url("https://example.github.io/demo/", brief["date"])
        self.assertEqual(url, "https://example.github.io/demo/?date=2026-07-13")
        card = notify.build_card(brief, url)
        self.assertIn("真实标题", json.dumps(card, ensure_ascii=False))
        self.assertIn(url, json.dumps(card, ensure_ascii=False))

    @patch("src.notify.feishu.send_interactive_message")
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    @patch("src.notify.feishu.read_all_records_with_ids")
    def test_already_sent_brief_is_not_duplicated(self, read_records, _token, send_message) -> None:
        read_records.return_value = [
            {
                "record_id": "brief1",
                "fields": {"简报ID": "2026-07-13", "发送状态": "已发送", "消息ID": "message1"},
            }
        ]
        result = notify.send(self.sample_brief(), "https://example.com", "ou_test")
        self.assertTrue(result["skipped"])
        send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
