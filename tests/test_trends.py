"""Google / X 7 日话题热力图：合同、锚点缩放、单侧失败与昨日回退。"""
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


def _google_batch(days: list[str]):
    """第一批锚点 50、其余批 25；每话题本批相对分都是 100。缩放后后批应 > 100。"""

    def batch_fn(keywords: list[str]):
        n = len(days)
        first_q = trends.QUERIES["agent"]["g"]
        anchor = 50.0 if keywords[1] == first_q else 25.0
        series = {trends.ANCHOR: [anchor] * n}
        related: dict[str, list[str]] = {}
        for keyword in keywords[1:]:
            series[keyword] = [100.0] * n
        if keywords[1] == first_q:
            related[first_q] = ["mcp gateway", "ai agent framework"]
        return list(days), series, related

    return batch_fn


def _x_counts(days: list[str], *, fail: set[str] | None = None):
    fail = fail or set()

    def api_get(query: str, start_time: str):
        topic = next(key for key, spec in trends.QUERIES.items() if spec["x"] == query)
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


class GoogleAnchorTest(_NoRawMixin):
    def test_rescales_later_batches_onto_the_first_anchor(self):
        days = _days()
        block = trends.fetch_google(days, batch_fn=_google_batch(days), sleep_fn=lambda _: None)
        self.assertFalse(block["error"])
        agent = block["matrix"]["raw"][trends.TOPICS.index("agent")]
        product = block["matrix"]["raw"][trends.TOPICS.index("product")]
        self.assertEqual(agent, [100.0] * 7)
        self.assertEqual(product, [200.0] * 7)
        self.assertGreater(max(product), 100)
        self.assertEqual(len(block["matrix"]["raw"]), 10)
        self.assertTrue(all(len(row) == 7 for row in block["matrix"]["raw"]))
        last = f"agent|{days[-1]}"
        self.assertIn("g-agent-q1", block["items"][last])
        self.assertEqual(block["itemIndex"]["g-agent-q1"]["title"], "mcp gateway")


class XCountsTest(_NoRawMixin):
    def test_buckets_daily_counts(self):
        days = _days()
        block = trends.fetch_x(
            days, bearer="token", api_get=_x_counts(days), sleep_fn=lambda _: None
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
            bearer="token",
            api_get=_x_counts(days, fail=set(trends.TOPICS)),
            sleep_fn=lambda _: None,
        )
        self.assertTrue(block["error"])
        self.assertFalse(any(value for row in block["matrix"]["raw"] for value in row))


class BuildPayloadTest(_NoRawMixin):
    def test_contract_and_one_sided_failure(self):
        days = _days()
        google = trends.fetch_google(days, batch_fn=_google_batch(days), sleep_fn=lambda _: None)

        def x_fail(_days_arg):
            return trends.empty_source(_days_arg, error="X boom")

        payload = trends.build_payload(
            today=date(2026, 8, 28),
            google_fn=lambda _: google,
            x_fn=x_fail,
        )
        self.assertEqual(payload["days"], days)
        self.assertEqual(payload["topics"], list(trends.TOPICS))
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
            old_days, [[5.0] * 7 for _ in trends.TOPICS], kind="g"
        )
        previous = {"days": old_days, "google-trends": previous_google, "x": trends.empty_source(old_days)}
        payload = trends.build_payload(
            today=date(2026, 8, 28),
            previous=previous,
            google_fn=lambda days: trends.empty_source(days, error="Trends blocked"),
            x_fn=lambda days: trends.empty_source(days, error="no token"),
        )
        row = payload["google-trends"]["matrix"]["raw"][0]
        self.assertEqual(payload["google-trends"]["error"], "Trends blocked")
        self.assertEqual(row[0], 5.0)
        self.assertEqual(row[-1], 0.0)
        self.assertEqual(payload["days"], new_days)

    def test_write_payload_roundtrip(self):
        days = _days()
        google = trends.fetch_google(days, batch_fn=_google_batch(days), sleep_fn=lambda _: None)
        payload = trends.build_payload(
            today=date(2026, 8, 28),
            google_fn=lambda _: google,
            x_fn=lambda d: trends.empty_source(d, error="skip"),
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


class WorkflowTest(unittest.TestCase):
    def test_daily_brief_refreshes_trends(self):
        text = Path(".github/workflows/daily-brief.yml").read_text(encoding="utf-8")
        self.assertIn("python -m src.trends", text)
        self.assertIn("heatmap-trends.json", text)
