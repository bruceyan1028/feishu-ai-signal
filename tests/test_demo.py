from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src import config, daily, main, notify, process, publish, rss, scrape, sources


class PipelineTests(unittest.TestCase):
    def test_only_active_rss_sources_are_mapped(self) -> None:
        records = [
            {"fields": {"source_id": "rss", "name": "RSS", "status": "active", "fetch_method": "RSS", "endpoint": "https://example.com/rss"}},
            {"fields": {"source_id": "scrape", "status": "active", "fetch_method": "Scrape", "endpoint": "https://example.com"}},
            {"fields": {"source_id": "paused", "status": "paused", "fetch_method": "RSS", "endpoint": "https://example.com/paused"}},
        ]
        self.assertEqual([item["id"] for item in sources.map_feed_sources(records)], ["rss"])

    def test_explicit_source_type_overrides_heuristics(self) -> None:
        records = [
            {
                "fields": {
                    "source_id": "openai-news",
                    "name": "OpenAI",
                    "status": "active",
                    "fetch_method": "RSS",
                    "endpoint": "https://openai.com/news/rss.xml",
                    "来源类型": "论文",
                }
            }
        ]
        feeds = sources.map_feed_sources(records)
        self.assertEqual(feeds[0]["source_type"], "论文")
        self.assertTrue(sources.is_paper_source(source_id="jmlr", entity_type="paper"))
        self.assertEqual(
            sources.catalog_signal_format("Hugging Face Papers Trending"),
            "论文",
        )

    def test_scrape_diag_mapping_includes_b_class(self) -> None:
        records = [
            {
                "fields": {
                    "source_id": "chatbot-arena",
                    "name": "Arena",
                    "status": "active",
                    "fetch_method": "Scrape",
                    "endpoint": "https://lmarena.ai/",
                }
            },
            {
                "fields": {
                    "source_id": "anthropic-news",
                    "name": "Anthropic",
                    "status": "active",
                    "fetch_method": "Scrape",
                    "endpoint": "https://www.anthropic.com/news",
                }
            },
        ]
        prod = sources.map_scrape_sources(records)
        self.assertEqual([f["id"] for f in prod], ["anthropic-news"])
        diag = sources.map_scrape_sources_for_diag(records)
        self.assertEqual({f["id"] for f in diag}, {"chatbot-arena", "anthropic-news"})
        self.assertEqual(sources.scrape_cohort("openai-careers"), "招聘")
        self.assertEqual(sources.scrape_cohort("hf-papers-trending"), "论文站")

    def test_hf_pwc_extracts_only_paper_urls(self) -> None:
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
        props = {
            "dailyPapers": [
                {
                    "title": "Paper Alpha",
                    "paper": {
                        "id": "2607.11111",
                        "title": "Paper Alpha",
                        "publishedAt": recent,
                        "upvotes": 40,
                    },
                },
                {
                    "title": "Paper Beta",
                    "paper": {
                        "id": "2607.11886",
                        "title": "Paper Beta",
                        "publishedAt": recent,
                        "upvotes": 50,
                    },
                },
                {
                    "title": "Old Cold",
                    "paper": {
                        "id": "2403.08299",
                        "title": "Old Cold",
                        "publishedAt": "2024-03-13T00:00:00.000Z",
                        "upvotes": 15,
                    },
                },
                {
                    "title": "Old Hot",
                    "paper": {
                        "id": "2412.20138",
                        "title": "Old Hot",
                        "publishedAt": "2024-12-28T00:00:00.000Z",
                        "upvotes": 120,
                    },
                },
            ],
            "isTrending": True,
        }
        # HTML 实体编码的 data-props（与 HF 页面一致）
        encoded = (
            json.dumps(props, separators=(",", ":"))
            .replace("&", "&amp;")
            .replace('"', "&quot;")
        )
        html = f"""
        <a href="/papers/trending">Trending</a>
        <a href="/papers/date/2026-07-14">Jul 14</a>
        <a href="/join/discord">Discord</a>
        <a href="/inference/models">Models</a>
        <div class="SVELTE_HYDRATER" data-target="DailyPapers" data-props="{encoded}"></div>
        <a href="https://huggingface.co/papers/2607.11111">dup</a>
        """
        feed = {
            "id": "hf-papers-trending",
            "url": "https://huggingface.co/papers/trending",
            "max_articles": 10,
            "extra_config": {
                "recent_days": 7,
                "min_upvotes": 30,
                "high_upvote_threshold": 100,
            },
        }
        links = scrape._extract_hf_pwc_paper_links(html, feed)
        urls = [x["url"] for x in links]
        self.assertEqual(
            urls,
            [
                "https://huggingface.co/papers/2607.11111",
                "https://huggingface.co/papers/2607.11886",
                "https://huggingface.co/papers/2412.20138",
            ],
        )
        self.assertEqual(links[0]["title"], "Paper Alpha")
        self.assertFalse(links[0]["heat_keep"])
        self.assertTrue(links[2]["heat_keep"])
        # PwC source_id 同样走专用抽取
        self.assertTrue(
            scrape._is_hf_pwc_paper_feed(
                {"id": "papers-with-code-trending", "url": "https://paperswithcode.com/"}
            )
        )
        self.assertEqual(
            scrape._extract_links_for_feed(html, feed, use_jina=False),
            links,
        )

    def test_pwc_co_uses_trending_api(self) -> None:
        payload = [
            {
                "paper_id": "1",
                "arxiv_id": "2607.04439",
                "title": "ResearchStudio-Idea",
                "date_published": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "paper_age_days": 2,
                "trending": {"stars_gained_24h": 10},
            },
            {
                "paper_id": "2",
                "arxiv_id": "2605.23904",
                "title": "Old Hot SkillOpt",
                "date_published": "2026-05-22",
                "paper_age_days": 54,
                "trending": {"stars_gained_24h": 131},
            },
            {
                "paper_id": "3",
                "arxiv_id": "2403.08299",
                "title": "Old Cold",
                "date_published": "2024-03-13",
                "paper_age_days": 800,
                "trending": {"stars_gained_24h": 5},
            },
        ]

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> list:
                return payload

        feed = {
            "id": "papers-with-code-trending",
            "url": "https://paperswithcode.co/",
            "max_articles": 10,
            "extra_config": {"recent_days": 7, "high_stars_gained_24h": 100},
        }
        with patch("src.scrape.requests.get", return_value=_Resp()) as mocked:
            links = scrape._extract_hf_pwc_paper_links("<html></html>", feed)
        mocked.assert_called()
        self.assertEqual(
            [x["url"] for x in links],
            [
                "https://huggingface.co/papers/2607.04439",
                "https://huggingface.co/papers/2605.23904",
            ],
        )
        self.assertEqual(links[0]["title"], "ResearchStudio-Idea")
        self.assertFalse(links[0]["heat_keep"])
        self.assertTrue(links[1]["heat_keep"])


    def test_rss_endpoint_spaces_are_encoded(self) -> None:
        self.assertEqual(
            sources.normalize_endpoint("https://news.google.com/rss/search?q=artificial intelligence"),
            "https://news.google.com/rss/search?q=artificial%20intelligence",
        )

    def test_arxiv_is_capped_across_feeds(self) -> None:
        raw = []
        for index in range(config.MAX_ARXIV_ITEMS + 3):
            raw.append(
                {
                    "title": f"LLM reasoning paper {index}",
                    "url": f"https://arxiv.org/abs/2607.{index:05d}",
                    "body": "A study of agent planning and LLM inference. " * 20,
                    "published_raw": datetime.now(timezone.utc).isoformat(),
                    "feed": {
                        "id": f"arxiv-{index % 2}",
                        "name": "arXiv",
                        "fetch_method": "RSS",
                        "lookback_hours": 168,
                        "keyword_regex": r"(llm|agent|reasoning)",
                        "min_content_chars": 100,
                        "source_type": "论文",
                    },
                }
            )
        with patch.object(config, "PAPER_ENRICH_ENABLED", False):
            cleaned = process.process_and_clean(raw)
        self.assertGreater(len(cleaned), config.MAX_ARXIV_ITEMS)
        self.assertEqual(len(main.filter_new_items(cleaned, set())), config.MAX_ARXIV_ITEMS)

    def test_feishu_field_mapping_matches_real_schema(self) -> None:
        fields = process.format_for_feishu(
            {
                "title": "Title",
                "url": "https://example.com",
                "source": "Source",
                "source_type": "纯网页",
                "fetch_method": "RSS",
                "category": "前沿模型公司",
                "tier": "L1 一级官方",
                "published_ms": 1,
                "collected_ms": 2,
                "duplicate_key": "key",
                "image_url": "https://example.com/cover.jpg",
            }
        )
        self.assertEqual(fields["路由来源"], "RSS")
        self.assertEqual(fields["图片链接"]["link"], "https://example.com/cover.jpg")
        self.assertNotIn("取值来源", fields)

    def test_rss_prefers_original_media_image(self) -> None:
        entry = {"media_content": [{"url": "https://example.com/original.jpg"}]}
        self.assertEqual(rss._best_image(entry, ""), "https://example.com/original.jpg")
        meta = '<meta content="/images/article.jpg" property="og:image">'
        self.assertEqual(
            rss._meta_image_from_html(meta, "https://example.com/news/1"),
            "https://example.com/images/article.jpg",
        )
        media = rss._media_assets(
            {},
            '<img src="/figure-1.png"><iframe src="https://www.youtube.com/embed/demo123"></iframe>',
            "https://example.com/paper",
        )
        self.assertEqual(media["images"][0]["url"], "https://example.com/figure-1.png")
        self.assertEqual(media["videos"][0]["embedUrl"], "https://www.youtube-nocookie.com/embed/demo123")


