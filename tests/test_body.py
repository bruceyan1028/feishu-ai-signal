"""正文清洗、段落保留与译文回退的单元测试（不打外网）。"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from src import config, daily, process, publish, rss, scrape


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

    def test_extracts_formatted_jsonld_date_published(self):
        """格式化 JSON-LD 冒号后有空格；旧正则只认紧凑写法会整级失效。"""
        html = """
        <script type="application/ld+json">
        {
          "@type": "BlogPosting",
          "datePublished": "2026-02-26T11:21:27.797Z",
          "dateModified": "2026-02-25T15:00:00.000Z"
        }
        </script>
        """
        self.assertEqual(
            scrape.extract_published_date_html(html),
            "2026-02-26T11:21:27.797Z",
        )

    def test_extracts_formatted_ssr_pubdate(self):
        """品玩等站 SSR 里是 pubDate: \"…\"（冒号后空格 + 大小写混用）。"""
        html = '<script>window.__DATA__ = {"pubDate": "2026-08-31T07:54:53.000Z"}</script>'
        self.assertEqual(
            scrape.extract_published_date_html(html),
            "2026-08-31T07:54:53.000Z",
        )

    def test_compact_json_date_still_works(self):
        html = '{"datePublished":"2026-03-01T00:00:00Z"}'
        self.assertEqual(
            scrape.extract_published_date_html(html),
            "2026-03-01T00:00:00Z",
        )

    def test_url_path_date_as_fallback(self):
        """HTML 抽不到时，用路径 /YYYY-MM-DD/ 兜底（财新等）。"""
        url = "https://www.caixin.com/2026-08-28/102123456.html"
        self.assertEqual(scrape._published_date_from_url(url), "2026-08-28")
        self.assertEqual(
            scrape.extract_published_date_html("<html><body>乱码页</body></html>", url),
            "2026-08-28",
        )

    def test_url_path_date_slash_separated(self):
        self.assertEqual(
            scrape._published_date_from_url("https://example.com/blog/2026/03/15/post"),
            "2026-03-15",
        )

    def test_url_path_date_rejects_invalid_calendar(self):
        self.assertEqual(
            scrape._published_date_from_url("https://example.com/2026-13-40/x.html"),
            "",
        )

    def test_url_path_date_does_not_override_meta(self):
        html = '<meta property="article:published_time" content="2026-01-02T00:00:00Z">'
        url = "https://example.com/2026-08-28/post.html"
        self.assertEqual(
            scrape.extract_published_date_html(html, url),
            "2026-01-02T00:00:00Z",
        )

    def test_link_recency_prefers_fuller_path_date(self):
        older = "https://ex.com/2026-01-01/a"
        newer = "https://ex.com/2026-08-28/z"
        year_only = "https://ex.com/2025/post-alpha"
        self.assertGreater(
            scrape._link_recency_key(newer),
            scrape._link_recency_key(older),
        )
        self.assertGreater(
            scrape._link_recency_key(older),
            scrape._link_recency_key(year_only),
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

    def test_anthropic_news_table_keeps_date_order(self):
        html = """
        <div class="PublicationList">
          <ul>
            <li><a href="/news/claude-text-watermark" class="PublicationList-module__listItem">
              <time>Aug 14, 2026</time><span class="subject">Announcements</span>
              <span class="title">How Claude's text watermark works</span>
            </a></li>
            <li><a href="/news/improving-fable-5-s-biology-safeguards" class="PublicationList-module__listItem">
              <time>Aug 7, 2026</time><span class="subject">Product</span>
              <span class="title">Improving Fable 5's biology safeguards</span>
            </a></li>
            <li><a href="/news/tino-cuellar" class="PublicationList-module__listItem">
              <time>Aug 4, 2026</time><span class="subject">Announcements</span>
              <span class="title">Tino Cuellar to join Anthropic</span>
            </a></li>
          </ul>
        </div>
        <a href="/news/zzz-old-featured">featured decoy</a>
        """
        feed = {"id": "anthropic-news", "url": "https://www.anthropic.com/news", "max_articles": 8}
        links = scrape._extract_links_for_feed(html, feed, use_jina=False)
        self.assertEqual(
            [item["url"].rsplit("/", 1)[-1] for item in links],
            ["claude-text-watermark", "improving-fable-5-s-biology-safeguards", "tino-cuellar"],
        )
        self.assertEqual(links[0]["title"], "How Claude's text watermark works")
        self.assertEqual(links[0]["published_raw"], "Aug 14, 2026")

    def test_link_path_include_relaxes_list_prefix_depth(self):
        """聚合列表页（/latest）链到其它栏目时，白名单应放行，不再要求 /latest/ 前缀。"""
        html = """
        <a href="/latest">Latest</a>
        <a href="/about">About</a>
        <a href="/publications/gpu-pricing-2026">GPU Pricing</a>
        <a href="/gradient-updates/chip-costs">Chip Costs</a>
        <a href="/data-insights/h100-fleet">H100 Fleet</a>
        <a href="/topics/compute">Topics</a>
        """
        feed = {
            "id": "epoch-compute",
            "url": "https://epoch.ai/latest",
            "max_articles": 8,
            "extra_config": {
                "link_path_include": "/gradient-updates/|/publications/|/data-insights/",
            },
        }
        links = scrape._extract_links_html(html, feed)
        urls = [item["url"] for item in links]
        self.assertEqual(
            urls,
            [
                "https://epoch.ai/publications/gpu-pricing-2026",
                "https://epoch.ai/gradient-updates/chip-costs",
                "https://epoch.ai/data-insights/h100-fleet",
            ],
        )

    def test_list_neighbor_date_fills_published_raw(self):
        """列表卡片邻近日期写入 published_raw，并按日期而非字母序截断。"""
        html = """
        <article>
          <img alt="old"/>
          Newsletter Jul. 22, 2026
          <a href="/gradient-updates/old-post">Old Post</a>
          summary of old post.
        </article>
        <article>
          <img alt="new"/>
          Newsletter Aug. 27, 2026
          <a href="/gradient-updates/new-post">New Post</a>
          summary of new post.
        </article>
        <article>
          Report Aug. 24, 2026
          <a href="/publications/mid-report">Mid Report</a>
        </article>
        """
        feed = {
            "id": "epoch-compute",
            "url": "https://epoch.ai/latest",
            "max_articles": 2,
            "extra_config": {
                "link_path_include": "/gradient-updates/|/publications/",
            },
        }
        links = scrape._extract_links_html(html, feed)
        self.assertEqual(
            [item["url"].rsplit("/", 1)[-1] for item in links],
            ["new-post", "mid-report"],
        )
        self.assertEqual(links[0]["published_raw"], "Aug. 27, 2026")
        self.assertEqual(links[1]["published_raw"], "Aug. 24, 2026")

    def test_url_date_wins_over_neighbor_date(self):
        html = """
        <a href="/2026-01-01/post.html">Post</a> Aug. 27, 2026
        """
        feed = {"id": "caixin", "url": "https://www.caixin.com/", "max_articles": 4}
        links = scrape._extract_links_html(html, feed)
        self.assertEqual(links[0]["published_raw"], "2026-01-01")

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

    def test_google_cloud_prefers_architecture_figures_over_hero_and_related_cards(self):
        url = "https://cloud.google.com/blog/products/ai/demo"
        html = """
        <article>
          <img class="rFf1Dd" src="https://storage.googleapis.com/x/marketing-hero.png">
          <p>Data enters the processing pipeline.</p>
          <img class="JcsBte" src="https://storage.googleapis.com/x/data-flow-architecture.jpg">
          <p>The deployment architecture keeps production immutable.</p>
          <img class="JcsBte" src="https://storage.googleapis.com/x/platform_engineering.png">
        </article>
        <section class="related">
          <img class="D5RK8d" src="https://storage.googleapis.com/x/other.max-700x700.png">
        </section>
        """
        self.assertEqual(
            [item["url"] for item in rss.extract_article_evidence_images(html, url)],
            [
                "https://storage.googleapis.com/x/data-flow-architecture.jpg",
                "https://storage.googleapis.com/x/platform_engineering.png",
            ],
        )

    def test_google_deepmind_uses_full_size_lazy_loaded_benchmark_charts(self):
        self.assertTrue(
            rss.strict_evidence_image_source(
                {
                    "sourceId": "google-deepmind-blog",
                    "url": "https://deepmind.google/blog/demo",
                }
            )
        )
        self.assertTrue(
            rss.strict_evidence_image_source(
                "https://blog.google/innovation-and-ai/models-and-research/demo/"
            )
        )
        url = "https://blog.google/innovation-and-ai/models-and-research/gemini-models/demo/"
        html = """
        <article>
          <img alt="Gemini launch hero"
               src="https://storage.googleapis.com/x/gemini.width-200.webp">
          <img alt="a chart showing production code quality"
               src="https://storage.googleapis.com/x/frontier.width-100.webp"
               data-loading='{ "mobile": "https://storage.googleapis.com/x/frontier.width-500.webp",
                               "desktop": "https://storage.googleapis.com/x/frontier.width-1000.webp" }'>
          <img alt="a chart showing enterprise workflow automation"
               src="https://storage.googleapis.com/x/automation.width-100.webp"
               data-loading='{ "mobile": "https://storage.googleapis.com/x/automation.width-500.webp",
                               "desktop": "https://storage.googleapis.com/x/automation.width-1000.webp" }'>
          <img alt="Quote from a customer"
               src="https://storage.googleapis.com/x/testimonial.width-1000.webp">
        </article>
        """
        self.assertEqual(
            [item["url"] for item in rss.extract_article_evidence_images(html, url)],
            [
                "https://storage.googleapis.com/x/frontier.width-1000.webp",
                "https://storage.googleapis.com/x/automation.width-1000.webp",
            ],
        )

    def test_huxiu_drops_people_and_decorative_illustrations(self):
        url = "https://www.huxiu.com/article/1.html"
        html = """
        <article>
          <p>本·伯南克表示社会需要提前准备。</p>
          <img data-w="554" data-h="367" src="https://img.huxiucdn.com/article/content/person.png">
          <p>本·伯南克，图源：诺贝尔官网</p>
          <p>工业革命时期的工厂，图源：英国国家博物馆</p>
          <img data-w="554" data-h="317" src="https://img.huxiucdn.com/article/content/factory.png">
        </article>
        """
        self.assertEqual(rss.extract_article_evidence_images(html, url), [])

    def test_huxiu_keeps_a_body_chart_when_context_identifies_it(self):
        url = "https://www.huxiu.com/article/1.html"
        html = """
        <article>
          <p>下图对比了不同模型的推理成本与准确率趋势。</p>
          <img data-w="1000" data-h="620" src="https://img.huxiucdn.com/article/content/model-cost.png">
          <p>主流大模型 API 输出价格对比。</p>
          <img data-w="1080" data-h="729" src="https://img.huxiucdn.com/article/content/model-price.png">
        </article>
        """
        self.assertEqual(
            [item["url"] for item in rss.extract_article_evidence_images(html, url)],
            [
                "https://img.huxiucdn.com/article/content/model-cost.png",
                "https://img.huxiucdn.com/article/content/model-price.png",
            ],
        )

    def test_openai_strict_curation_drops_art_card_and_keeps_evaluation_chart(self):
        signal = {
            "sourceId": "openai-news",
            "url": "https://openai.com/index/demo",
            "contentType": "文章",
            "titleCn": "模型安全评测",
            "imageUrl": "https://images.ctfassets.net/x/Art_Card.png",
            "mediaAssets": {
                "images": [
                    {"url": "https://images.ctfassets.net/x/Art_Card.png", "alt": ""},
                    {
                        "url": "https://images.ctfassets.net/x/exploitgym-inline-results.png",
                        "alt": "ExploitGym evaluation results",
                        "kind": "article-cover",
                    },
                ],
                "videos": [],
            },
        }
        media, primary = rss.curate_display_media(signal)
        self.assertEqual(primary, "https://images.ctfassets.net/x/exploitgym-inline-results.png")
        self.assertEqual(len(media["images"]), 1)
        self.assertEqual(media["images"][0]["kind"], "article-figure")

    @mock.patch.object(rss.requests, "get")
    def test_wallstreetcn_uses_detail_api_and_drops_membership_promo(self, get):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "image": {"uri": "https://wpimg-wscn.awtmt.com/decorative-cover.jpeg"},
                "content": """
                  <p>从上面的散点图可以看到，智能指数与价格正相关。</p>
                  <img class="wscnph" data-wscntype="image" data-wscnw="634"
                       data-wscnh="435" src="https://wpimg-wscn.awtmt.com/chart.png">
                  <p>风险提示</p>
                  <img class="shield-text wscnph" data-wscntype="image"
                       data-wscnw="1080" data-wscnh="3130"
                       src="https://wpimg-wscn.awtmt.com/member-promo.png">
                """,
            }
        }
        get.return_value = response
        result = rss.fetch_article_media("https://wallstreetcn.com/articles/3779138")
        self.assertIn("/apiv1/content/articles/3779138?extract=0", get.call_args.args[0])
        self.assertEqual(
            [item["url"] for item in result["images"]],
            ["https://wpimg-wscn.awtmt.com/chart.png"],
        )

    def test_strict_sources_keep_multiple_figures_and_never_fall_back_to_cover(self):
        signal = {
            "sourceId": "gcp-ai-infra",
            "url": "https://cloud.google.com/blog/products/ai/demo",
            "contentType": "文章",
            "imageUrl": "https://storage.googleapis.com/x/marketing-hero.png",
            "mediaAssets": {
                "images": [
                    {
                        "url": "https://storage.googleapis.com/x/marketing-hero.png",
                        "alt": "营销封面",
                        "kind": "article-cover",
                    }
                ],
                "videos": [],
            },
        }
        bundle = {
            "cover": "https://storage.googleapis.com/x/marketing-hero.png",
            "images": [
                {"url": "https://storage.googleapis.com/x/data-flow-architecture.jpg", "alt": ""},
                {"url": "https://storage.googleapis.com/x/benchmark-chart.png", "alt": ""},
            ],
        }
        media, primary = rss.curate_display_media(signal, bundle)
        self.assertEqual(primary, bundle["images"][0]["url"])
        self.assertEqual(len(media["images"]), 2)
        self.assertNotIn(bundle["cover"], [item["url"] for item in media["images"]])

    def test_candidate_extractor_keeps_nearby_text_and_drops_avatars(self):
        html = """
        <article>
          <p>下图对比了不同模型的推理成本。</p>
          <img src="/model-cost.png" alt="成本曲线" width="1000" height="620">
          <img src="/author-avatar.jpg" alt="作者头像" width="400" height="400">
          <img src="/icon.png" width="24" height="24">
        </article>
        """
        candidates = rss.extract_article_image_candidates(html, self.PAGE)
        self.assertEqual(
            [item["url"] for item in candidates],
            ["https://example.com/model-cost.png"],
        )
        self.assertIn("推理成本", candidates[0]["context"])

    def test_analysis_headings_read_chinese_and_markdown_sections(self):
        self.assertEqual(
            rss.analysis_section_headings("【发生了什么】正文\n## 关键结果\n结论"),
            ["发生了什么", "关键结果"],
        )

    @mock.patch.object(rss, "_llm_pick_article_images")
    def test_pushed_article_uses_llm_cover_and_section_figures(self, pick):
        pick.return_value = {
            "cover_index": 0,
            "body": [
                {"index": 1, "after_heading": "关键结果", "alt": "评测曲线"},
                {"index": 0, "after_heading": "发生了什么", "alt": "应被去重"},
                {"index": 99, "after_heading": "关键结果", "alt": "越界"},
            ],
        }
        signal = {
            "contentType": "文章",
            "titleCn": "新模型发布",
            "deepAnalysis": "【发生了什么】发布。\n【关键结果】评测上升。",
            "mediaAssets": {"images": [], "videos": []},
        }
        bundle = {
            "cover": "https://example.com/hero.jpg",
            "excerpt": "原文讲评测曲线。",
            "candidates": [
                {"url": "https://example.com/hero.jpg", "alt": "现场", "context": "封面"},
                {"url": "https://example.com/chart.png", "alt": "曲线", "context": "评测"},
            ],
            "images": [],
        }
        media, cover = rss.select_pushed_article_images(signal, bundle)
        self.assertEqual(cover, "https://example.com/hero.jpg")
        self.assertEqual(media["curatedBy"], "llm")
        self.assertEqual(media["cover"], cover)
        self.assertEqual(
            media["images"],
            [
                {
                    "url": "https://example.com/chart.png",
                    "alt": "评测曲线",
                    "kind": "article-figure",
                    "afterHeading": "关键结果",
                }
            ],
        )

    @mock.patch.object(rss, "_llm_pick_article_images", return_value=None)
    def test_pushed_article_falls_back_to_heuristic_when_llm_unavailable(self, _pick):
        signal = {
            "contentType": "文章",
            "titleCn": "Jeff Dean 创办新公司",
            "imageUrl": "https://example.com/lucas.png",
            "mediaAssets": {"images": [], "videos": []},
        }
        media, cover = rss.select_pushed_article_images(
            signal, "https://example.com/jeff-dean.jpg"
        )
        self.assertEqual(cover, "https://example.com/jeff-dean.jpg")
        self.assertEqual(media["images"][0]["kind"], "article-cover")
        self.assertNotEqual(media.get("curatedBy"), "llm")

    @mock.patch.object(rss, "fetch_article_media")
    def test_publish_skips_refetch_when_llm_already_curated(self, fetch):
        signal = {
            "contentType": "文章",
            "url": "https://example.com/a",
            "imageUrl": "https://example.com/cover.jpg",
            "mediaAssets": {
                "curatedBy": "llm",
                "cover": "https://example.com/cover.jpg",
                "images": [
                    {
                        "url": "https://example.com/fig.png",
                        "afterHeading": "关键结果",
                        "kind": "article-figure",
                    }
                ],
            },
        }
        publish.curate_web_media([{"signals": [signal]}])
        fetch.assert_not_called()
        self.assertEqual(signal["imageUrl"], "https://example.com/cover.jpg")
        self.assertEqual(signal["mediaAssets"]["images"][0]["afterHeading"], "关键结果")


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
        # 中文媒体与公众号照抄原文，精编只留给其它分类的长文
        fields = {
            "来源类型": "纯网页",
            "source_id": "nvidia-blog",
            "来源": "NVIDIA Blog",
            "分类": "算力芯片云",
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
            "来源类型": "纯网页",
            "source_id": "nvidia-blog",
            "来源": "NVIDIA Blog",
            "分类": "算力芯片云",
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
            "来源类型": "纯网页",
            "source_id": "nvidia-blog",
            "来源": "NVIDIA Blog",
            "分类": "算力芯片云",
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
            "来源类型": "纯网页",
            "source_id": "nvidia-blog",
            "来源": "NVIDIA Blog",
            "分类": "算力芯片云",
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


class VerbatimBodyTest(unittest.TestCase):
    """中文媒体与公众号：详情页照抄原文，连各级小标题一起保留。"""

    def _wechat_fields(self, body: str) -> dict:
        return {
            "标题": "国产大模型的一次组织重构",
            "来源": "虎嗅",
            "来源类型": "公众号",
            "source_id": "huxiu",
            "分类": "中文媒体",
            "原文": body,
        }

    def _long_chinese_body(self) -> str:
        section = "这一段包含具体的事实、数字与作者判断。" * 12
        return (
            f"导语交代事件本身。\n\n## 直接原因：模型危机\n\n{section}\n\n"
            f"### 时间线\n\n{section}\n\n#### 一处细节\n\n{section}\n\n"
            f"## 对行业的影响\n\n{section}"
        )

    def test_chinese_wechat_article_goes_verbatim(self):
        fields = self._wechat_fields(self._long_chinese_body())
        self.assertTrue(daily.verbatim_body_mode(fields))
        # 照抄原文时不再做原结构精编，结构由正文本身保留
        self.assertFalse(daily.editorial_structure_mode(fields))
        requirement = daily.analysis_requirement(
            is_paper=False, is_social=False, verbatim_body=True
        )
        self.assertIn("照抄原文", requirement)
        self.assertNotIn("【核心内容】", requirement)

    def test_chinese_media_web_article_goes_verbatim(self):
        fields = self._wechat_fields(self._long_chinese_body())
        fields.update({"来源类型": "纯网页", "来源": "机器之心", "source_id": "jiqizhixin"})
        self.assertEqual(daily.content_type(fields), "文章")
        self.assertTrue(daily.verbatim_body_mode(fields))

    def test_english_and_short_bodies_keep_deep_analysis(self):
        english = self._wechat_fields("The company restructured its model team. " * 60)
        self.assertFalse(daily.verbatim_body_mode(english))
        short = self._wechat_fields("只抓到一句导语。")
        self.assertFalse(daily.verbatim_body_mode(short))
        paper = {"来源类型": "论文", "分类": "中文媒体", "原文": self._long_chinese_body()}
        self.assertFalse(daily.verbatim_body_mode(paper))

    def test_verbatim_signal_carries_body_and_drops_deep_analysis(self):
        fields = self._wechat_fields(self._long_chinese_body())
        analysis = {
            "title_cn": "国产大模型的一次组织重构",
            "summary_cn": "摘要。",
            "deep_analysis_cn": "【核心内容】\n存量解读。",
            "why": "重要。",
            "impact": 70,
            "novelty": 60,
            "actionability": 50,
            "urgency": "中",
            "topics": ["LLM"],
        }
        signal = daily._signal_from_fields("rec1", fields, analysis)
        self.assertTrue(signal["bodyVerbatim"])
        self.assertEqual(signal["deepAnalysis"], "")
        self.assertEqual(signal["editorialStructure"], "")
        for marker in ("## 直接原因：模型危机", "### 时间线", "#### 一处细节"):
            self.assertIn(marker, signal["body"])

    def test_verbatim_entries_skip_deep_analysis_backfill(self):
        fields = self._wechat_fields(self._long_chinese_body())
        fields["AI深度解读"] = "【核心内容】\n存量解读。"
        analysis = {"summary_cn": "摘要", "why": "重要"}
        with mock.patch.object(daily.report, "_llm_json") as llm:
            self.assertEqual(daily._ensure_deep_analysis(fields, analysis), {})
        llm.assert_not_called()
        self.assertEqual(analysis["deep_analysis_cn"], "")

    def test_published_record_carries_verbatim_body(self):
        record = {
            "record_id": "rec2",
            "fields": {
                **self._wechat_fields(self._long_chinese_body()),
                "中文摘要": "摘要。",
                "AI深度解读": "【核心内容】\n存量解读。",
                "链接": "https://mp.weixin.qq.com/s/abc",
            },
        }
        signal = publish._signal_from_record(record)
        self.assertTrue(signal["bodyVerbatim"])
        self.assertEqual(signal["deepAnalysis"], "")
        self.assertIn("## 对行业的影响", signal["body"])

    def test_tail_notices_and_brand_signature_are_dropped(self):
        body = (
            "正文最后一段结论。\n\n"
            "AI行业信号 频道: 前沿科技\n\n"
            "本内容由作者授权发布，观点仅代表作者本人，不代表虎嗅立场。\n\n"
            "如对本稿件有异议或投诉，请联系 tougao@huxiu.com。"
        )
        self.assertEqual(daily.clean_body(body, "虎嗅"), "正文最后一段结论。")
        self.assertEqual(
            daily.clean_body("正文结论。\n\n雷峰网 雷峰网", "AI科技评论 / 雷峰网"),
            "正文结论。",
        )
        # 只清理尾部：同样的说法出现在正文中段时不动
        middle = "本内容由作者授权发布，仅供参考。\n\n真正的正文段落。"
        self.assertEqual(daily.clean_body(middle, "虎嗅"), middle)

    def test_html_extraction_keeps_heading_levels(self):
        html = (
            "<h2>一级小节</h2><p>段落一。</p>"
            "<h3>二级小节</h3><p>段落二。</p>"
            "<h4>三级小节</h4><p>段落三。</p>"
            "<h5>更深一层</h5><p>段落四。</p>"
        )
        text = scrape.html_to_text(html)
        self.assertIn("## 一级小节", text)
        self.assertIn("### 二级小节", text)
        self.assertIn("#### 三级小节", text)
        # h5/h6 罕见，并到四级，不再生出更深的层级
        self.assertIn("#### 更深一层", text)


if __name__ == "__main__":
    unittest.main()
