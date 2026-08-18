import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src import timeline


class TimelineTest(unittest.TestCase):
    def test_entity_name_alias_keyword_and_exclusion_matching(self):
        entity = {
            "name": "OpenAI",
            "aliases": ["ChatGPT"],
            "keywords": ["GPT-6"],
            "excludes": ["招聘"],
        }
        self.assertEqual(
            timeline.match_entity(
                entity, {"title": "OpenAI launches a model", "summary": "", "tags": []}
            ),
            ("名称：OpenAI", 95),
        )
        self.assertEqual(
            timeline.match_entity(
                entity, {"title": "ChatGPT update", "summary": "", "tags": []}
            ),
            ("别名：ChatGPT", 90),
        )
        self.assertIsNone(
            timeline.match_entity(
                entity,
                {"title": "OpenAI 招聘工程师", "summary": "", "tags": []},
            )
        )
        # 英文实体使用单词边界，避免 AI 错命中 paid。
        self.assertIsNone(
            timeline.match_entity(
                {"name": "AI", "aliases": [], "keywords": [], "excludes": []},
                {"title": "Paid plan", "summary": "", "tags": []},
            )
        )

    def test_match_events_applies_lookback_impact_and_event_type(self):
        entities = [
            {
                "id": "anthropic",
                "name": "Anthropic",
                "aliases": ["Claude"],
                "keywords": [],
                "excludes": [],
                "status": "active",
                "lookbackDays": 30,
                "minImpact": 70,
            }
        ]
        records = [{"record_id": "keep"}, {"record_id": "low"}, {"record_id": "old"}]
        signals = {
            "keep": {
                "recordId": "keep",
                "title": "Anthropic releases Claude",
                "titleCn": "Anthropic 发布 Claude",
                "summary": "新模型上线",
                "tags": [],
                "impact": 85,
                "publishedDate": "2026-08-10",
                "source": "Anthropic",
                "url": "https://example.com/keep",
            },
            "low": {
                "recordId": "low",
                "title": "Claude update",
                "summary": "",
                "tags": [],
                "impact": 40,
                "publishedDate": "2026-08-10",
            },
            "old": {
                "recordId": "old",
                "title": "Anthropic release",
                "summary": "",
                "tags": [],
                "impact": 90,
                "publishedDate": "2026-01-01",
            },
        }
        with patch(
            "src.timeline.publish._signal_from_record",
            side_effect=lambda record: signals[record["record_id"]],
        ):
            events = timeline.match_events(
                entities, records, datetime.fromisoformat("2026-08-18T12:00:00+08:00")
            )
        self.assertEqual([event["信号记录ID"] for event in events], ["keep"])
        self.assertEqual(events[0]["事件类型"], "产品发布")
        self.assertTrue(events[0]["event_id"].startswith("anthropic:"))
        self.assertNotIn("keep", events[0]["event_id"])

    def test_duplicate_entry_records_collapse_into_one_event(self):
        entity = {
            "id": "nvidia",
            "name": "NVIDIA",
            "aliases": ["英伟达"],
            "keywords": [],
            "excludes": [],
            "status": "active",
            "lookbackDays": 30,
            "minImpact": 0,
        }
        records = [{"record_id": "first"}, {"record_id": "duplicate"}]
        signals = {
            record_id: {
                "recordId": record_id,
                "title": "NVIDIA opens Alpamayo 2 Super",
                "titleCn": "英伟达开放 Alpamayo 2 Super 商用",
                "summary": "面向自动驾驶",
                "tags": [],
                "impact": impact,
                "publishedDate": "2026-08-04",
                "source": "NVIDIA",
                "url": f"https://example.com/{record_id}",
            }
            for record_id, impact in (("first", 80), ("duplicate", 85))
        }
        with patch(
            "src.timeline.publish._signal_from_record",
            side_effect=lambda record: signals[record["record_id"]],
        ):
            events = timeline.match_events(
                [entity],
                records,
                datetime.fromisoformat("2026-08-18T12:00:00+08:00"),
            )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["信号记录ID"], "duplicate")
        self.assertEqual(events[0]["影响分"], 85)

    def test_payload_groups_events_by_stable_entity_id(self):
        entities = [
            {
                "recordId": "rec_entity",
                "id": "openai",
                "name": "OpenAI",
                "type": "机构",
                "status": "active",
            }
        ]
        records = [
            {
                "record_id": "rec_event",
                "fields": {
                    "event_id": "openai:signal-1",
                    "entity_id": "openai",
                    "信号记录ID": "signal-1",
                    "事件日期": 1_776_038_400_000,
                    "事件类型": "产品发布",
                    "标题": "发布新模型",
                    "摘要": "摘要",
                    "影响分": 88,
                    "来源": "OpenAI",
                    "原文链接": "https://example.com",
                    "匹配依据": "名称：OpenAI",
                    "置信度": 95,
                },
            }
        ]
        payload = timeline.payload_from_records(entities, records)
        self.assertEqual(payload["entities"][0]["events"][0]["recordId"], "signal-1")
        self.assertEqual(payload["entities"][0]["events"][0]["impact"], 88)

    def test_payload_defensively_hides_duplicate_entities_and_events(self):
        entity = {
            "recordId": "first",
            "id": "nvidia",
            "name": "NVIDIA",
            "type": "机构",
            "status": "active",
        }
        event = {
            "record_id": "event-1",
            "fields": {
                "event_id": "nvidia:stable",
                "entity_id": "nvidia",
                "信号记录ID": "signal-1",
                "事件日期": 1_776_038_400_000,
                "标题": "同一事件",
            },
        }
        payload = timeline.payload_from_records(
            [entity, {**entity, "recordId": "duplicate"}],
            [event, {**event, "record_id": "event-duplicate"}],
        )
        self.assertEqual(len(payload["entities"]), 1)
        self.assertEqual(len(payload["entities"][0]["events"]), 1)

    def test_same_day_events_become_one_summary_with_original_links(self):
        events = [
            {
                "id": f"nvidia:{index}",
                "recordId": f"signal-{index}",
                "date": "2026-08-04",
                "type": event_type,
                "title": title,
                "summary": f"{title}的实质内容",
                "impact": impact,
                "source": "NVIDIA",
                "url": f"https://example.com/{index}",
                "confidence": 95,
            }
            for index, event_type, title, impact in (
                (1, "产品发布", "发布自动驾驶模型", 90),
                (2, "产品发布", "开放商业许可", 80),
                (3, "技术成果", "公布研究结果", 70),
            )
        ]
        summaries = timeline.summarize_daily_events(events)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["count"], 3)
        self.assertEqual(len(summaries[0]["originals"]), 3)
        self.assertNotIn("当日共捕捉", summaries[0]["summary"])
        self.assertNotIn("主要涉及", summaries[0]["summary"])
        self.assertIn("发布自动驾驶模型的实质内容", summaries[0]["summary"])

    def test_write_payload_creates_public_timeline_json(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "data" / "timeline-latest.json"
            timeline.write_payload({"entities": []}, output)
            self.assertIn('"entities": []', output.read_text(encoding="utf-8"))

    def test_strategy_validation_removes_unbacked_claims_and_relations(self):
        raw = {
            "overview": "总体判断",
            "themes": [
                {
                    "id": "theme-1",
                    "name": "基础设施",
                    "summary": "形成完整栈",
                    "eventIds": ["daily:2026-08-01", "invented"],
                }
            ],
            "relations": [
                {
                    "fromEventId": "daily:2026-08-01",
                    "toEventId": "daily:2026-08-02",
                    "label": "能力前置",
                    "confidence": 85,
                },
                {
                    "fromEventId": "invented",
                    "toEventId": "daily:2026-08-02",
                },
            ],
            "hypotheses": [
                {
                    "title": "全栈布局",
                    "assessment": "判断",
                    "confidence": 82,
                    "themeIds": ["theme-1", "invented-theme"],
                    "evidenceEventIds": ["daily:2026-08-01", "invented"],
                },
                {
                    "title": "无证据判断",
                    "evidenceEventIds": ["invented"],
                },
            ],
        }
        valid = {"daily:2026-08-01", "daily:2026-08-02"}
        result = timeline.validate_strategy(raw, valid, "fingerprint")
        self.assertEqual(result["themes"][0]["eventIds"], ["daily:2026-08-01"])
        self.assertEqual(len(result["relations"]), 1)
        self.assertEqual(len(result["hypotheses"]), 1)
        self.assertEqual(result["hypotheses"][0]["themeIds"], ["theme-1"])

    def test_strategy_cache_skips_llm_when_evidence_is_unchanged(self):
        events = [
            {
                "id": "daily:2026-08-01",
                "title": "事件",
                "summary": "摘要",
                "originals": [],
            }
        ]
        cached = {
            "fingerprint": timeline.strategy_fingerprint(events),
            "overview": "缓存判断",
        }
        with patch("src.timeline.report._llm_json") as llm:
            result = timeline.synthesize_strategy(
                {"name": "NVIDIA"}, events, cached
            )
        self.assertEqual(result, cached)
        llm.assert_not_called()

    def test_frontend_loads_real_timeline_without_preview_fallback(self):
        html = (Path(__file__).parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn("loadTimeline()", html)
        self.assertIn("data/timeline-latest.json", html)
        self.assertIn("let timelineData = {};", html)
        self.assertIn("长期布局研判", html)
        self.assertIn("潜在布局意图", html)
        self.assertNotIn("timelineData = timelinePreviewData", html)


if __name__ == "__main__":
    unittest.main()
