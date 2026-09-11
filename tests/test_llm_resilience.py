"""LLM 调用的容错行为（不打外网）。

这两条都是 2026-08-08 那次事故的回归测试：网关返回 418，既没有重试，
单条信号的异常又直接冒到顶层，把跑了 28 分钟的整份简报作废。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Barrier
import unittest
from unittest import mock

from src import daily, report


class LlmRetryTest(unittest.TestCase):
    def _response(self, status: int, content: str = '{"ok": 1}'):
        resp = mock.MagicMock()
        resp.status_code = status
        resp.json.return_value = {"choices": [{"message": {"content": content}}]}
        resp.iter_content.return_value = [
            json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        ]
        resp.encoding = "utf-8"
        resp.raise_for_status = mock.MagicMock()
        return resp

    def test_gateway_418_is_retried_not_raised(self):
        posts = [self._response(418), self._response(200)]
        with mock.patch("requests.post", side_effect=posts) as post:
            with mock.patch("time.sleep"):
                result = report._llm_json("prompt")
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(post.call_count, 2)

    def test_llm_uses_bounded_connect_and_read_timeouts(self):
        response = self._response(200)
        with mock.patch("requests.post", return_value=response) as post:
            result = report._llm_json("prompt")
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(
            post.call_args.kwargs["timeout"],
            (report.config.LLM_CONNECT_TIMEOUT_SECONDS, report.config.LLM_READ_TIMEOUT_SECONDS),
        )
        self.assertTrue(post.call_args.kwargs["stream"])

    def test_retriable_primary_failure_switches_to_configured_fallback(self):
        backup = self._response(200)
        backup.iter_content.return_value = [b'{"choices":[{"message":{"content":"{\\"ok\\":2}"}}]}']
        fallback = {
            "name": "fallback_1",
            "api_key": "fallback-key",
            "base_url": "https://fallback.example/v1",
            "model": "fallback-model",
        }
        with (
            mock.patch.object(report.config, "LLM_API_KEY", "primary-key"),
            mock.patch.object(report.config, "LLM_FALLBACK_PROVIDERS", (fallback,)),
            mock.patch.object(report.config, "LLM_MAX_RETRIES", 1),
            mock.patch("requests.post", side_effect=[__import__("requests").Timeout("down"), backup]) as post,
        ):
            result = report._llm_json("prompt")
        self.assertEqual(result, {"ok": 2})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer fallback-key")

    def test_retry_status_covers_transient_gateway_codes(self):
        for status in (408, 409, 418, 425, 429, 500, 502, 503, 504):
            self.assertIn(status, report._RETRY_STATUS)

    def test_client_errors_are_not_retried(self):
        # 400/401 重试没有意义，只会让每条信号都白等三轮
        for status in (400, 401, 403, 422):
            self.assertNotIn(status, report._RETRY_STATUS)

    def test_error_carries_gateway_reason(self):
        # CI 日志里 URL 是打码的，正文是唯一能看出「为什么被拒」的东西
        blocked = self._response(418)
        blocked.text = '{"error":"access denied from this IP"}'
        blocked.iter_content.return_value = [blocked.text.encode()]
        with mock.patch("requests.post", return_value=blocked):
            with mock.patch("time.sleep"):
                with self.assertRaises(report.LlmHttpError) as caught:
                    report._llm_json("prompt")
        self.assertIn("access denied from this IP", str(caught.exception))
        self.assertEqual(caught.exception.status, 418)


class AnalysisFailureToleranceTest(unittest.TestCase):
    def test_manually_excluded_entry_is_not_a_daily_candidate(self):
        now = datetime.now(timezone.utc)
        records = [
            {
                "record_id": "excluded",
                "fields": {
                    "source_id": "demo",
                    "标题": "Unrelated item",
                    "发布时间": int(now.timestamp() * 1000),
                    "状态": "已排除",
                },
            }
        ]
        self.assertEqual(
            daily.select_candidates(records, {"demo": "P1"}, {"demo"}, now=now),
            [],
        )

    def test_legacy_deep_analysis_is_not_on_daily_critical_path_by_default(self):
        fields = {"原文": "x" * 500, "来源": "demo", "标题": "Existing signal"}
        analysis = {"summary_cn": "已有摘要", "why": "已有结论"}
        with mock.patch.object(daily.config, "DAILY_FILL_LEGACY_DEEP_ANALYSIS", False):
            with mock.patch.object(daily.report, "_llm_json") as llm:
                self.assertEqual(daily._ensure_deep_analysis(fields, analysis), {})
        llm.assert_not_called()

    def test_isolated_failures_do_not_abort_the_brief(self):
        self.assertFalse(daily.analysis_failure_is_systemic(1, 30))
        self.assertFalse(daily.analysis_failure_is_systemic(15, 30))

    def test_majority_failure_aborts(self):
        self.assertTrue(daily.analysis_failure_is_systemic(16, 30))
        self.assertTrue(daily.analysis_failure_is_systemic(30, 30))

    def test_no_attempts_is_not_a_failure(self):
        # 全部命中缓存时一条都不用分析，这不该被当成故障
        self.assertFalse(daily.analysis_failure_is_systemic(0, 0))

    def test_daily_analyzes_missing_candidates_concurrently(self):
        stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        candidates = [
            {
                "record_id": f"r{index}",
                "source_id": "demo",
                "priority": "P0",
                "fields": {
                    "source_id": "demo",
                    "标题": f"Model release {index}",
                    "原文": "Evidence about an AI model release. " * 8,
                    "发布时间": stamp - index,
                    "链接": {"link": f"https://example.com/{index}"},
                    "来源类型": "文章",
                },
            }
            for index in range(2)
        ]
        params = [{"fields": {"source_id": "demo", "status": "active", "priority": "P0"}}]
        barrier = Barrier(2)

        def analyze(fields):
            barrier.wait(timeout=2)
            title = str(fields["标题"])
            return {
                "title_cn": title,
                "summary_cn": "摘要",
                "deep_analysis_cn": "解读",
                "why": "重要",
                "impact": 80,
                "novelty": 70,
                "actionability": 60,
                "urgency": "中",
                "topics": ["AI"],
                "category": "",
            }

        def read_records(_token, table_id, *_args):
            return [] if table_id == "briefs" else candidates

        with (
            mock.patch.object(daily.config, "LLM_API_KEY", "test-key"),
            mock.patch.object(daily.config, "FEISHU_ENTRY_TABLE_ID", "entries"),
            mock.patch.object(daily.config, "FEISHU_BRIEF_TABLE_ID", "briefs"),
            mock.patch.object(daily.config, "DAILY_ANALYSIS_CONCURRENCY", 2),
            mock.patch.object(daily.feishu, "get_tenant_access_token", return_value="token"),
            mock.patch.object(daily.feishu, "ensure_entry_enrichment_fields"),
            mock.patch.object(daily.feishu, "ensure_select_option"),
            mock.patch.object(daily.feishu, "read_param_records", return_value=params),
            mock.patch.object(daily.feishu, "read_all_records_with_ids", side_effect=read_records),
            mock.patch.object(daily.feishu, "batch_update_records"),
            mock.patch.object(daily.feishu, "create_record", return_value={"record_id": "brief-row"}),
            mock.patch.object(daily, "select_candidates", return_value=candidates),
            mock.patch.object(daily.cluster, "collapse_for_brief", side_effect=lambda items, **_kwargs: items),
            mock.patch.object(daily, "analyze_signal", side_effect=analyze) as analyzed,
            mock.patch.object(daily.rss, "fetch_article_media", return_value={}),
            mock.patch.object(daily.report, "_llm_json", return_value={"intro": "导语", "bullets": []}),
        ):
            payload = daily.generate("2026-09-11")

        self.assertEqual(analyzed.call_count, 2)
        self.assertEqual(payload["signals"][0]["recordId"], "r0")


if __name__ == "__main__":
    unittest.main()
