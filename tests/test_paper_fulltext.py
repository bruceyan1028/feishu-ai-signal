from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import fitz

from src import paper_fulltext, report


def _sample_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Abstract\nWe introduce a compact multimodal model.\n\n"
        "1 Introduction\nThe system reads scientific documents.\n\n"
        "2 Method\nOur method aligns text and visual evidence.",
        fontsize=11,
    )
    page2 = document.new_page()
    page2.insert_text(
        (72, 72),
        "3 Experiments\nWe compare against three baselines.\n\n"
        "Table 1: Accuracy improves from 71.0 to 84.5.\n"
        "Higher is better.\n\n"
        "4 Conclusion\nThe combined method performs best.",
        fontsize=11,
    )
    data = document.tobytes()
    document.close()
    return data


class PaperFullTextTest(unittest.TestCase):
    def test_extracts_sections_captions_and_visual_pages(self) -> None:
        evidence = paper_fulltext.extract_pdf_evidence(_sample_pdf())
        self.assertEqual(evidence["source"], "pdf")
        self.assertEqual(evidence["pages"], 2)
        self.assertIn("摘要", [item["title"] for item in evidence["sections"]])
        self.assertIn("实验", [item["title"] for item in evidence["sections"]])
        self.assertEqual(evidence["visual_pages"], [2])
        self.assertIn("84.5", paper_fulltext.evidence_text(evidence))

    @patch("src.paper_fulltext.fetch_pdf", return_value=_sample_pdf())
    def test_renders_visual_page_for_multimodal_input(self, _fetch: MagicMock) -> None:
        images = paper_fulltext.render_visual_pages("https://example.com/paper.pdf", [2])
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/png;base64,"))

    @patch("src.paper_fulltext.fetch_pdf", return_value=_sample_pdf())
    def test_writes_visual_page_into_static_site(self, _fetch: MagicMock) -> None:
        with TemporaryDirectory() as tmp:
            images = paper_fulltext.write_visual_page_images(
                "https://example.com/paper.pdf",
                [2],
                Path(tmp),
                "record/unsafe",
                [{"page": 2, "text": "Table 1: Accuracy improves to 84.5."}],
            )
            self.assertEqual(len(images), 1)
            self.assertTrue((Path(tmp) / images[0]["filename"]).exists())
            self.assertIn("84.5", images[0]["alt"])

    def test_canonical_hf_pdf_url(self) -> None:
        self.assertEqual(
            paper_fulltext.canonical_pdf_url(
                "https://huggingface.co/papers/2607.11889"
            ),
            "https://arxiv.org/pdf/2607.11889.pdf",
        )
        self.assertEqual(
            paper_fulltext.canonical_pdf_url(
                "http://jmlr.org/papers/v27/25-1549.html"
            ),
            "http://jmlr.org/papers/volume27/25-1549/25-1549.pdf",
        )

    @patch("requests.post")
    @patch("src.report.config.LLM_MODEL", "gpt-5.6-sol")
    @patch(
        "src.report.config.LLM_BASE_URL",
        "https://llm.example/v1/chat/completions",
    )
    @patch("src.report.config.LLM_API_KEY", "test")
    def test_llm_json_sends_images_in_chat_format(self, post: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": '{"ok": true}'}}]
        }
        post.return_value = response
        result = report._llm_json(
            "Read the chart",
            image_urls=["data:image/png;base64,AAAA"],
        )
        self.assertTrue(result["ok"])
        content = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Read the chart"})
        self.assertEqual(content[1]["type"], "image_url")


if __name__ == "__main__":
    unittest.main()
