from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    # 社区热度走 HF 实时接口，点赞数随时会变；固定住才能稳定比对两次抽取结果
    @patch("src.scrape._hf_paper_community", return_value=("", {}))
    def test_hf_pwc_extracts_only_paper_urls(self, _community: MagicMock) -> None:
        now = datetime.now(timezone.utc)
        recent = now.strftime("%Y-%m-%dT00:00:00.000Z")
        # arXiv ID 的 YYMM 要跟发布日同月，否则会被首发时间修正判成「旧论文重新上榜」
        pid_recent = now.strftime("%y%m")
        mid_aged = now - timedelta(days=14)
        props = {
            "dailyPapers": [
                {
                    "title": "Paper Alpha",
                    "paper": {
                        "id": f"{pid_recent}.11111",
                        "title": "Paper Alpha",
                        "publishedAt": recent,
                        "upvotes": 40,
                    },
                },
                {
                    "title": "Paper Beta",
                    "paper": {
                        "id": f"{pid_recent}.11886",
                        "title": "Paper Beta",
                        "publishedAt": recent,
                        "upvotes": 50,
                    },
                },
                {
                    "title": "Mid Hot",
                    "paper": {
                        "id": f"{mid_aged.strftime('%y%m')}.30000",
                        "title": "Mid Hot",
                        "publishedAt": mid_aged.strftime("%Y-%m-%dT00:00:00.000Z"),
                        "upvotes": 120,
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
        <a href="https://huggingface.co/papers/{pid_recent}.11111">dup</a>
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
        # 2412.20138 再高赞也超出高热年龄上限，不再算「近期发布」
        self.assertEqual(
            urls,
            [
                f"https://huggingface.co/papers/{pid_recent}.11111",
                f"https://huggingface.co/papers/{pid_recent}.11886",
                f"https://huggingface.co/papers/{mid_aged.strftime('%y%m')}.30000",
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
        now = datetime.now(timezone.utc)
        mid_aged = now - timedelta(days=14)
        payload = [
            {
                "paper_id": "1",
                "arxiv_id": f"{now.strftime('%y%m')}.04439",
                "title": "ResearchStudio-Idea",
                "date_published": now.strftime("%Y-%m-%d"),
                "paper_age_days": 2,
                "trending": {"stars_gained_24h": 10},
            },
            {
                "paper_id": "2",
                "arxiv_id": f"{mid_aged.strftime('%y%m')}.23904",
                "title": "Mid Hot SkillOpt",
                "date_published": mid_aged.strftime("%Y-%m-%d"),
                "paper_age_days": 14,
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
            {
                "paper_id": "4",
                "arxiv_id": "2403.09999",
                "title": "Old Hot Resurfaced",
                # 接口把「重新上榜日」当发布日报上来，年龄也跟着算成 2 天
                "date_published": now.strftime("%Y-%m-%d"),
                "paper_age_days": 2,
                "trending": {"stars_gained_24h": 131},
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
        # 2403.09999 的 arXiv ID 说明它 2024 年就发过，重新上榜不算新发布
        self.assertEqual(
            [x["url"] for x in links],
            [
                f"https://huggingface.co/papers/{now.strftime('%y%m')}.04439",
                f"https://huggingface.co/papers/{mid_aged.strftime('%y%m')}.23904",
            ],
        )
        self.assertEqual(links[0]["title"], "ResearchStudio-Idea")
        self.assertFalse(links[0]["heat_keep"])
        self.assertTrue(links[1]["heat_keep"])

    def test_resurfaced_paper_falls_back_to_arxiv_first_publication(self) -> None:
        listed = "2026-07-23T00:00:00.000Z"
        # ID 月份早于上榜日：以 ID 月末为首发时间上界
        self.assertEqual(
            scrape._paper_first_published_raw("2605.09635", listed),
            "2026-05-31T23:59:59Z",
        )
        # 同月发布不改动，保留榜单给的精确日期
        self.assertEqual(scrape._paper_first_published_raw("2607.23588", listed), listed)
        # 无日期仍保持无日期，交给上游按「缺发布时间」丢弃
        self.assertEqual(scrape._paper_first_published_raw("2605.09635", ""), "")


    def _github_feed(self, **extra: object) -> dict[str, object]:
        return {
            "id": "github-trending",
            "url": "https://github.com/trending",
            "max_articles": 5,
            "github_config": {
                "min_stars": 2000,
                "release_recent_days": 30,
                "min_new_repo_stars": 500,
                **extra,
            },
        }

    @patch("src.scrape._github_issue_feedback", return_value="")
    @patch("src.scrape._github_readme_images", return_value=[])
    @patch("src.scrape._github_readme_raw", return_value="# Repo\n\n项目介绍。")
    def test_github_hotlist_needs_a_real_release_event(
        self, _readme: MagicMock, _images: MagicMock, _issues: MagicMock
    ) -> None:
        now = datetime.now(timezone.utc)
        iso = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT00:00:00Z")
        repos = [
            {
                # 2018 年的老项目，昨天有人提交代码，但最近一次发版在半年前
                "full_name": "old/transformers",
                "name": "transformers",
                "description": "LLM inference toolkit",
                "html_url": "https://github.com/old/transformers",
                "stargazers_count": 160000,
                "forks_count": 30000,
                "topics": ["llm"],
                "language": "Python",
                "created_at": "2018-10-29T00:00:00Z",
                "pushed_at": iso(1),
            },
            {
                # 同样是老项目，但三天前发了新版本
                "full_name": "live/vllm",
                "name": "vllm",
                "description": "LLM serving engine",
                "html_url": "https://github.com/live/vllm",
                "stargazers_count": 80000,
                "forks_count": 9000,
                "topics": ["llm", "inference"],
                "language": "Python",
                "created_at": "2023-02-09T00:00:00Z",
                "pushed_at": iso(1),
            },
        ]
        releases = {
            "old/transformers": {
                "tag_name": "v5.0.0",
                "published_at": iso(180),
                "html_url": "https://github.com/old/transformers/releases/tag/v5.0.0",
                "body": "",
            },
            "live/vllm": {
                "tag_name": "v0.26.0",
                "published_at": iso(3),
                "html_url": "https://github.com/live/vllm/releases/tag/v0.26.0",
                "body": "## What's Changed\n新增分布式推理调度。",
            },
        }
        with patch("src.scrape._github_search", return_value=repos), patch(
            "src.scrape._github_latest_release", side_effect=lambda fn: releases[fn]
        ):
            items = scrape._fetch_github_items(self._github_feed())

        self.assertEqual([item["title"] for item in items], ["live/vllm v0.26.0"])
        item = items[0]
        # 时间取发版日，不是 pushed_at
        self.assertEqual(item["published_raw"], iso(3))
        self.assertEqual(item["url"], "https://github.com/live/vllm/releases/tag/v0.26.0")
        self.assertIn("新版本 v0.26.0", item["body"])
        self.assertIn("新增分布式推理调度", item["body"])

    @patch("src.scrape._github_issue_feedback", return_value="")
    @patch("src.scrape._github_readme_images", return_value=[])
    @patch("src.scrape._github_readme_raw", return_value="# Repo\n\n项目介绍。")
    def test_github_new_repo_enters_below_sedimentation_bar(
        self, _readme: MagicMock, _images: MagicMock, _issues: MagicMock
    ) -> None:
        now = datetime.now(timezone.utc)
        iso = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT00:00:00Z")
        repos = [
            {
                "full_name": "new/agent-kit",
                "name": "agent-kit",
                "description": "brand new agent framework",
                "html_url": "https://github.com/new/agent-kit",
                "stargazers_count": 900,  # 低于沉淀线 2000，但过了新项目线 500
                "forks_count": 20,
                "topics": ["agent"],
                "language": "Python",
                "created_at": iso(6),
                "pushed_at": iso(1),
            },
        ]
        with patch("src.scrape._github_search", return_value=repos), patch(
            "src.scrape._github_latest_release", return_value={}
        ) as release:
            items = scrape._fetch_github_items(self._github_feed())

        self.assertEqual([item["title"] for item in items], ["new/agent-kit"])
        self.assertEqual(items[0]["published_raw"], iso(6))
        self.assertIn("新项目", items[0]["body"])
        # 新建仓库本身就是发布事件，不必再查 release
        release.assert_not_called()

    @patch("src.scrape._github_issue_feedback", return_value="")
    @patch("src.scrape._github_readme_images", return_value=[])
    @patch("src.scrape._github_readme_raw", return_value="# Repo")
    def test_github_skips_repo_when_release_info_unavailable(
        self, _readme: MagicMock, _images: MagicMock, _issues: MagicMock
    ) -> None:
        repos = [
            {
                "full_name": "live/vllm",
                "name": "vllm",
                "description": "LLM serving engine",
                "html_url": "https://github.com/live/vllm",
                "stargazers_count": 80000,
                "forks_count": 9000,
                "topics": ["llm"],
                "language": "Python",
                "created_at": "2023-02-09T00:00:00Z",
                "pushed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z"),
            },
        ]
        # None = 查不到发布信息（限流/网络），不能当成「没发过新版」
        with patch("src.scrape._github_search", return_value=repos), patch(
            "src.scrape._github_latest_release", return_value=None
        ):
            self.assertEqual(scrape._fetch_github_items(self._github_feed()), [])

    def test_github_latest_release_skips_prerelease(self) -> None:
        atom = """
        <feed>
          <entry>
            <id>tag:github.com,2008:Repository/1/v0.26.1rc0</id>
            <updated>2026-07-29T00:00:00Z</updated>
            <link rel="alternate" href="https://github.com/o/r/releases/tag/v0.26.1rc0"/>
            <title>v0.26.1rc0</title>
            <content type="html">&lt;p&gt;预发布&lt;/p&gt;</content>
          </entry>
          <entry>
            <id>tag:github.com,2008:Repository/1/v0.26.0</id>
            <updated>2026-07-27T00:00:00Z</updated>
            <link rel="alternate" href="https://github.com/o/r/releases/tag/v0.26.0"/>
            <title>v0.26.0</title>
            <content type="html">&lt;p&gt;新增分布式推理调度&lt;/p&gt;</content>
          </entry>
        </feed>
        """
        scrape._GH_RELEASE_CACHE.pop("o/r", None)
        with patch("src.scrape._github_releases_atom", return_value=atom):
            release = scrape._github_latest_release("o/r")
        self.assertEqual(release["tag_name"], "v0.26.0")
        self.assertEqual(release["published_at"], "2026-07-27T00:00:00Z")
        self.assertIn("新增分布式推理调度", scrape._github_release_notes(release))

        scrape._GH_RELEASE_CACHE.pop("o/none", None)
        with patch("src.scrape._github_releases_atom", return_value=""):
            # 拉不到 feed 与「没有正式发布」要区分开
            self.assertIsNone(scrape._github_latest_release("o/none"))

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
    def test_edge_topic_is_added_deterministically(self) -> None:
        fields = {
            "标题": "OPPO 开放首个端侧 Multi-Agent 系统内测",
            "原文": "模型在手机 NPU 上本地运行，不依赖云端推理。",
        }
        self.assertEqual(
            daily.normalize_topics(fields, ["AI", "Agent"]),
            ["AI", "Agent", "端侧"],
        )

    def test_edge_topic_does_not_match_npu_inside_ordinary_words(self) -> None:
        fields = {
            "标题": "Optimizing attention with input perturbations",
            "原文": "This paper studies input representations for server-side training.",
        }
        self.assertEqual(daily.normalize_topics(fields, ["AI"]), ["AI"])

    def test_edge_topic_ignores_generic_local_deployment(self) -> None:
        fields = {
            "标题": "Open WebUI：面向本地部署的可扩展 AI 交互平台",
            "中文摘要": "可部署在服务器上并接入多种远程模型 API。",
        }
        self.assertEqual(daily.normalize_topics(fields, ["开源", "LLM"]), ["开源", "LLM"])

    def test_edge_topic_survives_four_topic_cap(self) -> None:
        fields = {"标题": "Jetson 边缘推理平台更新"}
        self.assertEqual(
            daily.normalize_topics(fields, ["AI", "硬件", "产品", "多模态"]),
            ["AI", "硬件", "产品", "端侧"],
        )

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
        # arXiv 条目均为论文，受论文上限 DAILY_MAX_PAPERS 约束（比 MAX_ARXIV_ITEMS 更紧）
        arxiv_selected = [item for item in selected if item["source_id"].startswith("arxiv-")]
        self.assertEqual(len(arxiv_selected), config.DAILY_MAX_PAPERS)
        self.assertLessEqual(len(arxiv_selected), config.MAX_ARXIV_ITEMS)
        self.assertNotIn("scrape", [item["record_id"] for item in selected])

    def test_github_hotlist_is_capped(self) -> None:
        now = datetime.now(timezone.utc)
        stamp = int(now.timestamp() * 1000)
        records = [
            {
                "record_id": f"gh{i}",
                "fields": {
                    "source_id": f"gh-src-{i}",
                    "来源类型": "Github热榜",
                    "发布时间": stamp - i,
                    "链接": {"link": f"https://github.com/o/r{i}"},
                },
            }
            for i in range(config.DAILY_MAX_GITHUB + 5)
        ]
        selected = daily.select_candidates(
            records,
            {f"gh-src-{i}": "P0" for i in range(len(records))},
            {f"gh-src-{i}" for i in range(len(records))},
            now=now,
            limit=50,
        )
        self.assertEqual(len(selected), config.DAILY_MAX_GITHUB)

    def test_single_source_cannot_flood_candidates(self) -> None:
        now = datetime.now(timezone.utc)
        stamp = int(now.timestamp() * 1000)
        records = [
            {
                "record_id": f"n{i}",
                "fields": {"source_id": "one-source", "发布时间": stamp - i},
            }
            for i in range(config.DAILY_MAX_PER_SOURCE + 6)
        ]
        selected = daily.select_candidates(
            records, {"one-source": "P0"}, {"one-source"}, now=now, limit=50
        )
        self.assertEqual(len(selected), config.DAILY_MAX_PER_SOURCE)

    def test_non_p0_sources_keep_reserved_slots(self) -> None:
        """P0 源足够多时也要给 P1/P2 留名额，否则中文媒体永远进不了简报。"""
        now = datetime.now(timezone.utc)
        stamp = int(now.timestamp() * 1000)
        limit = 20
        p0_ids = [f"p0-{i}" for i in range(12)]
        p1_ids = [f"p1-{i}" for i in range(12)]
        records = [
            {"record_id": f"{sid}-{j}", "fields": {"source_id": sid, "发布时间": stamp - j}}
            for sid in p0_ids + p1_ids
            for j in range(3)
        ]
        priorities = {sid: "P0" for sid in p0_ids}
        priorities.update({sid: "P1" for sid in p1_ids})
        selected = daily.select_candidates(
            records, priorities, set(p0_ids + p1_ids), now=now, limit=limit
        )
        non_p0 = [item for item in selected if item["priority"] != "P0"]
        self.assertEqual(len(selected), limit)
        self.assertGreaterEqual(len(non_p0), config.DAILY_MIN_NON_P0)

    def test_plain_news_gets_explicit_article_type(self) -> None:
        self.assertEqual(
            daily.content_type({"来源": "TechCrunch AI", "链接": {"link": "https://techcrunch.com/a"}}),
            "文章",
        )

    def test_output_keeps_minimum_video_slot(self) -> None:
        ranked = [
            {"recordId": f"article-{index}", "contentType": ""}
            for index in range(5)
        ]
        ranked.append({"recordId": "video-1", "contentType": "视频"})

        selected = daily.balance_output_signals(ranked, 3)

        self.assertEqual(len(selected), 3)
        self.assertIn("video-1", [item["recordId"] for item in selected])

    def test_candidate_does_not_use_collection_time_as_publish_time(self) -> None:
        now = datetime.now(timezone.utc)
        stamp = int(now.timestamp() * 1000)
        selected = daily.select_candidates(
            [
                {
                    "record_id": "missing-published",
                    "fields": {
                        "source_id": "official-rss",
                        "采集时间": stamp,
                    },
                }
            ],
            {"official-rss": "P0"},
            {"official-rss"},
            now=now,
        )
        self.assertEqual(selected, [])

    def test_candidate_respects_source_specific_24h_window(self) -> None:
        now = datetime.now(timezone.utc)

        def record(record_id: str, hours_old: int) -> dict:
            return {
                "record_id": record_id,
                "fields": {
                    "source_id": "meta-ai-blog",
                    "发布时间": int((now - timedelta(hours=hours_old)).timestamp() * 1000),
                },
            }

        selected = daily.select_candidates(
            [record("fresh", 23), record("stale", 25)],
            {"meta-ai-blog": "P0"},
            {"meta-ai-blog"},
            {"meta-ai-blog": 24},
            now=now,
        )
        self.assertEqual([item["record_id"] for item in selected], ["fresh"])

    def test_candidate_keeps_global_seven_day_hard_maximum(self) -> None:
        now = datetime.now(timezone.utc)
        selected = daily.select_candidates(
            [
                {
                    "record_id": "old-trending",
                    "fields": {
                        "source_id": "github-trending",
                        "发布时间": int((now - timedelta(days=8)).timestamp() * 1000),
                    },
                }
            ],
            {"github-trending": "P0"},
            {"github-trending"},
            {"github-trending": 30 * 24},
            now=now,
        )
        self.assertEqual(selected, [])


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

    def multi_category_brief(self) -> dict:
        brief = self.sample_brief()
        brief["signals"].append(
            {
                "recordId": "rec2",
                "title": "Second",
                "titleCn": "第二条中文标题",
                "source": "量子位",
                "url": "https://example.com/second",
                "category": "中文科技媒体",
                "summary": "第二条摘要，" * 20,
                "impact": 80,
                "contentType": "公众号",
            }
        )
        return brief

    def test_daily_card_is_text_only_directory(self) -> None:
        brief = self.multi_category_brief()
        card = notify.build_card(brief, "https://example.com/brief")
        dumped = json.dumps(card, ensure_ascii=False)
        self.assertFalse([item for item in card["elements"] if item.get("tag") == "img"])
        # 导语、摘要与内部打分只属于网页，卡片里不该出现
        self.assertNotIn("今日真实情报。", dumped)
        self.assertNotIn("第二条摘要，", dumped)
        self.assertNotIn("影响分", dumped)

    def test_daily_card_title_and_groups_are_centered_color_bands(self) -> None:
        brief = self.multi_category_brief()
        card = notify.build_card(brief, "https://example.com/brief")
        # 标题移进正文才能居中，因此卡片不再带 header
        self.assertNotIn("header", card)
        bands = [item for item in card["elements"] if item.get("tag") == "column_set"]
        self.assertEqual(
            [band["background_style"] for band in bands],
            ["red-100", "blue-100", "wathet-100"],
        )
        headings = [band["columns"][0]["elements"][0] for band in bands]
        self.assertTrue(all(item["text_align"] == "center" for item in headings))
        self.assertEqual(
            [item["content"] for item in headings],
            [
                "<font color='red'>**AI Signal 每日情报 · 2026-07-13**</font>",
                "<font color='blue'>**🚀 前沿模型公司**</font>",
                "<font color='wathet'>**📰 中文科技媒体**</font>",
            ],
        )

    def test_daily_card_links_title_and_greys_out_meta(self) -> None:
        brief = self.multi_category_brief()
        card = notify.build_card(brief, "https://example.com/brief")
        rows = [
            item
            for item in card["elements"]
            if item.get("tag") == "markdown" and item.get("text_align") != "center"
        ]
        self.assertEqual(
            [item["content"] for item in rows],
            [
                "**[真实中文标题](https://example.com/news)**",
                "<font color='grey'>OpenAI</font>",
                "**[第二条中文标题](https://example.com/second)**",
                "<font color='grey'>量子位 · 公众号</font>",
            ],
        )
        # 来源行必须降到辅助字号，否则和标题挤在一起
        self.assertEqual([item.get("text_size") for item in rows[1::2]], ["notation"] * 2)

    def test_daily_card_caps_groups_and_items(self) -> None:
        brief = self.multi_category_brief()
        groups = notify.group_signals(brief["signals"])
        self.assertEqual([name for name, _ in groups], ["前沿模型公司", "中文科技媒体"])
        self.assertTrue(all(len(items) <= notify.MAX_ITEMS_PER_GROUP for _, items in groups))
        crowded = [
            dict(signal, recordId=f"rec{index}", category=f"板块{index // 4}")
            for index in range(40)
            for signal in [brief["signals"][0]]
        ]
        capped = notify.group_signals(crowded)
        self.assertLessEqual(len(capped), notify.MAX_GROUPS)
        self.assertLessEqual(sum(len(items) for _, items in capped), notify.MAX_CARD_ITEMS)

    def test_youtube_preview_tries_in_page_player_with_fallback_link(self) -> None:
        template = Path("index.html").read_text(encoding="utf-8")
        self.assertIn("App.playVideo(this)", template)
        self.assertIn("data-embed=", template)
        self.assertIn("youtube-nocookie.com/embed/", template)
        self.assertIn("在 YouTube 打开 ↗", template)
        self.assertNotIn("ytPosterLoaded", template)

    def test_historical_brief_drops_meta_signal_outside_24h_window(self) -> None:
        old_meta = {
            "sourceId": "meta-ai-blog",
            "publishedAtMs": int(
                datetime(2026, 4, 8, tzinfo=timezone.utc).timestamp() * 1000
            ),
        }
        fresh_meta = {
            "sourceId": "meta-ai-blog",
            "publishedAtMs": int(
                datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp() * 1000
            ),
        }
        windows = {"meta-ai-blog": 24}

        self.assertFalse(
            publish._within_source_window(old_meta, "2026-07-27", windows)
        )
        self.assertTrue(
            publish._within_source_window(fresh_meta, "2026-07-27", windows)
        )

    def test_configured_source_with_unknown_date_is_hidden(self) -> None:
        self.assertFalse(
            publish._within_source_window(
                {"sourceId": "meta-ai-blog", "publishedAtMs": 0},
                "2026-07-27",
                {"meta-ai-blog": 24},
            )
        )

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

    @patch("src.notify.feishu.update_record")
    @patch(
        "src.notify.feishu.send_interactive_message",
        side_effect=["msg1", "msg2", "report-msg"],
    )
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    @patch("src.notify.feishu.read_all_records_with_ids")
    def test_daily_delivery_report_uses_names_without_open_ids(
        self, read_records, _token, send_message, _update_record
    ) -> None:
        read_records.return_value = [
            {"record_id": "brief1", "fields": {"简报ID": "2026-07-13", "发送状态": "待发送"}}
        ]
        with (
            patch.object(
                config,
                "FEISHU_RECIPIENT_NAME_BY_OPEN_ID",
                {"ou_one": "yanyufeng", "ou_two": "guojiexin"},
            ),
            patch.object(config, "FEISHU_DELIVERY_REPORT_OPEN_ID", "ou_one"),
        ):
            result = notify.send_many(
                self.sample_brief(), "https://example.com", ["ou_one", "ou_two"]
            )
        self.assertEqual(
            result["recipientStatuses"],
            {"yanyufeng": "success", "guojiexin": "success"},
        )
        self.assertEqual(result["deliveryReportMessageId"], "report-msg")
        report_card = send_message.call_args_list[2].args[2]
        report_text = report_card["elements"][0]["text"]["content"]
        self.assertIn("yanyufeng：发送成功", report_text)
        self.assertIn("guojiexin：发送成功", report_text)
        self.assertNotIn("ou_one", json.dumps(report_card, ensure_ascii=False))

    @patch("src.notify.feishu.update_record")
    @patch(
        "src.notify.feishu.send_interactive_message",
        side_effect=[RuntimeError("open_id cross app"), "msg2", "msg3"],
    )
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    @patch("src.notify.feishu.read_all_records_with_ids")
    def test_one_bad_recipient_does_not_block_the_others(
        self, read_records, _token, send_message, update_record
    ) -> None:
        read_records.return_value = [
            {"record_id": "brief1", "fields": {"简报ID": "2026-07-13", "发送状态": "待发送"}}
        ]
        result = notify.send_many(
            self.sample_brief(), "https://example.com", ["ou_bad", "ou_two", "ou_three"]
        )
        self.assertEqual(result["messageIds"], {"ou_two": "msg2", "ou_three": "msg3"})
        self.assertEqual(list(result["failed"]), ["ou_bad"])
        # 有人收到就算已发送，回写的消息ID只记成功的那些
        self.assertEqual(update_record.call_args[0][3]["发送状态"], "已发送")
        self.assertNotIn("ou_bad", update_record.call_args[0][3]["消息ID"])

    @patch("src.notify.feishu.update_record")
    @patch("src.notify.feishu.send_interactive_message", side_effect=RuntimeError("boom"))
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    @patch("src.notify.feishu.read_all_records_with_ids")
    def test_all_recipients_failing_marks_brief_failed(
        self, read_records, _token, _send_message, update_record
    ) -> None:
        read_records.return_value = [
            {"record_id": "brief1", "fields": {"简报ID": "2026-07-13", "发送状态": "待发送"}}
        ]
        with self.assertRaises(RuntimeError):
            notify.send_many(self.sample_brief(), "https://example.com", ["ou_a", "ou_b"])
        self.assertEqual(update_record.call_args[0][3]["发送状态"], "失败")


if __name__ == "__main__":
    unittest.main()
