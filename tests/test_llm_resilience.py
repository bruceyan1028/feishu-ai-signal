"""LLM 调用的容错行为（不打外网）。

这两条都是 2026-08-08 那次事故的回归测试：网关返回 418，既没有重试，
单条信号的异常又直接冒到顶层，把跑了 28 分钟的整份简报作废。
"""
from __future__ import annotations

import unittest
from unittest import mock

from src import daily, report


class LlmRetryTest(unittest.TestCase):
    def _response(self, status: int, content: str = '{"ok": 1}'):
        resp = mock.MagicMock()
        resp.status_code = status
        resp.json.return_value = {"choices": [{"message": {"content": content}}]}
        resp.raise_for_status = mock.MagicMock()
        return resp

    def test_gateway_418_is_retried_not_raised(self):
        posts = [self._response(418), self._response(200)]
        with mock.patch("requests.post", side_effect=posts) as post:
            with mock.patch("time.sleep"):
                result = report._llm_json("prompt")
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(post.call_count, 2)

    def test_retry_status_covers_transient_gateway_codes(self):
        for status in (408, 409, 418, 425, 429, 500, 502, 503, 504):
            self.assertIn(status, report._RETRY_STATUS)

    def test_client_errors_are_not_retried(self):
        # 400/401 重试没有意义，只会让每条信号都白等三轮
        for status in (400, 401, 403, 422):
            self.assertNotIn(status, report._RETRY_STATUS)


class AnalysisFailureToleranceTest(unittest.TestCase):
    def test_isolated_failures_do_not_abort_the_brief(self):
        self.assertFalse(daily.analysis_failure_is_systemic(1, 30))
        self.assertFalse(daily.analysis_failure_is_systemic(15, 30))

    def test_majority_failure_aborts(self):
        self.assertTrue(daily.analysis_failure_is_systemic(16, 30))
        self.assertTrue(daily.analysis_failure_is_systemic(30, 30))

    def test_no_attempts_is_not_a_failure(self):
        # 全部命中缓存时一条都不用分析，这不该被当成故障
        self.assertFalse(daily.analysis_failure_is_systemic(0, 0))


if __name__ == "__main__":
    unittest.main()
