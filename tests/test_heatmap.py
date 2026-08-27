"""话题热力图：打标校验、按周聚合、模拟数据。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import heatmap, publish, tag_topics


class TopicValidationTest(unittest.TestCase):
    def test_rejects_labels_outside_the_enum(self):
        self.assertIsNone(heatmap.parse_topics_strict(["agent", "llm-hype"]))
        self.assertEqual(heatmap.parse_topics_strict(["agent", "infra"]), ["agent", "infra"])
        self.assertEqual(heatmap.parse_topics_strict([]), ["other"])
        self.assertEqual(heatmap.sanitize_topics(["nope"]), ["other"])


class TagBatchTest(unittest.TestCase):
    def test_retries_when_model_returns_unknown_topic(self):
        rows = [{"id": "a", "title": "MCP gateway", "summary": "", "source": "x"}]
        good = {"results": [{"id": "a", "topics": ["agent"]}]}
        bad = {"results": [{"id": "a", "topics": ["hype"]}]}
        with patch("src.tag_topics._llm_json", side_effect=[bad, good]) as mock, patch(
            "src.tag_topics.time.sleep"
        ):
            tagged = tag_topics.tag_batch(rows, retries=3)
        self.assertEqual(tagged["a"], ["agent"])
        self.assertEqual(mock.call_count, 2)

    def test_skips_already_tagged_date(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            items = root / "items"
            tagged = root / "tagged"
            items.mkdir()
            tagged.mkdir()
            (items / "2026-08-27.jsonl").write_text(
                json.dumps({"id": "1", "title": "x", "summary": "", "url": "", "source": "s", "date": "2026-08-27"})
                + "\n",
                encoding="utf-8",
            )
            existing = tagged / "2026-08-27.jsonl"
            existing.write_text("{}\n", encoding="utf-8")
            with patch.object(heatmap, "ITEMS_DIR", items), patch.object(heatmap, "TAGGED_DIR", tagged):
                out = tag_topics.tag_date("2026-08-27")
            self.assertEqual(out.read_text(encoding="utf-8"), "{}\n")


class AggregateTest(unittest.TestCase):
    def test_normalizes_per_topic_and_marks_heating_trend(self):
        rows = []
        # W32 funding 4; W35 funding 1 → 退烧。agent 反过来。
        for day, topic, n in (
            ("2026-08-05", "funding", 4),
            ("2026-08-05", "agent", 1),
            ("2026-08-12", "funding", 3),
            ("2026-08-12", "agent", 2),
            ("2026-08-19", "funding", 1),
            ("2026-08-19", "agent", 3),
            ("2026-08-26", "funding", 1),
            ("2026-08-26", "agent", 4),
        ):
            for i in range(n):
                rows.append(
                    {
                        "id": f"{topic}-{day}-{i}",
                        "title": topic,
                        "source": "s",
                        "url": "https://example.com",
                        "date": day,
                        "topics": [topic],
                    }
                )
        payload = heatmap.build_heatmap(rows, window=12)
        self.assertEqual(payload["weeks"], ["2026-W32", "2026-W33", "2026-W34", "2026-W35"])
        self.assertEqual(payload["window_end"], "2026-W35")
        agent = payload["topics"].index("agent")
        funding = payload["topics"].index("funding")
        self.assertEqual(payload["matrix"]["raw"][agent], [1, 2, 3, 4])
        self.assertEqual(payload["matrix"]["normalized"][agent][-1], 1.0)
        self.assertLess(payload["trend"]["funding"], 0.5)
        self.assertGreater(payload["trend"]["agent"], 1.5)
        self.assertIn("agent-2026-08-26-0", payload["items"]["agent|2026-W35"])

    def test_seed_mock_writes_four_weeks_and_site_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                patch.object(heatmap, "ITEMS_DIR", root / "items"),
                patch.object(heatmap, "TAGGED_DIR", root / "tagged"),
                patch.object(heatmap, "HEATMAP_PATH", root / "heatmap.json"),
                patch.object(heatmap, "SITE_HEATMAP_PATH", root / "site" / "heatmap.json"),
            ):
                payload = heatmap.seed_mock()
            self.assertEqual(len(payload["weeks"]), 4)
            self.assertTrue((root / "tagged" / "2026-08-26.jsonl").is_file())
            self.assertTrue((root / "site" / "heatmap.json").is_file())
            self.assertGreater(payload["trend"]["agent"], 1.5)
            self.assertLess(payload["trend"]["funding"], 0.5)


class PublishIntegrationTest(unittest.TestCase):
    def test_heatmap_survives_site_rebuild(self):
        self.assertIn("heatmap.json", publish._PERSISTENT_DATA_GLOBS)


class FrontendContractTest(unittest.TestCase):
    def test_topic_heat_sits_on_the_dark_data_rail(self):
        template = Path("index.html").read_text(encoding="utf-8")
        self.assertIn("function heatmapHtml", template)
        self.assertIn("function feedNavHtml", template)
        self.assertIn("setHeatmapSource", template)
        self.assertIn("pickHeatmapCell", template)
        self.assertIn("Google Trends", template)
        self.assertIn("🔥", template)
        self.assertIn("❄️", template)
        rail = template.split("function dataRailHtml")[1].split("function heatmapHtml")[0]
        self.assertIn("${heatmapHtml(A)}", rail)
