"""论文质量富集与打分的单元测试（不打外网）。"""
from __future__ import annotations

import unittest

from src import paper_enrich, process


class PaperEnrichTest(unittest.TestCase):
    def test_extract_arxiv_id(self):
        self.assertEqual(
            paper_enrich.extract_arxiv_id("https://arxiv.org/abs/2607.11889v1"),
            "2607.11889",
        )
        self.assertEqual(
            paper_enrich.extract_arxiv_id(
                "https://huggingface.co/papers/2607.11889"
            ),
            "2607.11889",
        )

    def test_hf_and_arxiv_share_dedup_key(self):
        feed = {"dedup_key": "arxiv_id(strip_version)"}
        self.assertEqual(
            process.build_dedup_key(
                "https://huggingface.co/papers/2607.11889", "Paper", feed
            ),
            process.build_dedup_key(
                "https://arxiv.org/abs/2607.11889v2", "Paper", feed
            ),
        )

    def test_parse_acceptance(self):
        self.assertIn(
            "NeurIPS",
            paper_enrich.parse_acceptance("Accepted to NeurIPS 2025 as a poster"),
        )
        self.assertEqual(paper_enrich.parse_acceptance("15 pages"), "")
        self.assertEqual(paper_enrich.parse_acceptance("Accepted to the LTEDI workshop"), "")
        self.assertEqual(paper_enrich.parse_acceptance("Accepted as oral"), "")

    def test_quality_formula(self):
        q = paper_enrich.compute_quality_score(
            venue=100, community=40, signal=60
        )
        # 0.40*100 + 0.25*40 + 0.35*60 = 40+10+21 = 71
        self.assertAlmostEqual(q, 71.0, places=0)

    def test_quality_renorm_without_community(self):
        q = paper_enrich.compute_quality_score(
            venue=20,
            community=0,
            signal=60,
            community_known=False,
        )
        # only venue+signal → (0.4*20 + 0.35*60) / 0.75 ≈ 38.7
        self.assertAlmostEqual(q, 38.7, places=1)

    def test_venue_score(self):
        self.assertEqual(
            paper_enrich.venue_score("ICLR 2025", ["iclr", "neurips"], None)[0],
            100.0,
        )
        self.assertEqual(paper_enrich.venue_score("", ["iclr"], None)[0], 20.0)


class EvaluatePaperTest(unittest.TestCase):
    URL = "https://arxiv.org/abs/2607.11889"

    def test_signal_gate_drops_before_enrich(self):
        calls = []
        original = paper_enrich.enrich_paper
        paper_enrich.enrich_paper = lambda *a, **k: calls.append(1) or {}
        try:
            metrics: dict = {}
            verdict = paper_enrich.evaluate_paper(
                "Lecture notes on transformers", "x" * 300, self.URL,
                {"min_signal_score": 55}, metrics,
            )
        finally:
            paper_enrich.enrich_paper = original
        self.assertFalse(verdict.keep)
        self.assertEqual(verdict.reason, "min_signal_score")
        self.assertEqual(calls, [], "信号分不过门槛时不应调用外网富集")
        self.assertLess(metrics["signal_score"], 55)

    def test_keep_fills_metrics_and_quality_fields(self):
        original = paper_enrich.enrich_paper
        paper_enrich.enrich_paper = lambda url, **k: {
            "arxiv_id": "2607.11889",
            "accepted_venue": "ICLR 2026",
            "community_heat": 42.0,
            "venue_score": 100.0,
            "venue_reason": "whitelist",
            "quality_score": 88.0,
            "community_upvotes": 10,
            "community_comments": 2,
        }
        try:
            metrics: dict = {}
            verdict = paper_enrich.evaluate_paper(
                "SOTA benchmark with code github.com/a/b", "x" * 900, self.URL,
                {"min_signal_score": 55}, metrics,
            )
        finally:
            paper_enrich.enrich_paper = original
        self.assertTrue(verdict.keep)
        self.assertTrue(verdict.enriched)
        self.assertIs(metrics["is_preprint"], False)
        self.assertEqual(metrics["accepted_venue"], "ICLR 2026")
        self.assertEqual(metrics["quality_score"], 88.0)
        self.assertEqual(verdict.quality_fields["quality_score"], 88.0)
        self.assertEqual(verdict.quality_fields["accepted_venue"], "ICLR 2026")
        pm = verdict.quality_fields["paper_metrics_json"]
        self.assertEqual(pm["community"]["upvotes"], 10)
        self.assertEqual(pm["signal_score"], metrics["signal_score"])

    def test_no_threshold_skips_gate(self):
        original = paper_enrich.enrich_paper
        paper_enrich.enrich_paper = lambda url, **k: {"quality_score": 30.0}
        try:
            verdict = paper_enrich.evaluate_paper(
                "Lecture notes", "x" * 100, self.URL, {}, {}
            )
        finally:
            paper_enrich.enrich_paper = original
        self.assertTrue(verdict.keep)


if __name__ == "__main__":
    unittest.main()
