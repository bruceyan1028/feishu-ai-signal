from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, patch

from src import notify, publish, sources_api, weekly


def ms(day: date) -> int:
    return int(
        datetime.combine(day, datetime.min.time(), tzinfo=timezone(timedelta(hours=8))).timestamp()
        * 1000
    )


def entry(record_id: str, day: date, *, source_id: str = "demo") -> dict:
    return {
        "record_id": record_id,
        "fields": {
            "source_id": source_id,
            "标题": f"title-{record_id}",
            "中文标题": f"标题-{record_id}",
            "中文摘要": "这是已经分析完成的中文摘要。",
            "AI深度解读": "足够长的深度解读。" * 30,
            "为何重要": "影响模型与基础设施选型。",
            "影响分": 85,
            "新颖度": 72,
            "可行动性": 68,
            "紧迫度": "High",
            "主题": ["AI", "产品"],
            "状态": "已分析",
            "发布时间": ms(day),
            "来源": "Demo Source",
            "分类": "前沿模型公司",
            "来源类型": "文章",
            "链接": {"link": f"https://example.com/{record_id}", "text": "原文"},
            "层级": "L1",
        },
    }


class WeeklyReportTest(unittest.TestCase):
    def test_week_window_is_rolling_seven_calendar_days(self):
        start, end = weekly.week_window(date(2026, 8, 17))
        self.assertEqual(start, date(2026, 8, 11))
        self.assertEqual(end, date(2026, 8, 17))
        self.assertEqual(weekly.week_id(end), "2026-W34")

    def test_collects_only_active_analyzed_entries_in_window(self):
        end = date(2026, 8, 17)
        records = [
            entry("keep", end),
            entry("old", end - timedelta(days=8)),
            entry("paused", end, source_id="paused"),
        ]
        params = [
            {"fields": {"source_id": "demo", "status": "active", "priority": "P0", "fetch_method": "RSS"}},
            {"fields": {"source_id": "paused", "status": "paused", "priority": "P1", "fetch_method": "RSS"}},
        ]
        result = weekly.collect_candidates(
            records, params, end - timedelta(days=6), end
        )
        self.assertEqual([item["record_id"] for item in result], ["keep"])

    def test_pending_entry_bypasses_window_and_active_filter(self):
        end = date(2026, 8, 17)
        records = [entry("focus", end - timedelta(days=30), source_id="paused")]
        params = [
            {"fields": {"source_id": "paused", "status": "paused", "priority": "P2", "fetch_method": "RSS"}}
        ]
        result = weekly.collect_candidates(
            records,
            params,
            end - timedelta(days=6),
            end,
            {"focus"},
        )
        self.assertEqual([item["record_id"] for item in result], ["focus"])

    def test_metrics_are_deterministic_and_compare_previous_week(self):
        signals = [
            {"impact": 90, "category": "模型", "source": "A"},
            {"impact": 70, "category": "算力", "source": "B"},
        ]
        previous = {
            "metrics": [
                {"label": "信号总数", "value": "1"},
                {"label": "平均影响分", "value": "75"},
            ]
        }
        metrics = {item["label"]: item for item in weekly.deterministic_metrics(signals, previous)}
        self.assertEqual(metrics["信号总数"], {"label": "信号总数", "value": "2", "sub": "较上周 +1"})
        self.assertEqual(metrics["高影响(≥80)"]["value"], "1")
        self.assertEqual(metrics["平均影响分"], {"label": "平均影响分", "value": "80", "sub": "较上周 +5"})

    def test_invalid_llm_references_are_removed(self):
        result = weekly.validate_synthesis(
            {
                "headline": "主线",
                "thesis": "综述",
                "areas": [
                    {"cat": "模型", "text": "观察", "refs": ["r1", "unknown"]}
                ],
                "topSignals": ["unknown"],
                "risks": ["风险"],
            },
            {"r1", "r2"},
            ["r2", "r1"],
        )
        self.assertEqual(result["areas"][0]["refs"], ["r1"])
        self.assertEqual(result["topSignals"], ["r2", "r1"])

    def test_generate_builds_and_upserts_a_complete_weekly_payload(self):
        end = date(2026, 8, 17)
        params = [
            {
                "fields": {
                    "source_id": "demo",
                    "status": "active",
                    "priority": "P0",
                    "fetch_method": "RSS",
                }
            }
        ]

        def read_table(_token, table_id, *args):
            if table_id == "entries":
                return [entry("r1", end)]
            return []

        synthesis = {
            "headline": "本周主线",
            "thesis": "本周综述",
            "areas": [{"cat": "模型", "text": "领域观察", "refs": ["r1"]}],
            "topSignals": ["r1"],
            "risks": ["风险"],
            "opportunities": ["机会"],
            "actions": ["行动"],
            "nextWeek": ["关注"],
        }
        with (
            patch("src.weekly.config.LLM_API_KEY", "test-key"),
            patch("src.weekly.config.FEISHU_ENTRY_TABLE_ID", "entries"),
            patch("src.weekly.config.FEISHU_WEEKLY_TABLE_ID", "weekly"),
            patch("src.weekly.config.FEISHU_WEEKLY_PENDING_TABLE_ID", "pending"),
            patch("src.weekly.feishu.get_tenant_access_token", return_value="token"),
            patch("src.weekly.feishu.read_param_records", return_value=params),
            patch("src.weekly.feishu.read_all_records_with_ids", side_effect=read_table),
            patch("src.weekly.feishu.create_record", return_value={"record_id": "weekly-row"}) as create,
            patch("src.weekly.feishu.batch_update_records"),
            patch("src.weekly.report._llm_json", return_value=synthesis),
        ):
            payload = weekly.generate(end)
        self.assertEqual(payload["weekId"], "2026-W34")
        self.assertEqual(payload["topSignals"], ["r1"])
        self.assertEqual(payload["weeklyRecordId"], "weekly-row")
        self.assertEqual(create.call_args.args[1], "weekly")
        stored = json.loads(create.call_args.args[2]["周报内容"])
        self.assertNotIn("signals", stored)
        self.assertEqual(json.loads(create.call_args.args[2]["信号记录ID"]), ["r1"])

    def test_weekly_export_writes_latest_and_archive_without_media_fetch(self):
        payload = {"weekId": "2026-W34", "signals": [{"recordId": "r1"}]}
        with tempfile.TemporaryDirectory() as tmp:
            site = publish.export_weekly(payload, tmp)
            latest = json.loads((site / "data" / "weekly-latest.json").read_text())
            archive = json.loads((site / "data" / "weekly-2026-W34.json").read_text())
            self.assertEqual(latest, payload)
            self.assertEqual(archive, payload)
            self.assertTrue((site / "index.html").exists())

    def test_daily_site_rebuild_keeps_existing_weekly_and_timeline(self) -> None:
        brief = {
            "date": "2026-08-20",
            "title": "日报",
            "intro": "引言",
            "bullets": [],
            "signals": [
                {
                    "recordId": "r1",
                    "title": "title",
                    "titleCn": "标题",
                    "source": "OpenAI",
                    "url": "https://example.com/a",
                    "category": "前沿模型公司",
                    "publishedDate": "2026-08-20",
                    "summary": "摘要",
                    "why": "原因",
                    "impact": 80,
                    "novelty": 70,
                    "actionability": 60,
                    "urgency": "中",
                    "tags": ["AI"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir(parents=True)
            (data / "weekly-latest.json").write_text(
                '{"weekId":"2026-W34","signals":[{"recordId":"r1"}]}',
                encoding="utf-8",
            )
            (data / "weekly-2026-W34.json").write_text(
                '{"weekId":"2026-W34"}', encoding="utf-8"
            )
            (data / "timeline-latest.json").write_text(
                '{"entities":[{"id":"nvidia"}]}', encoding="utf-8"
            )
            site = publish.build_site([brief], directory)
            latest = json.loads((site / "data" / "weekly-latest.json").read_text())
            self.assertEqual(latest["weekId"], "2026-W34")
            self.assertTrue((site / "data" / "weekly-2026-W34.json").exists())
            timeline = json.loads((site / "data" / "timeline-latest.json").read_text())
            self.assertEqual(timeline["entities"][0]["id"], "nvidia")

    def test_weekly_card_uses_stable_record_ids_for_top_signals(self):
        brief = {
            "title": "AI 周报",
            "headline": "本周主线",
            "thesis": "本周综述",
            "metrics": [{"label": "信号总数", "value": "2"}],
            "topSignals": ["r2"],
            "signals": [
                {"recordId": "r1", "titleCn": "一"},
                {"recordId": "r2", "titleCn": "二", "source": "B", "impact": 88},
            ],
        }
        card = notify.build_weekly_card(brief, "https://example.com/?week=2026-W34")
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("二", rendered)
        self.assertNotIn("**1. 一**", rendered)
        self.assertIn("查看完整 AI 周报", rendered)

    def test_weekly_detail_url_opens_task_report(self):
        url = notify.weekly_detail_url("https://example.com/", "2026-W34")
        self.assertEqual(
            url,
            "https://example.com/?page=tasks&tab=report&week=2026-W34",
        )

    def test_frontend_loads_real_weekly_data_without_demo_fallback(self):
        html = (Path(__file__).resolve().parents[1] / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("data/weekly-latest.json", html)
        self.assertIn("data/timeline-latest.json", html)
        self.assertIn("App.openWeeklyDetail", html)
        self.assertIn("/api/report-pending", html)
        self.assertIn('id="trackType"', html)
        self.assertNotIn('id="trackType" ${state.timelineWritable?\'\':\'disabled\'}', html)
        self.assertIn('<option value="人物">人物</option>', html)
        self.assertNotIn("2026-06-30 → 2026-07-07", html)

    @patch("src.sources_api.feishu.create_record")
    @patch("src.sources_api.feishu.read_all_records_with_ids")
    @patch("src.sources_api.feishu.get_tenant_access_token", return_value="token")
    def test_pending_api_persists_real_entry_record_id(
        self, _token, read_records, create_record
    ):
        read_records.side_effect = [
            [entry("r1", date(2026, 8, 17))],
            [],
        ]
        create_record.return_value = {"record_id": "queue-1"}
        with patch("src.sources_api._pending_table_id", return_value="pending"):
            result = sources_api.create_pending({"recordId": "r1"})
        self.assertEqual(result["item"]["recordId"], "r1")
        fields = create_record.call_args.args[2]
        self.assertEqual(fields["条目记录ID"], "r1")
        self.assertEqual(fields["状态"], "待纳入")

    @patch("src.notify.wait_until_public_json")
    @patch("src.notify.feishu.update_record")
    @patch("src.notify.feishu.send_interactive_message", return_value="message-1")
    @patch("src.notify.feishu.read_all_records_with_ids")
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    def test_weekly_send_is_recorded_per_week(
        self, _token, read_records, _send, update_record, _wait
    ):
        read_records.return_value = [
            {
                "record_id": "weekly-row",
                "fields": {"周报ID": "2026-W34", "发送状态": "待发送"},
            }
        ]
        brief = {
            "weekId": "2026-W34",
            "weeklyTableId": "weekly-table",
            "title": "AI 周报",
            "headline": "主线",
            "thesis": "综述",
            "metrics": [],
            "topSignals": [],
            "signals": [{"recordId": "r1"}],
        }
        result = notify.send_weekly_many(
            brief, "https://example.com", ["ou_1"], force=False
        )
        self.assertFalse(result["skipped"])
        self.assertEqual(result["messageIds"], {"ou_1": "message-1"})
        self.assertEqual(update_record.call_args.args[3]["发送状态"], "已发送")

    @patch("src.notify.wait_until_public_json")
    @patch("src.notify.feishu.update_record")
    @patch("src.notify.feishu.send_interactive_message", return_value="group-message")
    @patch("src.notify.feishu.read_all_records_with_ids")
    @patch("src.notify.feishu.get_tenant_access_token", return_value="token")
    def test_weekly_send_prefers_group_chat(
        self, _token, read_records, send_message, update_record, _wait
    ):
        read_records.return_value = [
            {
                "record_id": "weekly-row",
                "fields": {"周报ID": "2026-W34", "发送状态": "待发送"},
            }
        ]
        brief = {
            "weekId": "2026-W34",
            "weeklyTableId": "weekly-table",
            "title": "AI 周报",
            "headline": "主线",
            "thesis": "综述",
            "metrics": [],
            "topSignals": [],
            "signals": [{"recordId": "r1"}],
        }
        result = notify.send_weekly_many(
            brief, "https://example.com", ["ou_1"], force=False, chat_ids=["oc_group"]
        )
        self.assertEqual(result["messageIds"], {"oc_group": "group-message"})
        send_message.assert_called_once_with(
            "token", "oc_group", ANY, receive_id_type="chat_id"
        )
        self.assertEqual(update_record.call_args.args[3]["发送状态"], "已发送")


if __name__ == "__main__":
    unittest.main()
