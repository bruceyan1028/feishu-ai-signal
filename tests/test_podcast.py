from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src import podcast, podcast_preview, process, sources

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
 xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
 xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <title>Example AI</title>
  <item>
    <title>Episode 1</title>
    <guid>episode-guid-1</guid>
    <link>https://example.com/e1</link>
    <pubDate>Tue, 28 Jul 2026 00:00:00 GMT</pubDate>
    <itunes:duration>1:02:03</itunes:duration>
    <description>Full episode notes</description>
    <enclosure url="https://cdn.example.com/e1.mp3" type="audio/mpeg" length="1234"/>
    <podcast:transcript url="https://example.com/e1.vtt" type="text/vtt" language="en"/>
  </item>
</channel>
</rss>"""


class PodcastTest(unittest.TestCase):
    @patch("src.podcast.report._llm_json")
    def test_official_description_is_structured_without_raw_promo(self, llm):
        llm.return_value = {
            "title_cn": "投资人谈AI泡沫与长期价值",
            "summary_cn": "本期讨论AI投资热度、应用机会和硬件产品边界。",
            "guest_intro_cn": "Will是BAI Capital高级合伙人。",
            "core_points": [
                {"title": "应用机会", "text": "简介预告将讨论Context和交互。"},
                {"title": "硬件边界", "text": "简介提出产品定义仍是稀缺能力。"},
            ],
            "why": "提供投资人与产品视角。",
        }
        result = podcast_preview.analyze_official_description(
            "十字路口 Crossing",
            "公路播客",
            "关注公众号并收听本期节目。",
        )
        self.assertEqual(result["guest_intro_cn"], "Will是BAI Capital高级合伙人。")
        self.assertEqual(len(result["core_points"]), 2)
        prompt = llm.call_args.args[0]
        self.assertIn("删除关注公众号", prompt)
        self.assertIn("不把“将讨论、尝试回答”", prompt)

    def test_duration_and_timestamp(self):
        self.assertEqual(podcast.parse_duration("1:02:03"), 3723)
        self.assertEqual(podcast.parse_duration("12:05"), 725)
        self.assertEqual(podcast.parse_duration("90"), 90)
        self.assertEqual(podcast.format_timestamp(3723), "01:02:03")

    def test_parse_vtt_preserves_timestamps(self):
        text = podcast.parse_transcript(
            "WEBVTT\n\n00:00:03.000 --> 00:00:08.000\nHello AI.\n\n"
            "00:01:10.000 --> 00:01:15.000\nSecond point.",
            "text/vtt",
        )
        self.assertIn("[00:00:03] Hello AI.", text)
        self.assertIn("[00:01:10] Second point.", text)

    def test_parse_podcast_index_json(self):
        text = podcast.parse_transcript(
            json.dumps(
                {
                    "segments": [
                        {"startTime": 12500, "body": "First"},
                        {"startTime": 70000, "body": "Second"},
                    ]
                }
            ),
            "application/json",
        )
        self.assertEqual(text, "[00:00:12] First\n[00:01:10] Second")

    @patch("src.podcast.requests.get")
    def test_fetch_source_reads_enclosure_namespace_and_guid(self, get):
        response = Mock(content=RSS)
        response.raise_for_status.return_value = None
        get.return_value = response
        feed = {
            "id": "example-podcast",
            "name": "Example AI",
            "url": "https://example.com/feed.xml",
            "fetch_method": "Podcast",
            "source_type": "播客",
            "extra_config": {},
        }
        items = podcast.fetch_source(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["podcast"]["guid"], "episode-guid-1")
        self.assertEqual(items[0]["podcast"]["duration_sec"], 3723)
        self.assertEqual(items[0]["podcast"]["transcripts"][0]["url"], "https://example.com/e1.vtt")
        self.assertEqual(items[0]["feed"]["podcast_guid"], "episode-guid-1")
        self.assertEqual(
            process.build_dedup_key(
                items[0]["url"], items[0]["title"], items[0]["feed"]
            ),
            "podcast:episode-guid-1",
        )

    @patch("src.podcast.requests.get")
    def test_fetch_source_uses_fallback_feed(self, get):
        response = Mock(content=RSS, url="https://fallback.example/feed")
        response.raise_for_status.return_value = None
        get.side_effect = [requests.Timeout("primary timeout"), response]
        items = podcast.fetch_source(
            {
                "id": "example-podcast",
                "name": "Example AI",
                "url": "https://primary.example/feed",
                "fetch_method": "Podcast",
                "extra_config": {
                    "max_items": 1,
                    "fallback_urls": ["https://fallback.example/feed"],
                },
            }
        )
        self.assertEqual(items[0]["podcast"]["guid"], "episode-guid-1")
        self.assertEqual(get.call_count, 2)

    def test_map_sources_allows_experimental_only_for_diag(self):
        records = [
            {
                "record_id": "rec1",
                "fields": {
                    "source_id": "pod-a",
                    "name": "Pod A",
                    "endpoint": "https://example.com/feed",
                    "fetch_method": "Podcast",
                    "status": "experimental",
                    "来源类型": "播客",
                },
            }
        ]
        self.assertEqual(sources.map_podcast_sources(records), [])
        feeds = sources.map_podcast_sources(records, allow_experimental=True)
        self.assertEqual(feeds[0]["dedup_key"], "podcast_guid")
        self.assertEqual(feeds[0]["record_id"], "rec1")

    @patch("src.podcast.summarize_transcript")
    @patch("src.podcast.transcribe_audio")
    @patch("src.podcast.fetch_public_transcript")
    def test_enrich_prefers_public_transcript(self, public, transcribe, summarize):
        public.return_value = ("[00:00:00] " + "word " * 100, "rss_transcript")
        summarize.return_value = (
            "[00:00:00] 证据",
            {
                "title_cn": "标题",
                "summary_cn": "完整摘要",
                "deep_analysis_cn": "深度分析",
                "why": "重要",
                "impact": 80,
                "novelty": 70,
                "actionability": 60,
                "urgency": "中",
                "topics": ["AI"],
            },
        )
        item = {
            "title": "Title",
            "feed": {"name": "Show", "fetch_method": "Podcast", "extra_config": {}},
            "podcast": {
                "duration_sec": 3600,
                "audio_url": "https://example.com/a.mp3",
                "transcripts": [{"url": "https://example.com/a.vtt"}],
            },
        }
        source = podcast.enrich_podcast_item(item)
        self.assertEqual(source, "rss_transcript")
        transcribe.assert_not_called()
        self.assertEqual(item["raw_content"], "[00:00:00] 证据")
        self.assertEqual(item["podcast_analysis"]["summary_cn"], "完整摘要")

    @patch("src.podcast.summarize_transcript")
    @patch("src.podcast.transcribe_audio")
    @patch("src.podcast.fetch_page_transcript", return_value=("", ""))
    @patch("src.podcast.fetch_public_transcript", return_value=("", ""))
    def test_enrich_falls_back_to_hosted_asr(
        self, _public, _page, transcribe, summarize
    ):
        transcribe.return_value = "[00:00:00] " + "word " * 100
        summarize.return_value = (
            "证据",
            {
                "title_cn": "标题",
                "summary_cn": "摘要",
                "deep_analysis_cn": "分析",
                "why": "重要",
                "impact": 60,
                "novelty": 60,
                "actionability": 60,
                "urgency": "低",
                "topics": ["AI"],
            },
        )
        item = {
            "title": "Title",
            "feed": {"name": "Show", "fetch_method": "Podcast", "extra_config": {}},
            "podcast": {"duration_sec": 0, "audio_url": "https://example.com/a.mp3"},
        }
        with patch("src.podcast.config.ASR_API_KEY", "test"):
            self.assertEqual(podcast.enrich_podcast_item(item), "hosted_asr")
        transcribe.assert_called_once()

    @patch("src.podcast.summarize_official_description")
    @patch("src.podcast.transcribe_audio")
    @patch("src.podcast.fetch_page_transcript", return_value=("", ""))
    @patch("src.podcast.fetch_public_transcript", return_value=("", ""))
    def test_enrich_uses_structured_description_without_asr(
        self, _public, _page, transcribe, summarize
    ):
        summarize.return_value = {
            "title_cn": "整理后标题",
            "summary_cn": "整理后的简介摘要",
            "deep_analysis_cn": "【简介要点1】\n具体内容",
            "why": "选题有价值",
            "impact": 60,
            "novelty": 50,
            "actionability": 40,
            "urgency": "中",
            "topics": ["AI", "产品"],
        }
        item = {
            "title": "Title",
            "body": "This official description contains enough concrete episode context. " * 3,
            "feed": {"name": "Show", "fetch_method": "Podcast", "extra_config": {}},
            "podcast": {"duration_sec": 1800, "audio_url": "https://example.com/a.mp3"},
            "metrics": {},
        }
        with patch("src.podcast.config.ASR_API_KEY", ""):
            self.assertEqual(
                podcast.enrich_podcast_item(item), "official_description"
            )
        transcribe.assert_not_called()
        self.assertEqual(
            item["podcast_metrics_json"]["transcript_source"],
            "official_description",
        )
        self.assertEqual(item["podcast_analysis"]["title_cn"], "整理后标题")

    @patch("src.podcast.enrich_podcast_item")
    def test_enrichment_failure_isolated(self, enrich):
        enrich.side_effect = [RuntimeError("bad"), "rss_transcript"]
        failed = {"url": "bad", "feed": {"fetch_method": "Podcast"}}
        good = {"url": "good", "feed": {"fetch_method": "Podcast"}}
        other = {"url": "web", "feed": {"fetch_method": "RSS"}}
        kept, stats = podcast.enrich_podcast_items([failed, good, other])
        self.assertEqual(kept, [good, other])
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["kept"], 1)

    @patch("src.podcast.report._llm_json")
    def test_hierarchical_summary_maps_then_reduces(self, llm):
        llm.side_effect = [
            {"notes_cn": "[00:00:01] 第一段证据"},
            {"notes_cn": "[00:10:00] 第二段证据"},
            {
                "title_cn": "中文标题",
                "summary_cn": "覆盖全期的中文摘要",
                "deep_analysis_cn": "【核心议题】\n分析",
                "evidence_cn": "[00:00:01] 第一段证据\n[00:10:00] 第二段证据",
                "why": "包含一手判断",
                "impact": 80,
                "novelty": 70,
                "actionability": 60,
                "urgency": "中",
                "topics": ["AI", "产品"],
            },
        ]
        transcript = "[00:00:01] " + "a" * 1200 + "\n[00:10:00] " + "b" * 1200
        with patch("src.podcast.config.PODCAST_TRANSCRIPT_CHUNK_CHARS", 2000), patch(
            "src.podcast.config.LLM_API_KEY", "test"
        ):
            evidence, analysis = podcast.summarize_transcript(
                "Episode", "Show", transcript
            )
        self.assertEqual(llm.call_count, 3)
        self.assertIn("[00:10:00]", evidence)
        self.assertEqual(analysis["summary_cn"], "覆盖全期的中文摘要")

    def test_podcast_production_filter_requires_ai_relevance_near_intro(self):
        feed = {
            "id": "podcast-show",
            "name": "Show",
            "fetch_method": "Podcast",
            "source_type": "播客",
            "lookback_hours": 168,
            "keyword_regex": r"(ai|llm|agent|model)",
            "keyword_min_hits": 1,
            "min_content_chars": 1,
        }
        common = {
            "url": "https://example.com/e1",
            "published_raw": "2026-07-28T00:00:00Z",
            "feed": feed,
        }
        items = [
            {
                **common,
                "title": "A general relativity lecture",
                "body": "physics " * 300 + " AI model agent",
            },
            {
                **common,
                "url": "https://example.com/e2",
                "title": "How AI changes mathematical discovery",
                "body": "A discussion with a mathematician.",
            },
        ]
        with patch("src.process.now_ms", return_value=1785225600000):
            cleaned = process.process_and_clean(items)
        self.assertEqual([item["title"] for item in cleaned], [items[1]["title"]])

    def test_format_for_feishu_writes_completed_podcast_analysis(self):
        fields = process.format_for_feishu(
            {
                "title": "Episode",
                "url": "https://example.com/e1",
                "source": "Show",
                "source_id": "podcast-show",
                "source_type": "播客",
                "fetch_method": "Podcast",
                "category": "其他",
                "tier": "L4 补充源",
                "published_ms": 1,
                "collected_ms": 2,
                "raw_content": "[00:00:01] 证据",
                "media_assets": {
                    "images": [],
                    "videos": [],
                    "audio": {"url": "https://example.com/e1.mp3"},
                },
                "topics": [],
                "duplicate_key": "podcast:g1",
                "quality_score": 70,
                "podcast_metrics_json": {"transcript_source": "hosted_asr"},
                "podcast_analysis": {
                    "title_cn": "中文标题",
                    "summary_cn": "完整摘要",
                    "deep_analysis_cn": "深度分析",
                    "why": "重要",
                    "impact": 80,
                    "novelty": 70,
                    "actionability": 60,
                    "urgency": "中",
                    "topics": ["AI"],
                },
            }
        )
        self.assertEqual(fields["状态"], "已分析")
        self.assertEqual(fields["中文摘要"], "完整摘要")
        self.assertEqual(fields["路由来源"], "Podcast")
        self.assertIn("hosted_asr", fields["播客指标"])
        self.assertIn("e1.mp3", fields["媒体资源"])

    @patch("src.podcast.requests.post")
    def test_asr_segments_apply_chunk_offset(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "segments": [{"start": 5.5, "text": "A point"}],
            "text": "A point",
        }
        post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chunk.mp3"
            path.write_bytes(b"audio")
            with patch("src.podcast.config.ASR_API_KEY", "test"):
                text = podcast._transcribe_chunk(path, 1200)
        self.assertEqual(text, "[00:20:05] A point")


if __name__ == "__main__":
    unittest.main()
