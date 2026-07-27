"""正文清洗、段落保留与译文回退的单元测试（不打外网）。"""
from __future__ import annotations

import unittest

from src import daily, rss, scrape


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

    def test_drops_wordpress_copyright_tail(self):
        raw = "Real body sentence.\n\nThe post Some Title appeared first on Microsoft Azure Blog ."
        self.assertEqual(daily.clean_body(raw, "Azure"), "Real body sentence.")

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

    def test_table_keeps_rows_and_columns(self):
        html = (
            "<p>Intro.</p>"
            "<figure><table><thead><tr><th>Model</th><th>Workload</th></tr></thead>"
            "<tbody><tr><td>Phi-4</td><td>700B tokens a month for<br>data prep</td></tr>"
            "<tr><td>Gemma 4</td><td>OTel2.0 workflows</td></tr></tbody></table></figure>"
        )
        self.assertEqual(
            scrape.html_to_text(html),
            "Intro.\n\nModel | Workload\n"
            "Phi-4 | 700B tokens a month for data prep\n"
            "Gemma 4 | OTel2.0 workflows",
        )


class ArticleMediaTest(unittest.TestCase):
    PAGE = "https://example.com/blog/post/"

    def test_skips_logos_icons_and_tiny_images(self):
        html = (
            '<img src="/img/site-logo.png">'
            '<img src="/img/spacer.gif" width="1" height="1">'
            '<img src="/img/thumb.jpg" width="120" height="90">'
            '<img src="/img/diagram.jpg" width="1200" height="800" alt="架构图">'
        )
        self.assertEqual(
            rss.extract_article_images(html, self.PAGE),
            [{"url": "https://example.com/img/diagram.jpg", "alt": "架构图"}],
        )

    def test_skips_wordpress_emoji_and_thumbnail_sizes(self):
        html = (
            '<img src="https://s.w.org/images/core/emoji/17.0.2/72x72/1f517.png" alt="🔗">'
            '<img src="/uploads/shot-150x150.png">'
            '<img src="/uploads/shot-1920x1080.png">'
        )
        self.assertEqual(
            [item["url"] for item in rss.extract_article_images(html, self.PAGE)],
            ["https://example.com/uploads/shot-1920x1080.png"],
        )

    def test_reads_lazy_and_srcset_sources_once(self):
        html = (
            '<img data-src="/a.jpg" width="900">'
            '<img srcset="/a.jpg 1x, /a@2x.jpg 2x" width="900">'
            '<img srcset="/b.jpg 800w" width="900">'
        )
        self.assertEqual(
            [item["url"] for item in rss.extract_article_images(html, self.PAGE)],
            ["https://example.com/a.jpg", "https://example.com/b.jpg"],
        )

    def test_drops_trailing_promo_block(self):
        body = "<p>" + "正文内容。" * 60 + '</p><div class="promotional"><img src="/cta.jpg" width="900"></div>'
        cleaned = rss.strip_trailing_promo(body)
        self.assertNotIn("cta.jpg", cleaned)
        self.assertIn("正文内容。", cleaned)

    def test_keeps_promo_like_container_inside_article(self):
        body = '<div class="wp-block-buttons"><a href="/x">Read more</a></div><p>' + "正文。" * 80 + "</p>"
        self.assertEqual(rss.strip_trailing_promo(body), body)

    def test_picks_content_over_related_article_cards(self):
        cards = "".join(f"<article><p>{'related teaser ' * 3}</p></article>" for _ in range(4))
        main = "<div class='entry'><p>" + "real article body. " * 80 + "</p></div>"
        chunk = rss._pick_content_chunk(f"<body>{cards}{main}</body>")
        self.assertIn("real article body.", chunk)


if __name__ == "__main__":
    unittest.main()
