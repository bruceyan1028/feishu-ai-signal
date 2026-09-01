"""纯日期发布时间：lookback 宽限一个采集周期。"""
from __future__ import annotations

import unittest
from unittest import mock

from src import process


_BODY = (
    "Claude Opus 5 is available today. It is a thoughtful and proactive model that "
    "comes close to the frontier intelligence of the previous generation at half "
    "the price. On coding and knowledge work evaluations, Opus 5 is the new state "
    "of the art according to published benchmarks."
)


def _item(*, published_raw: str, url: str = "https://www.anthropic.com/news/x") -> dict:
    return {
        "title": "Introducing Claude Opus 5",
        "url": url,
        "body": _BODY,
        "published_raw": published_raw,
        "feed": {
            "id": "anthropic_news",
            "fetch_method": "Scrape",
            "lookback_hours": 24,
            "min_content_chars": 100,
            "keyword_regex": "",
        },
    }


class DateOnlyLookbackTest(unittest.TestCase):
    def test_detects_date_only_formats(self) -> None:
        for raw in (
            "Aug 27, 2026",
            "March 10, 2026",
            "27 Aug 2026",
            "2026-08-26",
            "2026/03/31",
            "2026年8月26日",
        ):
            self.assertTrue(process.is_date_only_published(raw), raw)

    def test_rejects_timestamps_with_time(self) -> None:
        for raw in (
            "2026-08-27T12:06:53Z",
            "Tue, 22 Apr 2025 06:00:00 GMT",
            "2026-08-31T15:22:01+08:00",
            "Aug 27, 2026 10:30 AM",
        ):
            self.assertFalse(process.is_date_only_published(raw), raw)

    def test_effective_lookback_adds_grace_only_for_date_only(self) -> None:
        self.assertEqual(
            process.effective_lookback_ms(24, "Aug 27, 2026"),
            (24 + process.DATE_ONLY_LOOKBACK_GRACE_HOURS) * 3600000,
        )
        self.assertEqual(
            process.effective_lookback_ms(24, "2026-08-27T12:06:53Z"),
            24 * 3600000,
        )

    def test_date_only_kept_when_age_between_24h_and_48h(self) -> None:
        # Aug 27 00:00 UTC → Aug 28 02:45 UTC ≈ 26.75h，配置 24h 本会误杀。
        published = "Aug 27, 2026"
        now = process.parse_date_ms("2026-08-28T02:45:00Z")
        assert now is not None
        drop_stats: dict[str, int] = {}
        with mock.patch.object(process, "now_ms", return_value=now):
            kept = process.process_and_clean([_item(published_raw=published)], drop_stats=drop_stats)
        self.assertEqual(len(kept), 1)
        self.assertEqual(drop_stats.get("anthropic_news", 0), 0)

    def test_date_only_still_dropped_when_older_than_grace(self) -> None:
        published = "Aug 26, 2026"
        now = process.parse_date_ms("2026-08-28T02:45:00Z")  # ~50.75h
        assert now is not None
        drop_stats: dict[str, int] = {}
        with mock.patch.object(process, "now_ms", return_value=now):
            kept = process.process_and_clean([_item(published_raw=published)], drop_stats=drop_stats)
        self.assertEqual(kept, [])
        self.assertEqual(drop_stats.get("anthropic_news", 0), 1)

    def test_timestamp_with_time_gets_no_grace(self) -> None:
        # 有时分秒：26.75h > 24h → 仍按配置杀掉，不扩窗。
        published = "2026-08-27T00:00:00Z"
        now = process.parse_date_ms("2026-08-28T02:45:00Z")
        assert now is not None
        drop_stats: dict[str, int] = {}
        with mock.patch.object(process, "now_ms", return_value=now):
            kept = process.process_and_clean([_item(published_raw=published)], drop_stats=drop_stats)
        self.assertEqual(kept, [])
        self.assertEqual(drop_stats.get("anthropic_news", 0), 1)


if __name__ == "__main__":
    unittest.main()
