"""正文清洗、段落保留与译文回退的单元测试（不打外网）。"""
from __future__ import annotations

import unittest

from src import daily, scrape


class CleanBodyTest(unittest.TestCase):
    def test_strips_chinese_byline_and_outlet_name(self):
        raw = "智东西 作者 | 云鹏 编辑 | 漠影 智东西7月25日报道，Anthropic 发布 Claude Opus 5。"
        self.assertEqual(
            daily.clean_body(raw, "智东西"),
            "智东西7月25日报道，Anthropic 发布 Claude Opus 5。",
        )

    def test_keeps_english_body_intact(self):
        raw = "During Google Cloud Next 2026, MLCommons announced MedPerf enablement."
        self.assertEqual(daily.clean_body(raw, "MLCommons"), raw)

    def test_does_not_strip_source_wording_inside_english_prose(self):
        raw = "First sentence mentions 来源：内部资料 clearly. Second keeps 来源：公开报道 intact."
        self.assertEqual(daily.clean_body(raw, "X"), raw)

    def test_preserves_paragraph_breaks(self):
        self.assertEqual(daily.clean_body("段落一。\n\n\n\n段落二。"), "段落一。\n\n段落二。")


class DisplayBodyTest(unittest.TestCase):
    def test_prefers_cached_translation(self):
        fields = {"来源": "JMLR", "原文": "a" * 100, "中文正文": "已翻译的正文。"}
        self.assertEqual(daily.display_body(fields)["body"], "已翻译的正文。")

    def test_marks_truncated_when_original_exceeds_limit(self):
        fields = {
            "来源": "JMLR",
            "原文": "a" * (daily.config.BODY_TRANSLATE_LIMIT + 10),
            "中文正文": "译文。",
        }
        self.assertTrue(daily.display_body(fields)["bodyTruncated"])

    def test_falls_back_to_original_without_translation(self):
        fields = {"来源": "智东西", "原文": "中文原文内容。", "中文正文": ""}
        result = daily.display_body(fields)
        self.assertEqual(result["body"], "中文原文内容。")
        self.assertFalse(result["bodyTruncated"])


class ScrapeParagraphTest(unittest.TestCase):
    def test_html_to_text_keeps_block_boundaries(self):
        html = "<p>First para.</p><p>Second para.</p><li>item</li>"
        self.assertEqual(
            scrape._html_to_text(html),
            "First para.\n\nSecond para.\n\nitem",
        )

    def test_titles_stay_single_line(self):
        self.assertEqual(scrape._one_line("A  b\nc"), "A b c")


if __name__ == "__main__":
    unittest.main()
