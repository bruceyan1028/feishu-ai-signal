"""capture_spec 加载 / 归一 / 确定性违规判定 单测。"""
import json
import tempfile
from pathlib import Path
import unittest

from src import capture_spec as cs


def _spec_full() -> dict:
    return {
        "version": 3,
        "probed_at": "2026-09-02T10:00:00+08:00",
        "enabled": True,
        "route": {
            "list": {"selector": ".card a", "min_links": 5},
            "date": {"selector": ".ArticleHeader time", "fallback": "meta"},
            "article": {"title": "h1", "body": "article"},
        },
        "expect": {"min_links": 5, "date_min_ratio": 0.8},
    }


class CaptureSpecTest(unittest.TestCase):
    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cs.load(Path(d) / "nope.json"), {})

    def test_load_bad_json_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "spec.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(cs.CaptureSpecError):
                cs.load(p)

    def test_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "spec.json"
            specs = {"anthropic-news": _spec_full()}
            cs.save(specs, p)
            loaded = cs.load(p)
            self.assertIn("anthropic-news", loaded)
            self.assertEqual(loaded["anthropic-news"]["route"]["date"]["selector"], ".ArticleHeader time")

    def test_normalize_defaults_enabled_from_route(self):
        got = cs.normalize_spec({"version": 1, "route": {"list": {"selector": "a"}}})
        self.assertTrue(got["enabled"])
        self.assertEqual(got["expect"], {})

    def test_spec_for_none_when_disabled_or_empty(self):
        self.assertIsNone(cs.spec_for("x", {}))
        self.assertIsNone(
            cs.spec_for("x", {"x": {"enabled": False, "route": {"list": {"selector": "a"}}}})
        )
        self.assertIsNone(
            cs.spec_for("x", {"x": {"enabled": True, "route": {}}})
        )

    def test_find_violations_min_links(self):
        specs = {"src1": _spec_full()}
        fetch_stats = {"src1": {"links": 2, "error": None}}
        out = cs.find_violations(fetch_stats, {}, specs, {})
        self.assertEqual(len(out), 1)
        self.assertIn("min_links", out[0]["violated"])

    def test_find_violations_ok_when_meets_expect(self):
        specs = {"src1": _spec_full()}
        # links 达标，且提供满足 date_min_ratio 的漏斗数据
        fetch_stats = {"src1": {"links": 8, "error": None}}
        funnel = {"src1": {"kept": 10, "missing_or_invalid_date": 0}}
        self.assertEqual(cs.find_violations(fetch_stats, {}, specs, funnel), [])

    def test_find_violations_no_funnel_data_not_date_violation(self):
        # 没有漏斗记录的源，不应被当成日期命中率 0 而误判
        specs = {"src1": _spec_full()}
        fetch_stats = {"src1": {"links": 8, "error": None}}
        self.assertEqual(cs.find_violations(fetch_stats, {}, specs, {}), [])

    def test_find_violations_spec_mismatch_via_error(self):
        specs = {"src1": _spec_full()}
        # 源级 error 已是 spec_mismatch：即便其他指标达标也要判违规
        fetch_stats = {"src1": {"links": 8, "error": "spec_mismatch"}}
        funnel = {"src1": {"kept": 10, "missing_or_invalid_date": 0}}
        out = cs.find_violations(fetch_stats, {}, specs, funnel)
        self.assertEqual(out[0]["violated"], ["spec_mismatch"])

    def test_find_violations_date_ratio(self):
        specs = {"src1": _spec_full()}
        fetch_stats = {"src1": {"links": 8, "error": None}}
        funnel = {"src1": {"kept": 3, "missing_or_invalid_date": 5}}
        out = cs.find_violations(fetch_stats, {}, specs, funnel)
        self.assertEqual(len(out), 1)
        self.assertIn("date_min_ratio", out[0]["violated"])
        # 3 / (3+5) = 0.375 < 0.8 才触发；min_links 8>=5 已满足
        self.assertNotIn("min_links", out[0]["violated"])

    def test_spec_without_expect_never_violates(self):
        specs = {"src1": cs.normalize_spec({"version": 0, "route": {"list": {"selector": "a"}}})}
        fetch_stats = {"src1": {"links": 0}}
        self.assertEqual(cs.find_violations(fetch_stats, {}, specs, {}), [])


if __name__ == "__main__":
    unittest.main()
