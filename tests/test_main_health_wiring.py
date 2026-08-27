"""main.run() 的健康记录接线测试。

正式日报每天跑 main.run()，这里的接线错了要等到定时任务才暴露。用 stub 顶掉所有
飞书调用和抓取通道，只验证：抓取结果与漏斗被正确汇总、健康记录落盘、
以及观测层出错不会影响采集本身。
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import health, main


def _param(source_id, name, *, fetch_method="RSS", endpoint=None):
    return {
        "record_id": f"rec-{source_id}",
        "fields": {
            "source_id": source_id,
            "name": name,
            "status": "active",
            "fetch_method": fetch_method,
            "endpoint": endpoint or f"https://{source_id}.example.com/feed",
            "dimension": "前沿模型公司",
            "来源类型": "纯网页",
            "tier": "L1",
            "priority": "P0",
            "lookback_window": "24h",
            "keyword_regex": "(gpt|claude|model)",
        },
    }


PARAMS = [
    _param("alpha", "Alpha 源"),
    _param("beta", "Beta 源"),
    _param("gamma", "Gamma 源"),
]


def _raw(source_id, title, *, hours_ago=2):
    stamp = datetime.datetime.utcfromtimestamp(
        (health.now_ms() - hours_ago * 3600000) / 1000
    ).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return {
        "title": title,
        "url": f"https://{source_id}.example.com/{title.replace(' ', '-')}",
        "body": "A new model release with claude and gpt improvements inside. " * 4,
        "published_raw": stamp,
        "feed": {
            "id": source_id,
            "name": source_id,
            "url": f"https://{source_id}.example.com",
            "fetch_method": "RSS",
            "lookback_hours": 24,
            "keyword_regex": "(gpt|claude|model)",
            "min_content_chars": 50,
            "source_type": "纯网页",
        },
    }


class MainHealthWiringTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.created = []

        # alpha 抓到两条并全部通过；beta 抓到一条但太老会被时间窗淘汰；gamma 抓取失败
        rss_items = [
            _raw("alpha", "Model launch A"),
            _raw("alpha", "Model launch B"),
            _raw("beta", "Ancient model post", hours_ago=500),
        ]
        rss_stats = {
            "alpha": {"source_id": "alpha", "engine": "feedparser", "entries": 2, "error": None},
            "beta": {"source_id": "beta", "engine": "feedparser", "entries": 1, "error": None},
            "gamma": {
                "source_id": "gamma",
                "engine": "feedparser",
                "entries": 0,
                "error": "unparseable_or_empty",
            },
        }

        feishu_noop = {
            name: mock.DEFAULT
            for name in (
                "ensure_entry_enrichment_fields",
                "ensure_paper_config_fields",
                "ensure_social_config_fields",
                "ensure_source_type_field",
                "ensure_select_option",
                "update_social_cursor_states",
            )
        }
        self.patches = [
            mock.patch.object(main.config, "validate", lambda: None),
            mock.patch.object(main.feishu, "get_tenant_access_token", lambda: "tok"),
            mock.patch.object(main.feishu, "read_param_records", lambda _t: PARAMS),
            mock.patch.object(main.feishu, "read_existing_dedup_keys", lambda _t: set()),
            mock.patch.object(main.feishu, "sync_param_collect_stats", lambda *a, **k: 0),
            mock.patch.object(
                main.feishu,
                "batch_create_records",
                lambda _t, fields: self.created.extend(fields) or len(fields),
            ),
            mock.patch.object(main.typed_config, "load_typed_configs", lambda _t: {}),
            mock.patch.object(
                main.rss, "fetch_feed_sources_with_stats", lambda _f: (rss_items, rss_stats)
            ),
            mock.patch.object(main.rss, "backfill_full_text", lambda _items: None),
            mock.patch.object(main.podcast, "enrich_podcast_items", lambda items: (items, {})),
            mock.patch.object(
                main.policy_document,
                "enrich_items",
                lambda _items: {"items_attempted": 0, "documents_read": 0},
            ),
            mock.patch.object(main.health, "HEALTH_DIR", self.dir),
        ]
        for name in feishu_noop:
            self.patches.append(mock.patch.object(main.feishu, name, lambda *a, **k: None))
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def _rows(self):
        files = list(self.dir.glob("dt=*.jsonl"))
        self.assertEqual(len(files), 1, "本轮应只写一个当天分片")
        return {
            json.loads(line)["source_id"]: json.loads(line)
            for line in files[0].read_text(encoding="utf-8").strip().splitlines()
        }

    def test_run_writes_one_health_row_per_attempted_source(self):
        self.assertEqual(main.run({"RSS"}), 0)
        rows = self._rows()
        self.assertEqual(set(rows), {"alpha", "beta", "gamma"})
        self.assertEqual(len(self.created), 2)

    def test_fetch_failures_and_rule_drops_land_in_different_columns(self):
        main.run({"RSS"})
        rows = self._rows()
        # 抓到并留下
        self.assertEqual(rows["alpha"]["written"], 2)
        self.assertEqual(rows["alpha"]["blocked_at"], "")
        # 抓到但被时间窗吃掉：规则问题，fetch 没报错
        self.assertEqual(rows["beta"]["written"], 0)
        self.assertEqual(rows["beta"]["blocked_at"], "lookback")
        self.assertIsNone(rows["beta"]["fetch"]["error"])
        # 一条都没抓到：链路问题，漏斗里没有 raw
        self.assertEqual(rows["gamma"]["funnel"].get("raw", 0), 0)
        self.assertEqual(rows["gamma"]["fetch"]["error"], "unparseable_or_empty")

    def test_rows_carry_run_id_and_config_metadata(self):
        main.run({"RSS"})
        row = self._rows()["alpha"]
        self.assertTrue(row["run_id"])
        self.assertEqual(row["name"], "Alpha 源")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["priority"], "P0")
        self.assertEqual(row["fetch_method"], "RSS")

    def test_health_write_failure_does_not_break_collection(self):
        # 观测层是附加物，它坏了不该让当轮采集白跑
        with mock.patch.object(
            main.health, "write_records", side_effect=OSError("disk full")
        ):
            self.assertEqual(main.run({"RSS"}), 0)
        self.assertEqual(len(self.created), 2)

    def test_health_rows_are_written_even_when_nothing_is_new(self):
        # 全部命中跨轮去重时 run() 会提前返回；静默的源恰恰是这时最该记录的
        with mock.patch.object(
            main.feishu,
            "read_existing_dedup_keys",
            lambda _t: {
                "https://alpha.example.com/model-launch-a",
                "https://alpha.example.com/model-launch-b",
            },
        ):
            self.assertEqual(main.run({"RSS"}), 0)
        self.assertEqual(self.created, [])
        rows = self._rows()
        self.assertEqual(set(rows), {"alpha", "beta", "gamma"})
        self.assertEqual(rows["alpha"]["written"], 0)
        self.assertEqual(rows["alpha"]["dedup_dropped"], 2)


if __name__ == "__main__":
    unittest.main()
