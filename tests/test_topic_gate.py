"""主题准入闸：泛媒体源靠正文关键词蹭进来的跑题文章不能进简报。"""
from __future__ import annotations

import json
import unittest

from src import daily


def _param(source_id: str, *, min_hits: int | None = None, keyword: str = "(算法|芯片)") -> dict:
    extra = {"link_path_include": "^/article/"}
    if min_hits is not None:
        extra["keyword_min_hits"] = min_hits
    return {
        "fields": {
            "source_id": source_id,
            "keyword_regex": keyword,
            "extra_config": json.dumps(extra, ensure_ascii=False),
        }
    }


class BodyAdmittedSourcesTest(unittest.TestCase):
    def test_only_min_hits_two_or_more_counts(self) -> None:
        params = [
            _param("huxiu", min_hits=2),
            _param("guokr", min_hits=3),
            _param("leiphone", min_hits=1),
            _param("openai-news"),
        ]
        self.assertEqual(daily._body_admitted_source_ids(params), {"huxiu", "guokr"})

    def test_source_without_keyword_regex_is_out_of_scope(self) -> None:
        params = [_param("social-media", min_hits=2, keyword="")]
        self.assertEqual(daily._body_admitted_source_ids(params), set())

    def test_broken_extra_config_does_not_raise(self) -> None:
        params = [{"fields": {"source_id": "huxiu", "keyword_regex": "AI", "extra_config": "{"}}]
        self.assertEqual(daily._body_admitted_source_ids(params), set())


class AiRelevanceTest(unittest.TestCase):
    def test_rejects_business_article_that_only_mentions_algorithms_in_body(self) -> None:
        fields = {"标题": "城市更新下半场：得「情绪」者，得 2.7 万亿"}
        analysis = {
            "title_cn": "城市更新转向“引人思维”：情绪经济规模达2.72万亿元",
            "summary_cn": "文章认为情绪经济规模已达2.72万亿元，城市更新应转向围绕本地日常需求"
            "打造疗愈、连接、圈层三类可持续复购场景。",
        }
        self.assertFalse(daily.is_ai_relevant(fields, analysis))

    def test_rejects_consumer_hardware_and_ip_consumption(self) -> None:
        for title in (
            "苹果据称研发首款OLED触屏MacBook Pro",
            "泡泡玛特需求可持续性的底层逻辑：IP与情绪消费",
            "8月新势力销量分化：零跑破10万、鸿蒙回落、理想反弹",
        ):
            self.assertFalse(daily.is_ai_relevant({"标题": title}), title)

    def test_keeps_latin_acronyms_adjacent_to_chinese(self) -> None:
        # 「因AI裁员」里 \b 不成立，早期写法会把这类真信号误杀。
        for title in (
            "超过半数因AI裁员的企业事后后悔并重新招聘",
            "惠普AI服务器与网络业务增长强劲",
            "当客户把AI带进会议室，顾问如何证明专业价值",
        ):
            self.assertTrue(daily.is_ai_relevant({"标题": title}), title)

    def test_keeps_signal_whose_topic_only_shows_in_summary(self) -> None:
        fields = {"标题": "库克执掌苹果15年后卸任"}
        analysis = {"title_cn": "库克卸任", "summary_cn": "接棒者需要迎战大模型带来的终端竞争。"}
        self.assertTrue(daily.is_ai_relevant(fields, analysis))

    def test_falls_back_to_stored_fields_without_analysis(self) -> None:
        fields = {"标题": "raw", "中文标题": "阿里更新Qwen3.8-Max", "中文摘要": "前端编程能力登顶"}
        self.assertTrue(daily.is_ai_relevant(fields))


if __name__ == "__main__":
    unittest.main()
