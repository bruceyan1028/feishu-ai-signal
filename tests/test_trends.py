"""Google / X 热力图：热搜筛 AI、单话题 0-100、单侧失败与昨日回退。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import publish, trends


class _NoRawMixin(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(trends, "write_raw")
        patcher.start()
        self.addCleanup(patcher.stop)


def _days() -> list[str]:
    return trends.day_labels(date(2026, 8, 28))


def _specs() -> list[trends.TopicSpec]:
    return trends.editorial_specs()


def _google_solo(days: list[str]):
    """每话题单独一条曲线。agent 带 rising；funding 峰值 ≤1，rising 不应进格子。"""

    def batch_fn(keywords: list[str]):
        n = len(days)
        keyword = keywords[0]
        if keyword == trends.QUERIES["agent"]["g"]:
            series = {keyword: [40.0 + index * 5 for index in range(n)]}
            related = {keyword: ["ai agent framework", "mcp gateway"]}
        elif keyword == trends.QUERIES["funding"]["g"]:
            series = {keyword: [1.0] * n}
            related = {keyword: ["junk rising"]}
        else:
            series = {keyword: [20.0] * n}
            related = {}
        return list(days), series, related

    return batch_fn


def _x_counts(days: list[str], *, fail: set[str] | None = None):
    fail = fail or set()

    def api_get(query: str, start_time: str):
        topic = next(spec.id for spec in _specs() if spec.query_x == query)
        if topic in fail:
            raise RuntimeError(f"{topic} boom")
        data = []
        for index, day in enumerate(days):
            start = f"{day}T00:00:00+08:00"
            data.append({"start": start, "tweet_count": (index + 1) * 10})
        return {"data": data}

    return api_get


class DayLabelTest(unittest.TestCase):
    def test_seven_beijing_days_ending_today(self):
        days = _days()
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0], "2026-08-22")
        self.assertEqual(days[-1], "2026-08-28")

    def test_google_timeframe_is_eight_calendar_days(self):
        self.assertEqual(trends.google_timeframe(date(2026, 8, 31)), "2026-08-23 2026-08-31")


class AiFilterTest(unittest.TestCase):
    def test_keeps_named_ai_products_and_drops_consumer_tech(self):
        self.assertTrue(trends.is_ai_trend("claude code"))
        self.assertTrue(trends.is_ai_trend("claude"))
        self.assertTrue(trends.is_ai_trend("agent ai"))
        self.assertTrue(trends.is_ai_trend("OpenAI"))
        self.assertTrue(trends.is_ai_trend("dlss 5"))
        self.assertFalse(trends.is_ai_trend("フィジカルai"))
        self.assertFalse(trends.is_latin_keyword("フィジカルai"))
        self.assertFalse(trends.is_ai_trend("claude raymond"))
        self.assertFalse(trends.is_ai_trend("iphone 18 pro"))
        self.assertFalse(trends.is_ai_trend("honor robot phone"))
        self.assertFalse(trends.is_ai_trend("real madrid vs málaga"))

    def test_selects_ai_hits_ranked_by_volume(self):
        hits = [
            {"keyword": "iphone 18", "volume": 20000, "geos": ["US"], "related": ["iphone"]},
            {"keyword": "Claude Code", "volume": 100, "geos": ["US"], "related": ["claude code"]},
            {"keyword": "agent ai", "volume": 200, "geos": ["US", "GB"], "related": ["agent ai"]},
            {"keyword": "agent ai", "volume": 80, "geos": ["IN"], "related": []},
        ]
        picked = trends.select_ai_topics(hits)
        self.assertEqual([spec.label for spec in picked], ["agent ai", "Claude Code"])
        self.assertEqual(picked[0].volume, 200)
        self.assertEqual(picked[0].geos, ("US", "GB", "IN"))
        self.assertIn("-is:retweet", picked[0].query_x)

    def test_drops_near_duplicate_dlss_variants(self):
        hits = [
            {"keyword": "dlss 5", "volume": 2000, "geos": ["TW"], "related": ["dlss 5"]},
            {"keyword": "nvidia dlss 5", "volume": 200, "geos": ["US"], "related": ["dlss"]},
            {"keyword": "dlss5", "volume": 500, "geos": ["DE"], "related": ["dlss5"]},
            {"keyword": "openai", "volume": 1000, "geos": ["JP"], "related": ["openai"]},
        ]
        picked = trends.select_ai_topics(hits)
        self.assertEqual([spec.label for spec in picked], ["dlss 5", "openai"])


class BreakoutScopeTest(unittest.TestCase):
    def test_flag_emoji_and_globe(self):
        self.assertEqual(trends.flag_emoji("JP"), "🇯🇵")
        self.assertEqual(trends.flag_emoji("US"), "🇺🇸")
        self.assertEqual(trends.flag_emoji(""), "🌐")

    def test_prefers_global_breakouts_then_fills_country(self):
        candidates = [
            trends.TopicSpec("dlss5", "dlss5", "dlss5", "x", volume=500, geos=("JP",)),
            trends.TopicSpec("cursor", "cursor", "cursor", "x", volume=1000, geos=("DE",)),
            trends.TopicSpec("openai", "openai", "openai", "x", volume=1000, geos=("JP",)),
            trends.TopicSpec("flat", "flat", "flat", "x", volume=100, geos=("US",)),
        ]
        global_series = {
            "dlss5": [0, 0, 1, 22, 56, 82, 100],
            "cursor": [90, 90, 90, 86, 100, 77, 88],
            "openai": [100, 100, 96, 86, 77, 64, 86],
            "flat": [10, 10, 10, 10, 10, 10, 10],
        }
        only = trends.pick_scoped_rows(candidates, global_series, min_rows=4, fill_hot=False)
        self.assertEqual([spec.id for spec, _ in only], ["dlss5"])
        picked = trends.pick_scoped_rows(candidates, global_series, min_rows=4)
        self.assertEqual([spec.id for spec, _ in picked], ["dlss5", "cursor", "openai", "flat"])
        self.assertEqual(picked[0][0].mark, "🌐")
        self.assertTrue(picked[0][0].breakout)
        self.assertFalse(picked[1][0].breakout)
        self.assertEqual([spec.scope for spec, _ in picked], ["global"] * 4)

        country_series = {
            "cursor": ("DE", [40, 42, 41, 45, 48, 50, 90]),
            "openai": ("JP", [20, 22, 21, 24, 30, 40, 80]),
        }
        filled = trends.pick_scoped_rows(
            candidates, global_series, country_series, min_rows=4
        )
        self.assertEqual([spec.id for spec, _ in filled], ["dlss5", "openai", "cursor", "flat"])
        self.assertEqual(filled[0][0].scope, "global")
        self.assertEqual(filled[1][0].mark, "🇯🇵")
        self.assertEqual(filled[2][0].mark, "🇩🇪")
        self.assertTrue(all(spec.breakout for spec, _ in filled[:3]))
        self.assertFalse(filled[3][0].breakout)
        self.assertEqual(filled[3][0].scope, "global")

    def test_skipping_yesterday_can_admit_a_late_spike(self):
        candidates = [
            trends.TopicSpec("late", "late spike", "late spike", "x", volume=200, geos=("US",)),
        ]
        global_series = {"late": [10, 10, 10, 10, 10, 80, 20]}
        picked = trends.pick_scoped_rows(candidates, global_series, min_rows=1)
        self.assertEqual([spec.id for spec, _ in picked], ["late"])
        self.assertLess(trends.row_ratio(global_series["late"]), 1.5)
        self.assertGreaterEqual(
            trends.row_ratio(global_series["late"], skip_yesterday=True), 1.5
        )


class GoogleFrameParseTest(unittest.TestCase):
    def test_series_from_frame_drops_partial_and_fills_missing(self):
        import pandas as pd

        frame = pd.DataFrame(
            {
                "artificial intelligence": [50, 60],
                "AI agent": [10, 20],
                "isPartial": [False, True],
            },
            index=pd.to_datetime(["2026-08-30", "2026-08-31"]),
        )
        dates, series = trends._series_from_frame(
            frame, ["artificial intelligence", "AI agent", "missing"]
        )
        self.assertEqual(dates, ["2026-08-30", "2026-08-31"])
        self.assertEqual(series["AI agent"], [10.0, 20.0])
        self.assertEqual(series["missing"], [0.0, 0.0])
        self.assertNotIn("isPartial", series)


class GoogleSoloTest(_NoRawMixin):
    def test_keeps_each_topic_on_its_own_scale(self):
        days = _days()
        block = trends.fetch_google(
            days, topics=_specs(), batch_fn=_google_solo(days), sleep_fn=lambda _: None
        )
        self.assertFalse(block["error"])
        agent = block["matrix"]["raw"][trends.TOPICS.index("agent")]
        product = block["matrix"]["raw"][trends.TOPICS.index("product")]
        funding = block["matrix"]["raw"][trends.TOPICS.index("funding")]
        self.assertEqual(agent, [40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0])
        self.assertEqual(product, [20.0] * 7)
        self.assertEqual(funding, [1.0] * 7)
        self.assertEqual(len(block["matrix"]["raw"]), 10)
        last = f"agent|{days[-1]}"
        self.assertIn("g-agent-q1", block["items"][last])
        self.assertEqual(block["itemIndex"]["g-agent-q1"]["title"], "ai agent framework")
        funding_last = f"funding|{days[-1]}"
        self.assertEqual(block["items"][funding_last], ["g-funding"])


class XCountsTest(_NoRawMixin):
    def test_buckets_daily_counts(self):
        days = _days()
        block = trends.fetch_x(
            days,
            topics=_specs(),
            bearer="token",
            api_get=_x_counts(days),
            sleep_fn=lambda _: None,
        )
        self.assertFalse(block["error"])
        row = block["matrix"]["raw"][0]
        self.assertEqual(len(row), 7)
        self.assertEqual(row[-1], 70.0)
        self.assertEqual(len(block["matrix"]["raw"]), 10)

    def test_missing_token_does_not_call_api(self):
        days = _days()
        called = []
        block = trends.fetch_x(
            days,
            topics=_specs(),
            bearer="",
            api_get=lambda query, start: called.append(query) or {},
            sleep_fn=lambda _: None,
        )
        self.assertEqual(block["error"], "未配置 X_BEARER_TOKEN")
        self.assertEqual(called, [])

    def test_all_queries_failing_sets_source_error(self):
        days = _days()
        block = trends.fetch_x(
            days,
            topics=_specs(),
            bearer="token",
            api_get=_x_counts(days, fail=set(trends.TOPICS)),
            sleep_fn=lambda _: None,
        )
        self.assertTrue(block["error"])
        self.assertFalse(any(value for row in block["matrix"]["raw"] for value in row))


class BuildPayloadTest(_NoRawMixin):
    def test_contract_and_one_sided_failure(self):
        days = _days()
        google = trends.fetch_google(
            days, topics=_specs(), batch_fn=_google_solo(days), sleep_fn=lambda _: None
        )

        def x_fail(_days_arg):
            return trends.empty_source(_days_arg, topics=list(trends.TOPICS), error="X boom")

        payload = trends.build_payload(
            today=date(2026, 8, 28),
            topics=_specs(),
            google_fn=lambda _: google,
            x_fn=x_fail,
        )
        self.assertEqual(payload["days"], days)
        self.assertEqual(payload["topics"], list(trends.TOPICS))
        self.assertEqual(payload["selection"]["method"], "google-trending-ai")
        self.assertEqual(len(payload["google-trends"]["matrix"]["raw"]), 10)
        self.assertEqual(len(payload["x"]["matrix"]["raw"]), 10)
        self.assertTrue(all(len(row) == 7 for row in payload["google-trends"]["matrix"]["raw"]))
        self.assertTrue(all(len(row) == 7 for row in payload["x"]["matrix"]["raw"]))
        self.assertFalse(payload["google-trends"]["error"])
        self.assertEqual(payload["x"]["error"], "X boom")
        self.assertIn("agent", payload["queries"])

    def test_failed_side_keeps_overlapping_days_from_yesterday(self):
        old_days = trends.day_labels(date(2026, 8, 27))
        new_days = trends.day_labels(date(2026, 8, 28))
        previous_google = trends._finish_source(
            old_days, [[5.0] * 7 for _ in trends.TOPICS], _specs(), kind="g"
        )
        previous = {
            "days": old_days,
            "topics": list(trends.TOPICS),
            "google-trends": previous_google,
            "x": trends.empty_source(old_days, topics=list(trends.TOPICS)),
        }
        payload = trends.build_payload(
            today=date(2026, 8, 28),
            previous=previous,
            topics=_specs(),
            google_fn=lambda days: trends.empty_source(
                days, topics=list(trends.TOPICS), error="Trends blocked"
            ),
            x_fn=lambda days: trends.empty_source(days, topics=list(trends.TOPICS), error="no token"),
        )
        row = payload["google-trends"]["matrix"]["raw"][0]
        self.assertEqual(payload["google-trends"]["error"], "Trends blocked")
        self.assertEqual(row[0], 5.0)
        self.assertEqual(row[-1], 0.0)
        self.assertEqual(payload["days"], new_days)

    def test_empty_trending_list_reuses_previous_topics(self):
        previous = {
            "topics": ["claude-code"],
            "labels": {"claude-code": "claude code"},
            "queries": {"claude-code": {"g": "claude code", "x": '"claude code" -is:retweet'}},
        }
        payload = trends.build_payload(
            today=date(2026, 8, 28),
            previous=previous,
            select_fn=lambda: [],
            google_fn=lambda days: trends.empty_source(days, topics=["claude-code"], error="skip"),
            x_fn=lambda days: trends.empty_source(days, topics=["claude-code"], error="skip"),
        )
        self.assertEqual(payload["topics"], ["claude-code"])
        self.assertEqual(payload["labels"]["claude-code"], "claude code")

    def test_breakouts_are_serialized_and_restored(self):
        specs = [
            trends.TopicSpec("dlss5", "dlss5", "dlss5", "x", breakout=True),
            trends.TopicSpec("flat", "flat", "flat", "x", breakout=False),
        ]
        payload = trends.build_payload(
            today=date(2026, 8, 28),
            topics=specs,
            google_fn=lambda days: trends.empty_source(days, topics=["dlss5", "flat"]),
            x_fn=lambda days: trends.empty_source(days, topics=["dlss5", "flat"]),
        )
        self.assertEqual(payload["breakouts"], ["dlss5"])
        self.assertEqual(payload["selection"]["breakouts"], ["dlss5"])
        restored = trends.specs_from_payload(payload)
        self.assertTrue(restored[0].breakout)
        self.assertFalse(restored[1].breakout)

    def test_write_payload_roundtrip(self):
        days = _days()
        google = trends.fetch_google(
            days, topics=_specs(), batch_fn=_google_solo(days), sleep_fn=lambda _: None
        )
        payload = trends.build_payload(
            today=date(2026, 8, 28),
            topics=_specs(),
            google_fn=lambda _: google,
            x_fn=lambda d: trends.empty_source(d, topics=list(trends.TOPICS), error="skip"),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "heatmap-trends.json"
            trends.write_payload(payload, output)
            loaded = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(loaded["topics"], list(trends.TOPICS))
            self.assertEqual(trends.load_previous(output)["generatedAt"], payload["generatedAt"])


class PublishIntegrationTest(unittest.TestCase):
    def test_trends_snapshot_survives_site_rebuild(self):
        self.assertIn("heatmap-trends.json", publish._PERSISTENT_DATA_GLOBS)
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            data.mkdir()
            (data / "heatmap-trends.json").write_text("{}", encoding="utf-8")
            kept = publish.stash_persistent_site_data(Path(temp))
            self.assertIn("heatmap-trends.json", kept)


class FrontendContractTest(unittest.TestCase):
    def test_load_heatmap_fetches_static_json_and_falls_back_to_demo(self):
        template = Path("index.html").read_text(encoding="utf-8")
        loader = template.split("async function loadHeatmap")[1].split("const App")[0]
        self.assertIn("data/heatmap-trends.json", loader)
        self.assertIn("applyHeatPayload", loader)
        self.assertIn("applyHeatDemo", loader)
        self.assertIn("function applyHeatPayload", template)
        self.assertIn("heatmapPayload", template)
        self.assertIn("暂无数据：", template)
        self.assertIn("Asia/Shanghai", template)
        self.assertIn("payload.topics", template)
        self.assertIn("heat-mark", template)
        self.assertIn("breakouts", template)
        self.assertIn("openHeatNews", template)
        self.assertIn("这两天有什么新闻吗", template)
        self.assertIn("🌐", template)


class WorkflowTest(unittest.TestCase):
    def test_daily_brief_refreshes_trends(self):
        text = Path(".github/workflows/daily-brief.yml").read_text(encoding="utf-8")
        self.assertIn("python -m src.trends", text)
        self.assertIn("heatmap-trends.json", text)
