import unittest

from src import source_view, sources_api


def record(record_id: str, **fields):
    base = {
        "source_id": "demo",
        "name": "Demo",
        "endpoint": "https://example.com/feed",
        "status": "active",
        "dimension": "前沿模型公司",
        "来源类型": "纯网页",
        "tier": "L1",
        "priority": "P0",
        "fetch_method": "RSS",
        "lookback_window": "24h",
    }
    base.update(fields)
    return {"record_id": record_id, "fields": base}


class SourceViewTest(unittest.TestCase):
    def test_three_states_survive_the_round_trip(self):
        """experimental 不能被折叠成「关闭」：待测和已暂停的处理方式完全不同。"""
        records = [
            record("r1", source_id="a", status="active"),
            record("r2", source_id="b", status="experimental"),
            record("r3", source_id="c", status="paused"),
        ]
        payload = source_view.build_payload(records)
        by_id = {s["id"]: s for s in payload["sources"]}
        self.assertEqual(by_id["a"]["status"], "active")
        self.assertEqual(by_id["b"]["status"], "experimental")
        self.assertEqual(by_id["c"]["status"], "paused")
        self.assertEqual(by_id["b"]["statusLabel"], "待测")
        self.assertTrue(by_id["a"]["on"])
        self.assertFalse(by_id["b"]["on"])

    def test_source_payload_never_leaks_filter_rules(self):
        records = [
            record(
                "r1",
                keyword_regex="(?i)(agent|模型)",
                min_content_chars=400,
                dedup_key="normalize(url)",
                extra_config='{"max_articles": 5}',
            )
        ]
        payload = source_view.build_payload(records)
        serialized = repr(payload)
        for secret in ("keyword_regex", "min_content_chars", "dedup_key", "extra_config"):
            self.assertNotIn(secret, serialized)

    def test_brief_count_is_selection_not_ingestion(self):
        """入库量和入选量是两个口径，天天入库却从不入选才是要暴露的问题。"""
        records = [
            record("r1", source_id="loud", **{"条目数": 20}),
            record("r2", source_id="quiet", **{"条目数": 0}),
        ]
        briefs = [
            {"signals": [{"sourceId": "loud"}, {"sourceId": "quiet"}]},
            {"signals": [{"sourceId": "quiet"}]},
        ]
        payload = source_view.build_payload(records, briefs=briefs)
        by_id = {s["id"]: s for s in payload["sources"]}
        self.assertEqual(by_id["loud"]["perDay"], 20)
        self.assertEqual(by_id["loud"]["briefCount"], 1)
        self.assertEqual(by_id["quiet"]["perDay"], 0)
        self.assertEqual(by_id["quiet"]["briefCount"], 2)
        self.assertEqual(payload["briefWindowDays"], 2)

    def test_static_export_is_read_only(self):
        payload = source_view.build_payload([record("r1")], writable=False)
        self.assertEqual(payload["meta"]["writable"], [])
        live = source_view.build_payload([record("r1")], writable=True)
        self.assertIn("status", live["meta"]["writable"])

    def test_priority_maps_between_feishu_codes_and_labels(self):
        payload = source_view.build_payload(
            [record("r1", priority="P2"), record("r2", source_id="x", priority="")]
        )
        priorities = {s["id"]: s["priority"] for s in payload["sources"]}
        self.assertEqual(priorities["demo"], "低")
        self.assertEqual(priorities["x"], "中")

    def test_sorted_by_status_then_priority(self):
        records = [
            record("r1", source_id="paused-p0", status="paused", priority="P0"),
            record("r2", source_id="active-p2", status="active", priority="P2"),
            record("r3", source_id="active-p0", status="active", priority="P0"),
        ]
        payload = source_view.build_payload(records)
        self.assertEqual(
            [s["id"] for s in payload["sources"]],
            ["active-p0", "active-p2", "paused-p0"],
        )


class SourcesApiTest(unittest.TestCase):
    def test_patch_translates_labels_to_feishu_codes(self):
        self.assertEqual(
            sources_api._patch_fields({"priority": "高"}), {"priority": "P0"}
        )
        self.assertEqual(
            sources_api._patch_fields({"status": "experimental"}),
            {"status": "experimental"},
        )

    def test_patch_rejects_unknown_priority(self):
        with self.assertRaises(sources_api.ApiError):
            sources_api._patch_fields({"priority": "紧急"})

    def test_patch_rejects_empty_body(self):
        with self.assertRaises(sources_api.ApiError):
            sources_api._patch_fields({"name": "改名字不走这个接口"})

    def test_slugify_avoids_collisions(self):
        taken = {"machine-heart", "machine-heart-2"}
        self.assertEqual(sources_api.slugify_source_id("机器之心 AI", taken), "ai")
        self.assertEqual(
            sources_api.slugify_source_id("Machine Heart", taken), "machine-heart-3"
        )


if __name__ == "__main__":
    unittest.main()
