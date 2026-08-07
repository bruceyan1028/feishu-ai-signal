"""正文清洗、段落保留与译文回退的单元测试（不打外网）。"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from src import config, daily, process, rss, scrape


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


class PublishedDateTest(unittest.TestCase):
    def test_missing_or_invalid_date_stays_unknown(self):
        self.assertIsNone(process.parse_date_ms(""))
        self.assertIsNone(process.parse_date_ms("not a date"))

    def test_extracts_published_meta_without_using_modified_time(self):
        html = """
        <meta property="article:published_time" content="2026-06-30T01:26:47Z">
        <meta property="article:modified_time" content="2026-07-27T08:00:00Z">
        """
        self.assertEqual(
            scrape.extract_published_date_html(html),
            "2026-06-30T01:26:47Z",
        )

    def test_extracts_escaped_nextjs_created_at(self):
        html = r'self.__next_f.push([1,"post\":{\"_createdAt\":\"2026-06-30T01:26:47Z\"}"])'
        self.assertEqual(
            scrape.extract_published_date_html(html),
            "2026-06-30T01:26:47Z",
        )

    def test_does_not_treat_modified_time_as_published(self):
        html = '<meta property="article:modified_time" content="2026-07-27T08:00:00Z">'
        self.assertEqual(scrape.extract_published_date_html(html), "")

    def test_extracts_meta_header_date_next_to_read_time(self):
        html = """
        <nav>Archive updated July 28, 2026</nav>
        <h1>Introducing Muse Spark</h1>
        <div class="article-meta">April 8, 2026 • 8 minute read</div>
        <article>Benchmarks measured on March 1, 2026.</article>
        <aside>Related post — July 20, 2026</aside>
        """
        self.assertEqual(
            scrape.extract_published_date_html(html),
            "April 8, 2026",
        )

    def test_does_not_accept_arbitrary_body_date(self):
        html = """
        <h1>Model evaluation report</h1>
        <article>The benchmark dataset was released on April 8, 2026.</article>
        """
        self.assertEqual(scrape.extract_published_date_html(html), "")

    def test_rss_updated_requires_explicit_source_policy(self):
        entry = {"updated": "2026-07-28T00:00:00Z"}
        self.assertEqual(rss._published_raw(entry, {"id": "generic"}), "")
        self.assertEqual(
            rss._published_raw(entry, {"id": "opencompass"}),
            "2026-07-28T00:00:00Z",
        )

    def test_rss_published_wins_over_updated(self):
        entry = {
            "published": "2026-06-30T00:00:00Z",
            "updated": "2026-07-28T00:00:00Z",
        }
        self.assertEqual(
            rss._published_raw(entry, {"id": "generic"}),
            "2026-06-30T00:00:00Z",
        )


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

    def test_zhipu_news_cards_keep_title_date_and_page_order(self):
        html = """
        <a class="group" href="/zh/news/152">
          <img alt="错误的图片标题">
          <h3>智谱首份业绩报告发布，探索 AGI 智能上界</h3>
          <p>2026/03/31</p>
        </a>
        <a class="group" href="/zh/news/151">
          <h3>智谱公布年度业绩发布会安排</h3>
          <p>2026/03/30</p>
        </a>
        """
        links = scrape._extract_zhipu_news_links(
            html,
            {"url": "https://www.zhipuai.cn/zh/news", "max_articles": 8},
        )
        self.assertEqual([item["url"].rsplit("/", 1)[-1] for item in links], ["152", "151"])
        self.assertEqual(links[0]["published_raw"], "2026/03/31")
        self.assertEqual(links[0]["title"], "智谱首份业绩报告发布，探索 AGI 智能上界")

    def test_zhipu_article_h1_overrides_generic_page_title(self):
        html = """
        <html><head><title>Z.ai - Inspiring AGI to Benefit Humanity</title></head>
        <body><main><h1>GLM 新模型正式发布</h1>
        <article><p>智谱发布新的 GLM 模型，面向智能体、软件工程和长程任务，
        同时公布模型能力、部署方法、开放平台入口以及后续生态计划。</p></article>
        </main></body></html>
        """
        item = scrape._build_item_direct(
            html,
            {
                "url": "https://www.zhipuai.cn/zh/news/200",
                "title": "列表标题",
                "published_raw": "2026/08/04",
            },
            {
                "id": "zhipu-ai",
                "extra_config": {"article_title_from_h1": True},
            },
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "GLM 新模型正式发布")
        self.assertEqual(item["published_raw"], "2026/08/04")

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

    def test_html_headings_survive_as_editorial_structure(self):
        html = (
            "<article><p>导语。</p><h2>深层原因：组织链条过长</h2>"
            "<p>模型训练、产品开发和市场反馈需要形成连续循环。</p>"
            "<h3>对行业的影响</h3><p>竞争已经转向系统化生产。</p></article>"
        )
        text = scrape.html_to_text(html)
        self.assertIn("## 深层原因：组织链条过长", text)
        self.assertIn("## 对行业的影响", text)


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

    def test_keeps_responsive_sized_images(self):
        html = (
            '<img src="/hero.jpg" width="100%" height="auto">'
            '<img src="/chart.png" width="1200px">'
            '<img src="/icon.png" width="32" height="32">'
        )
        self.assertEqual(
            [item["url"] for item in rss.extract_article_images(html, self.PAGE)],
            ["https://example.com/hero.jpg", "https://example.com/chart.png"],
        )

    def test_skips_site_chrome_images(self):
        html = (
            '<a href="/"><img class="jmlr" src="/img/jmlr.jpg"></a>'
            '<img src="/img/RSS.gif" class="rss" alt="RSS Feed">'
            '<img src="/img/figure-2.png" alt="Approximation error">'
        )
        self.assertEqual(
            [item["url"] for item in rss.extract_article_images(html, "http://jmlr.org/papers/v27/25-1549.html")],
            ["http://jmlr.org/img/figure-2.png"],
        )

    def test_wide_figure_survives_a_misleading_alt(self):
        # README 里常见把正文大图的 alt 照抄成 "logo"，尺寸才是可靠信号
        html = (
            '<img src="https://cdn.example.com/brand.png" alt="logo" width="400">'
            '<img src="https://cdn.example.com/adoption.png" alt="logo" width="800">'
        )
        self.assertEqual(
            [item["url"] for item in rss.extract_article_images(html, "https://example.com/x")],
            ["https://cdn.example.com/adoption.png"],
        )

    def test_page_header_images_stay_out_of_the_body(self):
        html = (
            '<header><img src="https://example.com/brand-wide.png" alt="Acme"></header>'
            "<article><p>" + "body text. " * 40 + "</p>"
            '<img src="https://example.com/chart-wide.png" alt="Chart"></article>'
        )
        parsed = rss.parse_article_html(html, "https://example.com/post", "")
        self.assertEqual(
            [item["url"] for item in parsed["images"]], ["https://example.com/chart-wide.png"]
        )

    def test_backfilled_images_land_on_the_item(self):
        item = {"media_assets": {"images": [], "videos": []}, "image_url": ""}
        rss._fill_missing_images(item, [{"url": "https://example.com/a.jpg", "alt": "图"}])
        self.assertEqual(item["media_assets"]["images"][0]["url"], "https://example.com/a.jpg")
        self.assertEqual(item["image_url"], "https://example.com/a.jpg")

    def test_backfill_does_not_overwrite_existing_images(self):
        item = {
            "media_assets": {"images": [{"url": "https://example.com/rss.jpg", "alt": ""}], "videos": []},
            "image_url": "https://example.com/rss.jpg",
        }
        rss._fill_missing_images(item, [{"url": "https://example.com/new.jpg", "alt": ""}])
        self.assertEqual(
            [image["url"] for image in item["media_assets"]["images"]],
            ["https://example.com/rss.jpg"],
        )

    def test_meta_cover_wins_over_author_and_related_images(self):
        signal = {
            "contentType": "文章",
            "titleCn": "Jeff Dean 创办新公司",
            "imageUrl": "https://example.com/lucas.png",
            "mediaAssets": {
                "images": [
                    {"url": "https://example.com/lucas.png", "alt": "Lucas Ropek"},
                    {"url": "https://example.com/related.jpg", "alt": "另一篇文章"},
                ],
                "videos": [],
            },
        }
        media, cover = rss.curate_display_media(signal, "https://example.com/jeff-dean.jpg")
        self.assertEqual(cover, "https://example.com/jeff-dean.jpg")
        self.assertEqual(
            media["images"],
            [
                {
                    "url": "https://example.com/jeff-dean.jpg",
                    "alt": "Jeff Dean 创办新公司",
                    "kind": "article-cover",
                }
            ],
        )

    def test_wechat_promo_images_are_not_used_as_fallback(self):
        signal = {
            "contentType": "公众号",
            "imageUrl": "",
            "mediaAssets": {
                "images": [
                    {"url": "https://example.com/wechat-qrcode.jpg", "alt": "扫码关注公众号"},
                ],
                "videos": [],
            },
        }
        media, cover = rss.curate_display_media(signal)
        self.assertEqual(cover, "")
        self.assertEqual(media["images"], [])

    def test_video_keeps_platform_thumbnail(self):
        signal = {
            "contentType": "视频",
            "titleCn": "模型发布直播",
            "imageUrl": "https://example.com/fallback.jpg",
            "mediaAssets": {
                "images": [],
                "videos": [{"thumbnailUrl": "https://i.ytimg.com/vi/demo/maxresdefault.jpg"}],
            },
        }
        media, cover = rss.curate_display_media(signal)
        self.assertEqual(cover, "https://i.ytimg.com/vi/demo/maxresdefault.jpg")
        self.assertEqual(media["images"][0]["kind"], "video-cover")

    def test_reads_wechat_script_cover(self):
        html = r"""<script>var msg_cdn_url = "https:\/\/mmbiz.qpic.cn\/cover.jpg";</script>"""
        self.assertEqual(
            rss._meta_image_from_html(html, "https://mp.weixin.qq.com/s/demo"),
            "https://mmbiz.qpic.cn/cover.jpg",
        )


class LeadingBoilerplateTest(unittest.TestCase):
    def test_keeps_short_lede(self):
        text = "Claude Opus 5 is available today.\n\nA longer paragraph with the details follows."
        self.assertTrue(
            rss._drop_leading_boilerplate(text, "Introducing Claude Opus 5").startswith(
                "Claude Opus 5 is available today."
            )
        )

    def test_drops_date_byline_and_reading_time(self):
        text = "Jul 24, 2026\n\nBy Jane Doe\n\n5 min read\n\nThe real body starts here."
        self.assertEqual(rss._drop_leading_boilerplate(text, "Some Title"), "The real body starts here.")

    def test_drops_section_labels(self):
        text = "Announcements\n\nProduct\n\n我们今天上线了新功能。"
        self.assertEqual(rss._drop_leading_boilerplate(text, "新功能"), "我们今天上线了新功能。")

    def test_drops_date_line_that_follows_a_subtitle(self):
        text = "Update Claude Fable 5 and Mythos 5 redeployed\n\nJul 1, 2026\n\nAccess is restored today."
        self.assertEqual(
            rss._drop_leading_boilerplate(text, "Redeploying Claude Fable 5"),
            "Update Claude Fable 5 and Mythos 5 redeployed\n\nAccess is restored today.",
        )

    def test_drops_author_and_engagement_block(self):
        text = (
            "Training Sparse Embedding Models\n\nPublished\n\nJuly 1, 2025\n\nUpdate on GitHub\n\n"
            "Upvote 138\n\n+132\n\nArthur BRESNU arthurbresnu Follow\n\n"
            "Sentence Transformers is a Python library for training embedding models."
        )
        self.assertEqual(
            rss._drop_leading_boilerplate(text, "Training Sparse Embedding Models"),
            "Sentence Transformers is a Python library for training embedding models.",
        )


class LabelParagraphTest(unittest.TestCase):
    def test_drops_link_labels_and_adjacent_duplicates(self):
        text = (
            "Sonnet 5 is available today across all plans.\n\n"
            "Read more\n\n"
            "Cost-performance curves at different effort levels.\n\n"
            "Cost-performance curves at different effort levels.\n\n"
            "相关阅读\n\n"
            "Developers can use claude-sonnet-5 via the API."
        )
        self.assertEqual(
            rss._drop_label_paragraphs(text),
            "Sonnet 5 is available today across all plans.\n\n"
            "Cost-performance curves at different effort levels.\n\n"
            "Developers can use claude-sonnet-5 via the API.",
        )

    def test_cuts_the_related_article_grid_at_the_tail(self):
        text = "\n\n".join(
            [
                "Sonnet 5 is available today across all plans and in Claude Code.",
                "Developers can use claude-sonnet-5 via the API from today onwards.",
                "Introducing Claude Opus 5",
                "Opus 5 is a step change improvement for the Opus tier.",
                "Read more",
                "A research agenda for the Economic Futures Research Fund",
                "We are sharing the research agenda for the fund.",
                "Read more",
            ]
        )
        self.assertEqual(
            rss._drop_label_paragraphs(text),
            "Sonnet 5 is available today across all plans and in Claude Code.\n\n"
            "Developers can use claude-sonnet-5 via the API from today onwards.",
        )

    def test_drops_the_footer_copyright_line(self):
        text = "The model is available today.\n\nMeta © 2026"
        self.assertEqual(rss._drop_label_paragraphs(text), "The model is available today.")

    def test_drops_trailing_footer_nav_labels(self):
        text = "The paper is available as a PDF.\n\nRSS Feed\n\nMastodon\n\nCookies"
        self.assertEqual(rss._drop_label_paragraphs(text), "The paper is available as a PDF.")

    def test_drops_trailing_footer_nav_rows(self):
        text = (
            "Muse Spark is available today at meta.ai.\n\n"
            "Meta AI Assistant Media Generation Vibes AI Studio\n\n"
            "Our approach About AI at Meta People Careers"
        )
        self.assertEqual(
            rss._drop_label_paragraphs(text), "Muse Spark is available today at meta.ai."
        )

    def test_keeps_a_trailing_fragment_that_reads_like_prose(self):
        text = (
            "The report covers three research directions.\n\n"
            "Model weights available on Hugging Face under Apache 2.0"
        )
        self.assertEqual(rss._drop_label_paragraphs(text), text)

    def test_keeps_a_trailing_sentence_even_when_short(self):
        text = "Longer opening paragraph of the article body.\n\nThat is all."
        self.assertEqual(rss._drop_label_paragraphs(text), text)

    def test_a_single_read_more_does_not_truncate_the_body(self):
        text = "\n\n".join(
            [
                "The first paragraph of a real article body goes here.",
                "Read more",
                "The article keeps going for several more paragraphs after that.",
            ]
        )
        self.assertEqual(
            rss._drop_label_paragraphs(text),
            "The first paragraph of a real article body goes here.\n\n"
            "The article keeps going for several more paragraphs after that.",
        )

    def test_keeps_full_sentences_that_start_like_a_label(self):
        text = "More from our team on this topic is available in the appendix section."
        self.assertEqual(rss._drop_label_paragraphs(text), text)


class ScrapeMediaTest(unittest.TestCase):
    def test_jina_item_carries_article_images(self):
        markdown = (
            "Title: Introducing Claude Sonnet 5\n\nPublished Time: 2026-07-27T00:00:00Z\n\n"
            "Markdown Content:\n"
            "![Hero](https://cdn.anthropic.com/hero-2880x1620.jpg)\n\n"
            "Sonnet 5 is designed to be our most capable Sonnet model yet, with tool use.\n"
        )
        item = scrape._build_item(
            markdown,
            {"url": "https://www.anthropic.com/news/claude-sonnet-5", "title": ""},
            {"id": "anthropic-news"},
        )
        self.assertEqual(item["title"], "Introducing Claude Sonnet 5")
        self.assertEqual(
            [image["url"] for image in item["media_assets"]["images"]],
            ["https://cdn.anthropic.com/hero-2880x1620.jpg"],
        )
        self.assertNotIn("![", item["body"])

    def test_readme_images_resolve_repo_relative_paths_and_skip_badges(self):
        readme = (
            '<img src="docs/hero.png" alt="架构" width="100%">\n'
            "[![Build](https://img.shields.io/badge/build-passing-green)](https://ci)\n"
            "![Bench](/docs/bench.png)\n"
        )
        self.assertEqual(
            [image["url"] for image in scrape._github_readme_images(readme, "acme/repo")],
            [
                "https://raw.githubusercontent.com/acme/repo/HEAD/docs/hero.png",
                "https://raw.githubusercontent.com/acme/repo/HEAD/docs/bench.png",
            ],
        )


class ReadmeNoiseTest(unittest.TestCase):
    def test_drops_language_switcher_and_code_blocks(self):
        readme = (
            "<p><b>English</b> |\n"
            '<a href="./i18n/README_zh.md">简体中文</a> |\n'
            '<a href="./i18n/README_ja.md">日本語</a></p>\n\n'
            "Transformers is a model-definition framework.\n\n"
            "## Installation\n\n```py\npython -m venv .my-env\n```\n"
        )
        text = scrape.readme_to_text(readme)
        self.assertEqual(text, "Transformers is a model-definition framework.")

    def test_keeps_prose_that_merely_contains_a_link(self):
        text = scrape.readme_to_text("See [the docs](https://example.com/docs) now.\n")
        self.assertEqual(text, "See the docs now.")

    def test_drops_pipe_delimited_link_bar_but_keeps_real_tables(self):
        readme = (
            "| [Documentation](https://docs.example.com) | [Blog](https://blog.example.com) |"
            " [Paper](https://arxiv.org/abs/1) |\n\n"
            "vLLM is a fast library for LLM serving.\n\n"
            "| Model | Params |\n| --- | --- |\n| Phi-4 | 14B |\n"
        )
        text = scrape.readme_to_text(readme)
        self.assertNotIn("Documentation", text)
        self.assertIn("vLLM is a fast library for LLM serving.", text)
        self.assertIn("Model | Params\nPhi-4 | 14B", text)

    def test_cut_on_boundary_never_splits_a_word(self):
        text = "First sentence here. Second sentence follows. Third trails off"
        self.assertFalse(scrape.cut_on_boundary(text, 40).endswith("Secon"))
        self.assertEqual(scrape.cut_on_boundary(text, 500), text)


class TranslateBodyTest(unittest.TestCase):
    BODY = "\n\n".join(f"Paragraph {index} " + "word " * 120 for index in range(12))

    @staticmethod
    def _echo_first_paragraph(prompt: str) -> dict[str, str]:
        """用片段的首个段落号当译文，方便断言拼接顺序。"""
        return {"body_cn": prompt.split("正文：\n", 1)[1].split(" ", 2)[1]}

    def test_splits_long_body_and_joins_in_order(self):
        with mock.patch.object(
            daily.report, "_llm_json", side_effect=self._echo_first_paragraph
        ) as llm:
            text, covered = daily.translate_body(self.BODY, config.BODY_TRANSLATE_LIMIT_FULL)
        self.assertEqual(llm.call_count, 3)
        self.assertEqual(text.split("\n\n"), ["0", "4", "8"])
        self.assertEqual(covered, len(self.BODY.strip()))

    def test_keeps_successful_prefix_when_a_chunk_fails(self):
        def flaky(prompt: str) -> dict[str, str]:
            snippet = prompt.split("正文：\n", 1)[1]
            if "Paragraph 4" in snippet:  # 第二段片段失败
                raise RuntimeError("rate limited")
            return self._echo_first_paragraph(prompt)

        with mock.patch.object(daily.report, "_llm_json", side_effect=flaky):
            text, covered = daily.translate_body(self.BODY, config.BODY_TRANSLATE_LIMIT_FULL)
        self.assertEqual(text, "0")
        self.assertLess(covered, len(self.BODY.strip()))

    def test_tier_sends_p0_and_high_impact_to_the_full_limit(self):
        self.assertEqual(daily.translate_limit_for("P0", 0), config.BODY_TRANSLATE_LIMIT_FULL)
        self.assertEqual(daily.translate_limit_for("P2", 95), config.BODY_TRANSLATE_LIMIT_FULL)
        self.assertEqual(daily.translate_limit_for("P2", 40), config.BODY_TRANSLATE_LIMIT)

    def test_truncated_flag_follows_recorded_coverage(self):
        fields = {"原文": "a" * 9000, "中文正文": "译文。", daily.TRANSLATED_CHARS_FIELD: 9000}
        self.assertFalse(daily.display_body(fields)["bodyTruncated"])
        fields[daily.TRANSLATED_CHARS_FIELD] = 6000
        self.assertTrue(daily.display_body(fields)["bodyTruncated"])

    def test_skips_retranslation_when_coverage_already_complete(self):
        fields = {
            "原文": "English body. " * 40,
            "中文正文": "已有译文。",
            daily.TRANSLATED_CHARS_FIELD: 10_000,
        }
        with mock.patch.object(daily.report, "_llm_json") as llm:
            self.assertEqual(daily._ensure_body_cn(fields), {})
        llm.assert_not_called()

    def test_retranslates_when_promoted_to_the_full_limit(self):
        fields = {
            "原文": "English body sentence. " * 400,
            "中文正文": "旧的截断译文。",
            daily.TRANSLATED_CHARS_FIELD: config.BODY_TRANSLATE_LIMIT,
        }
        with mock.patch.object(
            daily.report,
            "_llm_json",
            side_effect=lambda prompt: {"body_cn": "译:" + prompt.split("正文：\n", 1)[1][:7]},
        ):
            updates = daily._ensure_body_cn(fields, priority="P0", impact=10)
        self.assertTrue(updates["中文正文"].startswith("译:English"))
        self.assertGreater(updates[daily.TRANSLATED_CHARS_FIELD], config.BODY_TRANSLATE_LIMIT)


class DeepAnalysisTest(unittest.TestCase):
    def test_paper_uses_research_specific_structure(self):
        requirement = daily.analysis_requirement(is_paper=True, is_social=False)
        self.assertIn("【研究问题与核心结论】", requirement)
        self.assertIn("【实验设计与关键结果】", requirement)
        self.assertIn("【证据强度与局限】", requirement)
        self.assertIn("不要输出“行动建议”", requirement)
        self.assertNotIn("【行动建议】", requirement)

    def test_structured_editorial_keeps_original_headings(self):
        section = "这是具有事实、机制与判断的长段落。" * 40
        fields = {
            "来源类型": "公众号",
            "source_id": "huxiu",
            "来源": "虎嗅",
            "原文": (
                f"导语。\n\n## 直接原因：模型危机\n\n{section}\n\n"
                f"## 深层原因：组织链条过长\n\n{section}\n\n"
                f"## 对行业的影响\n\n{section}"
            ),
        }
        self.assertTrue(daily.editorial_structure_mode(fields))
        requirement = daily.analysis_requirement(
            is_paper=False,
            is_social=False,
            preserve_structure=True,
        )
        self.assertIn("沿用原文已有的小标题", requirement)
        self.assertIn("## 中文小标题 || 原文小标题", requirement)
        self.assertIn("硬上限 1400 字", requirement)
        self.assertNotIn("【核心内容】", requirement)

    def test_english_only_headings_trigger_rebuild_for_chinese(self):
        """英文小标题必须配中文，存量解读缺中文时要重新生成而不是照搬。"""
        section = "该章节包含足够多的事实和分析。" * 40
        fields = {
            "来源类型": "公众号",
            "source_id": "nvidia-blog",
            "来源": "NVIDIA Blog",
            "原文": (
                f"## World Models Are the Foundation\n\n{section}\n\n"
                f"## Cosmos 3: The Frontier Model\n\n{section}\n\n"
                f"## How Developers Put It to Work\n\n{section}"
            ),
            "AI深度解读": (
                "## World Models Are the Foundation\n旧版只有英文小标题。\n\n"
                "## Cosmos 3: The Frontier Model\n旧版只有英文小标题。"
            ),
        }
        self.assertTrue(daily.editorial_headings_need_cn(fields["AI深度解读"]))
        rebuilt = (
            "## 世界模型是物理 AI 的基础 || World Models Are the Foundation\n精编。\n\n"
            "## Cosmos 3：前沿模型 || Cosmos 3: The Frontier Model\n精编。"
        )
        self.assertFalse(daily.editorial_headings_need_cn(rebuilt))
        analysis = {"summary_cn": "摘要", "why": "重要"}
        with mock.patch.object(
            daily.report, "_llm_json", return_value={"deep_analysis_cn": rebuilt}
        ):
            updates = daily._ensure_deep_analysis(fields, analysis)
        self.assertEqual(updates, {"AI深度解读": rebuilt})

    def test_chinese_headings_are_not_rebuilt(self):
        section = "该章节包含足够多的事实和分析。" * 40
        fields = {
            "来源类型": "公众号",
            "source_id": "huxiu",
            "来源": "虎嗅",
            "原文": (
                f"## 直接原因\n\n{section}\n\n"
                f"## 深层原因\n\n{section}\n\n"
                f"## 行业影响\n\n{section}"
            ),
            "AI深度解读": "## 直接原因\n精编。\n\n## 深层原因\n精编。\n\n## 行业影响\n精编。",
        }
        analysis = {"summary_cn": "摘要", "why": "重要"}
        with mock.patch.object(daily.report, "_llm_json") as llm:
            self.assertEqual(daily._ensure_deep_analysis(fields, analysis), {})
        llm.assert_not_called()

    def test_structured_editorial_is_compacted_without_reordering_headings(self):
        sections = "\n\n".join(
            f"## 小节{index}\n" + ("这是包含事实与判断的详细段落。" * 45)
            for index in range(1, 8)
        )
        compacted = daily.compact_editorial_analysis(sections)
        self.assertLessEqual(len(compacted), 1400)
        self.assertEqual(compacted.count("## "), 6)
        self.assertLess(compacted.index("## 小节1"), compacted.index("## 小节6"))
        self.assertNotIn("## 小节7", compacted)

    def test_short_or_unstructured_news_keeps_standard_framework(self):
        fields = {
            "来源类型": "公众号",
            "原文": "一条没有小标题的短消息。" * 80,
        }
        self.assertFalse(daily.editorial_structure_mode(fields))
        requirement = daily.analysis_requirement(is_paper=False, is_social=False)
        self.assertIn("【核心内容】", requirement)

    def test_old_fixed_analysis_is_rebuilt_for_structured_editorial(self):
        section = "该章节包含足够多的事实和分析。" * 40
        fields = {
            "来源类型": "公众号",
            "source_id": "huxiu",
            "来源": "虎嗅",
            "原文": (
                f"## 直接原因\n\n{section}\n\n"
                f"## 深层原因\n\n{section}\n\n"
                f"## 行业影响\n\n{section}"
            ),
            "AI深度解读": "【核心内容】\n旧版固定结构。",
        }
        analysis = {"summary_cn": "摘要", "why": "重要"}
        deep = "## 直接原因\n精编。\n\n## 深层原因\n精编。\n\n## 行业影响\n精编。"
        with mock.patch.object(
            daily.report, "_llm_json", return_value={"deep_analysis_cn": deep}
        ) as llm:
            updates = daily._ensure_deep_analysis(fields, analysis)
        self.assertEqual(updates, {"AI深度解读": deep})
        self.assertIn("不得套用", llm.call_args.args[0])
        self.assertEqual(analysis["editorial_structure"], "source")

    def test_old_paper_analysis_version_is_rebuilt(self):
        fields = {
            "状态": "已分析",
            "来源类型": "论文",
            "中文摘要": "已有摘要",
            "论文指标": json.dumps(
                {"llm": {"analysis_version": daily.PAPER_ANALYSIS_VERSION - 1}}
            ),
        }
        self.assertIsNone(daily._existing_analysis(fields))
        fields["论文指标"] = json.dumps(
            {"llm": {"analysis_version": daily.PAPER_ANALYSIS_VERSION}}
        )
        self.assertIsNotNone(daily._existing_analysis(fields))

    def test_backfills_structured_deep_analysis(self):
        fields = {
            "标题": "A new model",
            "来源": "Official Blog",
            "原文": "The model improves tool use and lowers inference cost. " * 20,
        }
        analysis = {"summary_cn": "模型能力提升。", "why": "有助于降低部署成本。"}
        deep = (
            "【核心内容】\n模型发布。\n\n【关键细节】\n工具调用提升。\n\n"
            "【价值与影响】\n成本下降。\n\n【局限与风险】\n原文未披露评测细节。\n\n"
            "【行动建议】\n在内部数据集复测。"
        )
        with mock.patch.object(
            daily.report, "_llm_json", return_value={"deep_analysis_cn": deep}
        ) as llm:
            updates = daily._ensure_deep_analysis(fields, analysis)
        self.assertEqual(updates, {"AI深度解读": deep})
        self.assertEqual(fields["AI深度解读"], deep)
        self.assertEqual(analysis["deep_analysis_cn"], deep)
        prompt = llm.call_args.args[0]
        self.assertIn("【局限与风险】", prompt)
        self.assertIn("不得虚构", prompt)

    def test_reuses_cached_deep_analysis(self):
        fields = {
            "原文": "English body. " * 30,
            "AI深度解读": "【核心内容】\n已有解读。",
        }
        analysis = {"summary_cn": "摘要", "why": "重要"}
        with mock.patch.object(daily.report, "_llm_json") as llm:
            self.assertEqual(daily._ensure_deep_analysis(fields, analysis), {})
        llm.assert_not_called()
        self.assertEqual(analysis["deep_analysis_cn"], "【核心内容】\n已有解读。")


if __name__ == "__main__":
    unittest.main()
