"""同事件聚合：跨语言匹配与择优优先级。"""
from __future__ import annotations

import unittest

from src import cluster


class CrossLanguageEventTest(unittest.TestCase):
    def test_english_official_matches_chinese_paraphrase(self):
        self.assertTrue(
            cluster.same_event("Introducing Claude Opus 5", "Claude Opus 5来了，Fable 5性能、一半价格")
        )

    def test_matches_on_product_and_version(self):
        self.assertTrue(
            cluster.same_event("Introducing Gemini 3.6 Flash", "谷歌发布 Gemini 3.6 Flash，推理提速")
        )

    def test_single_shared_brand_is_not_enough(self):
        self.assertFalse(cluster.same_event("Introducing Claude Code 2", "Claude Opus 5来了"))

    def test_chinese_paraphrases_of_one_event_merge(self):
        self.assertTrue(
            cluster.same_event(
                "Claude Opus 5来了，Fable 5性能、一半价格",
                "半价干翻Fable 5？Opus 5实测炸场，网友：差点从椅子上摔下来",
            )
        )

    def test_same_platform_different_launches_stay_apart(self):
        # 共享 vera/rubin 但没有共同版本号，是同一平台的不同发布
        self.assertFalse(
            cluster.same_event(
                "英伟达 Vera Rubin NVL72 加速量产，主打更高能效与更低 Token 成本",
                "面向Vera Rubin平台的NVIDIA Spectrum-6进入超大规模AI工厂",
            )
        )

    def test_same_conference_different_companies_stay_apart(self):
        # 年份不能当版本号，否则一场大会的几十篇稿子会被并成一条
        self.assertFalse(
            cluster.same_event(
                "WAIC 2026现场直击：海康观澜大模型体系亮相 加速AI落地物理世界",
                "摩尔线程为什么提出「三大AI工厂」？｜WAIC 2026",
            )
        )

    def test_passing_mention_of_a_model_does_not_merge(self):
        # 两条都提到 Fable 5，但一条是它的发布、一条是别的模型打败了它
        self.assertFalse(
            cluster.same_event(
                "Redeploying Claude Fable 5 \\ Anthropic",
                "打败Fable 5！Kimi K3冲上第一，杨植麟导师很骄傲",
            )
        )

    def test_sibling_model_releases_stay_apart(self):
        self.assertFalse(
            cluster.same_event("Introducing Claude Opus 5", "Introducing Claude Sonnet 5")
        )

    def test_generic_word_title_is_not_absorbed_by_long_title(self):
        self.assertFalse(
            cluster.same_event(
                "Optimization",
                "Performance Profiling on AMD GPUs – Part 5: Profiling-Driven Kernel Optimization",
            )
        )

    def test_chinese_title_without_latin_anchor_does_not_match(self):
        self.assertFalse(cluster.same_event("Introducing Claude Opus 5", "智元机器人赴港上市"))

    def test_generic_words_do_not_bridge_unrelated_items(self):
        self.assertFalse(
            cluster.same_event("Introducing a new open model", "新的开源 model 发布了 AI")
        )


class PreferOfficialTest(unittest.TestCase):
    def test_official_l1_outranks_public_account(self):
        official = cluster.prefer_score(tier="L1", priority="P0", source_type="纯网页", stamp=1)
        wechat = cluster.prefer_score(tier="", priority="P2", source_type="公众号", stamp=2)
        self.assertGreater(official, wechat)

    def test_cluster_picks_official_as_primary(self):
        items = [
            {
                "fields": {"标题": "Claude Opus 5来了，Fable 5性能、一半价格", "来源": "智东西",
                           "来源类型": "公众号", "层级": ""},
            },
            {
                "fields": {"标题": "Introducing Claude Opus 5", "来源": "Anthropic",
                           "来源类型": "纯网页", "层级": "L1"},
            },
        ]
        primaries = cluster.collapse_for_brief(items)
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0]["source"], "Anthropic")
        self.assertEqual(len(primaries[0]["eventPeers"]), 1)


class EventAggregationTest(unittest.TestCase):
    def test_summary_entities_bridge_different_editorial_angles(self):
        techcrunch = {
            "title": "Jeff Dean and other top AI researchers are leaving Google",
            "summary": "Jeff Dean and Sanjay Ghemawat founded Discovery Loop to automate science.",
            "publishedDate": "2026-08-06",
        }
        huxiu = {
            "title": "谷歌AI领导体系大调整：哈萨比斯转任董事长",
            "summary": "Jeff Dean与Sanjay Ghemawat离职创办Discovery Loop，谷歌同步调整DeepMind管理架构。",
            "publishedDate": "2026-08-06",
        }
        self.assertTrue(cluster.contextual_same_event(techcrunch, huxiu))

    def test_one_shared_company_does_not_merge_unrelated_news(self):
        leadership = {
            "title": "Google adjusts DeepMind leadership",
            "summary": "Koray Kavukcuoglu takes over Gemini product delivery.",
            "publishedDate": "2026-08-06",
        }
        storage = {
            "title": "Google Cloud launches a new storage tier",
            "summary": "Google Cloud cuts archive storage prices for enterprise customers.",
            "publishedDate": "2026-08-06",
        }
        self.assertFalse(cluster.contextual_same_event(leadership, storage))

    def test_same_url_from_different_sources_is_kept(self):
        primary = {
            "source": "官方 RSS",
            "url": "https://example.com/release",
            "title": "Model release",
            "tier": "L1",
        }
        sibling = {
            "source": "媒体监测",
            "url": "https://example.com/release",
            "title": "模型发布解读",
            "tier": "L3",
        }
        aggregation = cluster.build_event_aggregation(primary, [sibling])
        self.assertEqual(aggregation["total"], 2)
        self.assertEqual(
            {item["source"] for group in aggregation["groups"] for item in group["items"]},
            {"官方 RSS", "媒体监测"},
        )

    def test_rebuild_preserves_members_already_collapsed_out_of_pool(self):
        signal = {
            "recordId": "primary",
            "source": "GitHub Releases",
            "url": "https://example.com/v2",
            "title": "Tool v2 released",
            "eventAggregation": {
                "total": 2,
                "groups": [
                    {
                        "key": "相关报道",
                        "items": [
                            {
                                "source": "GitHub Trending",
                                "url": "https://example.com/v1",
                                "title": "Tool v1 released",
                            }
                        ],
                    }
                ],
            },
        }
        rebuilt = cluster.enrich_with_pool([signal], [signal])
        members = [
            item
            for group in rebuilt[0]["eventAggregation"]["groups"]
            for item in group["items"]
        ]
        self.assertEqual(
            {(item["source"], item["url"]) for item in members},
            {
                ("GitHub Releases", "https://example.com/v2"),
                ("GitHub Trending", "https://example.com/v1"),
            },
        )


if __name__ == "__main__":
    unittest.main()
