from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import fitz

from src import daily, policy_document, process, rss, sources


def _sample_pdf(text: str = "Executive Summary\nFund the Genesis Mission and submit plans in 90 days.") -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    payload = document.tobytes()
    document.close()
    return payload


def _sample_visual_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Policy Recommendation\nAgencies should invest in AI research infrastructure.\n\n"
        "Figure 1: Federal AI investment rises from 20 to 40 billion dollars.\n"
        "The chart compares fiscal years 2026 and 2028.",
    )
    payload = document.tobytes()
    document.close()
    return payload


class PolicyDocumentTest(unittest.TestCase):
    def test_extracts_same_host_pdf_documents(self) -> None:
        body = """
        <p><a href="/wp-content/uploads/2026/07/report.pdf">Science report</a></p>
        <p><a href="https://www.whitehouse.gov/wp-content/uploads/2026/07/memo.pdf">R&amp;D memo</a></p>
        <p><a href="https://www.govinfo.gov/old.pdf">External historical reference</a></p>
        """
        documents = rss.extract_pdf_documents(
            body,
            "https://www.whitehouse.gov/releases/2026/07/example/",
        )
        self.assertEqual([doc["title"] for doc in documents], ["Science report", "R&D memo"])
        self.assertTrue(all("whitehouse.gov" in doc["url"] for doc in documents))

    def test_enriches_policy_item_with_pdf_evidence(self) -> None:
        item = {
            "source_id": "whitehouse-tech-releases",
            "raw_content": "Official release body.",
            "feed": {
                "extra_config": {
                    "document_pdf_enrich": True,
                    "max_document_pdfs": 2,
                }
            },
            "media_assets": {
                "documents": [
                    {
                        "url": "https://www.whitehouse.gov/report.pdf",
                        "title": "Science report",
                    }
                ]
            },
        }
        with patch("src.policy_document.fetch_pdf", return_value=_sample_visual_pdf()):
            self.assertEqual(policy_document.enrich_item(item), 1)
        document = item["media_assets"]["documents"][0]
        self.assertEqual(document["fullTextSource"], "pdf")
        self.assertEqual(document["pages"], 1)
        self.assertEqual(document["visualPages"], [1])
        self.assertIn("Federal AI investment", item["raw_content"])
        self.assertLess(
            item["raw_content"].index("Federal AI investment"),
            item["raw_content"].index("Official release body"),
        )

    @patch("src.policy_document.fetch_pdf", return_value=_sample_visual_pdf())
    def test_renders_and_writes_policy_chart_crops(self, _fetch) -> None:
        images = policy_document.render_visual_pages(
            "https://www.whitehouse.gov/report.pdf",
            [1],
        )
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/png;base64,"))
        with TemporaryDirectory() as tmp:
            files = policy_document.write_visual_images(
                "https://www.whitehouse.gov/report.pdf",
                [1],
                Path(tmp),
                "policy/report",
            )
            self.assertEqual(len(files), 1)
            self.assertTrue((Path(tmp) / files[0]["filename"]).exists())
            self.assertIn("第 1 页图表", files[0]["alt"])

    @patch("src.policy_document.fetch_pdf", side_effect=ValueError("not a PDF"))
    def test_pdf_failure_keeps_release_body(self, _fetch) -> None:
        item = {
            "source_id": "whitehouse-tech-releases",
            "raw_content": "Official release body.",
            "feed": {"extra_config": {"document_pdf_enrich": True}},
            "media_assets": {
                "documents": [{"url": "https://www.whitehouse.gov/broken.pdf", "title": "Broken"}]
            },
        }
        self.assertEqual(policy_document.enrich_item(item), 0)
        self.assertEqual(item["raw_content"], "Official release body.")
        self.assertEqual(item["media_assets"]["documents"][0]["fullTextSource"], "unavailable")

    def test_policy_stage_and_documents_are_persisted(self) -> None:
        feed = {
            "id": "whitehouse-tech-actions",
            "name": "White House 科技总统行动",
            "url": "https://www.whitehouse.gov/presidential-actions/feed/",
            "category": "政策监管地缘",
            "tier": "L1 一级官方",
            "fetch_method": "RSS",
            "source_type": "纯网页",
            "lookback_hours": 720,
            "keyword_regex": r"artificial intelligence|\bAI\b",
            "keyword_min_hits": 2,
            "dedup_key": "normalize(url)",
            "extra_config": {"policy_stage_extract": True},
        }
        raw = {
            "title": "Promoting Advanced Artificial Intelligence Innovation and Security",
            "url": "https://www.whitehouse.gov/presidential-actions/2026/08/ai-security/",
            "body": "Artificial intelligence policy advances AI innovation and AI security.",
            "published_raw": "Tue, 04 Aug 2026 01:00:00 +0000",
            "entry_tags": ["Presidential Actions", "Executive Orders"],
            "media_assets": {
                "images": [],
                "videos": [],
                "documents": [{"url": "https://www.whitehouse.gov/order.pdf", "title": "Order"}],
            },
            "feed": feed,
        }
        with patch("src.process.now_ms", return_value=1785808800000):
            cleaned = process.process_and_clean([raw])
        self.assertEqual(len(cleaned), 1)
        policy = cleaned[0]["media_assets"]["policy"]
        self.assertEqual(policy["stage"], "行政命令")
        fields = process.format_for_feishu(cleaned[0])
        media = json.loads(fields["媒体资源"])
        self.assertEqual(media["policy"]["agency"], "White House")
        self.assertEqual(media["documents"][0]["title"], "Order")

    def test_policy_analysis_uses_dedicated_structure(self) -> None:
        requirement = daily.analysis_requirement(
            is_paper=False,
            is_social=False,
            is_policy=True,
        )
        self.assertIn("【政策性质与效力】", requirement)
        self.assertIn("【时间表与执行机制】", requirement)
        self.assertIn("不得把建议性报告写成法律", requirement)
        topics = daily.normalize_topics({"source_id": "whitehouse-tech-releases"}, ["AI"])
        self.assertEqual(topics, ["AI", "监管"])

    def test_policy_stage_ignores_navigation_labels(self) -> None:
        feed = {"extra_config": {"document_pdf_enrich": True}}
        report = process.extract_policy_metadata(
            "OSTP Director Releases Landmark Report and Recommendations",
            "https://www.whitehouse.gov/releases/2026/07/report/",
            "Navigation: Fact Sheets Executive Orders. The report recommends action.",
            feed,
        )
        determination = process.extract_policy_metadata(
            "Presidential Determination Pursuant to Section 101",
            "https://www.whitehouse.gov/presidential-actions/2026/07/determination/",
            "Executive Order navigation label",
            feed,
            ["Presidential Actions", "Executive Orders"],
        )
        self.assertEqual(report["stage"], "政策报告")
        self.assertEqual(determination["stage"], "总统决定")

    def test_seed_enables_both_whitehouse_feeds(self) -> None:
        seed = json.loads(
            (Path(__file__).parents[1] / "src" / "seed_default.json").read_text(
                encoding="utf-8"
            )
        )
        records = [{"fields": row} for row in seed["一级参数"]]
        feeds = {
            feed["id"]: feed
            for feed in sources.map_feed_sources(records)
            if str(feed["id"]).startswith("whitehouse-tech-")
        }
        self.assertEqual(
            set(feeds),
            {"whitehouse-tech-releases", "whitehouse-tech-actions"},
        )
        self.assertTrue(all(feed["keyword_min_hits"] == 2 for feed in feeds.values()))
        self.assertTrue(all(feed["title_exclude_regex"] for feed in feeds.values()))
        self.assertTrue(
            all("space transportation" in feed["title_exclude_regex"] for feed in feeds.values())
        )
        self.assertTrue(
            all("science and technology" not in (feed.get("keyword_regex") or "") for feed in feeds.values())
        )

    def test_whitehouse_keyword_config_filters_non_ai_policy(self) -> None:
        """泛科技备忘录由种子里的 AI 关键词配置挡下，无需源级硬编码特判。"""
        seed = json.loads(
            (Path(__file__).parents[1] / "src" / "seed_default.json").read_text(
                encoding="utf-8"
            )
        )
        records = [{"fields": row} for row in seed["一级参数"]]
        feed = next(
            f
            for f in sources.map_feed_sources(records)
            if f["id"] == "whitehouse-tech-actions"
        )
        space = {
            "title": "The National Space Transportation Policy",
            "url": "https://www.whitehouse.gov/presidential-actions/2026/08/21/the-national-space-transportation-policy/",
            "body": (
                "This memorandum expands U.S. space launch and reentry. "
                "It covers science and technology, research and development, "
                "spectrum, commercial spaceports, and advanced manufacturing."
            ),
            "published_raw": "Thu, 21 Aug 2026 01:00:00 +0000",
            "feed": feed,
        }
        ai_order = {
            "title": "Promoting Advanced Artificial Intelligence Innovation",
            "url": "https://www.whitehouse.gov/presidential-actions/2026/08/21/promoting-ai-innovation/",
            "body": (
                "This executive order advances AI safety and AI governance, "
                "directing agencies to evaluate foundation models and large "
                "language models used across the federal government."
            ),
            "published_raw": "Thu, 21 Aug 2026 01:00:00 +0000",
            "feed": feed,
        }
        with patch("src.process.now_ms", return_value=1787284800000):
            cleaned = process.process_and_clean([space, ai_order])
        self.assertEqual(
            [row["url"] for row in cleaned],
            [process.normalize_url(ai_order["url"])],
        )


if __name__ == "__main__":
    unittest.main()
