from __future__ import annotations

import unittest

from src import backfill_published_dates as backfill


class PublishedDateBackfillTest(unittest.TestCase):
    def test_candidate_requires_scrape_and_near_collection_time(self) -> None:
        self.assertTrue(
            backfill._candidate(
                {
                    "fields": {
                        "路由来源": "Scrape",
                        "发布时间": 10_000_000,
                        "采集时间": 10_000_000 + 60_000,
                    }
                }
            )
        )
        self.assertFalse(
            backfill._candidate(
                {
                    "fields": {
                        "路由来源": "RSS",
                        "发布时间": 10_000_000,
                        "采集时间": 10_000_000,
                    }
                }
            )
        )
        self.assertFalse(
            backfill._candidate(
                {
                    "fields": {
                        "路由来源": "Scrape",
                        "发布时间": 10_000_000,
                        "采集时间": 10_000_000 + 4 * 3_600_000,
                    }
                }
            )
        )

    def test_partition_only_deletes_unresolved_records(self) -> None:
        changed, unresolved = backfill._partition_inspected(
            [
                {"record_id": "fix", "corrected_ms": 1, "changed": True},
                {"record_id": "same", "corrected_ms": 2, "changed": False},
                {"record_id": "drop", "corrected_ms": None, "changed": False},
            ]
        )
        self.assertEqual([item["record_id"] for item in changed], ["fix"])
        self.assertEqual([item["record_id"] for item in unresolved], ["drop"])


if __name__ == "__main__":
    unittest.main()