class DailyTests(unittest.TestCase):
    def test_candidate_selection_respects_rss_set_and_arxiv_cap(self) -> None:
        now = datetime.now(timezone.utc)
        stamp = int(now.timestamp() * 1000)
        records = [
            {"record_id": "official", "fields": {"source_id": "official-rss", "发布时间": stamp, "链接": {"link": "https://example.com"}}},
            *[
                {
                    "record_id": f"a{i}",
                    "fields": {"source_id": "arxiv-cl", "发布时间": stamp - i, "链接": {"link": f"https://arxiv.org/abs/{i}"}},
                }
                for i in range(config.MAX_ARXIV_ITEMS + 2)
            ],
            {"record_id": "scrape", "fields": {"source_id": "scrape", "发布时间": stamp}},
        ]
        selected = daily.select_candidates(
            records,
            {"official-rss": "P0", "arxiv-cl": "P1"},
            {"official-rss", "arxiv-cl"},
            now=now,
            limit=50,
        )
        self.assertEqual(selected[0]["record_id"], "official")
        self.assertEqual(len([item for item in selected if item["source_id"].startswith("arxiv-")]), config.MAX_ARXIV_ITEMS)
        self.assertNotIn("scrape", [item["record_id"] for item in selected])


class DeliveryTests(unittest.TestCase):
    def sample_brief(self) -> dict:
        return {
            "date": "2026-07-13",
            "title": "AI Signal 每日情报 · 2026-07-13",
            "intro": "今日真实情报。",
            "bullets": [{"text": "一条要点", "refs": [1]}],
            "signals": [
                {
                    "recordId": "rec1",
                    "title": "真实标题",
                    "titleCn": "真实中文标题",
                    "source": "OpenAI",
                    "url": "https://example.com/news",
                    "category": "前沿模型公司",
                    "publishedDate": "2026-07-13",
                    "summary": "真实摘要",
                    "why": "值得关注",
                    "impact": 90,
                    "novelty": 80,
                    "actionability": 70,
                    "urgency": "高",
                    "tags": ["AI"],
                    "imageUrl": "https://example.com/original.jpg",
                }
            ],
            "briefRecordId": "brief1",
            "briefTableId": "table1",
        }

    def test_static_site_contract_and_card_url(self) -> None:
        brief = self.sample_brief()
        with tempfile.TemporaryDirectory() as directory:
            site = publish.build_site([brief], directory)
            latest = json.loads((site / "data" / "brief-latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["signals"][0]["title"], "真实标题")
            self.assertTrue((site / "index.html").exists())
        url = notify.detail_url("https://example.github.io/demo/", brief["date"])
        self.assertEqual(url, "https://example.github.io/demo/?date=2026-07-13")
        card = notify.build_card(brief, url)
        self.assertIn("真实中文标题", json.dumps(card, ensure_ascii=False))
        self.assertIn(url, json.dumps(card, ensure_ascii=False))

    def test_brief_bullet_title_replaces_placeholder(self) -> None:
        title = daily.brief_bullet_title("模型治理从原则走向工程，企业开始部署审计工具。", "要点1")
        self.assertEqual(title, "模型治理从原则走向工程")
        self.assertEqual(daily.brief_bullet_title("正文", "具体结论"), "具体结论")

    def test_content_type_is_inferred_from_source(self) -> None:
        self.assertEqual(
            daily.content_type({"来源": "arXiv cs.CL", "链接": {"link": "https://arxiv.org/abs/1"}}),
            "论文",
        )
        self.assertEqual(
            daily.content_type({"来源类型": "社交媒体", "链接": {"link": "https://x.com/a/status/1"}}),
            "社交媒体帖子",
        )
        self.assertEqual(
            daily.content_type({"链接": {"link": "https://mp.weixin.qq.com/s/demo"}}),
            "公众号",
        )

    @patch("src.notify.feishu.send_interactive_message")
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    @patch("src.notify.feishu.read_all_records_with_ids")
    def test_already_sent_brief_is_not_duplicated(self, read_records, _token, send_message) -> None:
        read_records.return_value = [
            {
                "record_id": "brief1",
                "fields": {"简报ID": "2026-07-13", "发送状态": "已发送", "消息ID": "message1"},
            }
        ]
        result = notify.send(self.sample_brief(), "https://example.com", "ou_test")
        self.assertTrue(result["skipped"])
        send_message.assert_not_called()

    @patch("src.notify.feishu.update_record")
    @patch("src.notify.feishu.send_interactive_message", side_effect=["msg1", "msg2"])
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    @patch("src.notify.feishu.read_all_records_with_ids")
    def test_brief_can_send_to_multiple_recipients(
        self, read_records, _token, send_message, update_record
    ) -> None:
        read_records.return_value = [
            {"record_id": "brief1", "fields": {"简报ID": "2026-07-13", "发送状态": "待发送"}}
        ]
        result = notify.send_many(
            self.sample_brief(), "https://example.com", ["ou_one", "ou_two", "ou_one"]
        )
        self.assertEqual(result["messageIds"], {"ou_one": "msg1", "ou_two": "msg2"})
        self.assertEqual(send_message.call_count, 2)
        update_record.assert_called_once()


if __name__ == "__main__":
    unittest.main()
