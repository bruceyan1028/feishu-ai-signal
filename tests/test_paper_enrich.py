"""论文质量富集与打分的单元测试（不打外网）。"""
from __future__ import annotations

import unittest

from src import paper_enrich


class PaperEnrichTest(unittest.TestCase):
    def test_extract_arxiv_id(self):
        self.assertEqual(
            paper_enrich.extract_arxiv_id("https://arxiv.org/abs/2607.11889v1"),
            "2607.11889",
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


if __name__ == "__main__":
    unittest.main()
