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


if __name__ == "__main__":
    unittest.main()
