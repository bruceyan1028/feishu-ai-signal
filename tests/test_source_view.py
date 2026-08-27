import contextlib
import unittest

from src import config, source_view, sources_api


@contextlib.contextmanager
def _patched(rows, calls):
    """替掉信号源表的读写，记录 update_record 调用。"""
    feishu = sources_api.feishu
    originals = (feishu.read_all_records_with_ids, feishu.update_record)
    table_id = config.FEISHU_SOURCE_TABLE_ID
    feishu.read_all_records_with_ids = lambda _token, _table: rows
    feishu.update_record = lambda _t, _table, rid, fields: calls.append((rid, fields))
    config.FEISHU_SOURCE_TABLE_ID = "tbl_source"
    try:
        yield
    finally:
        feishu.read_all_records_with_ids, feishu.update_record = originals
        config.FEISHU_SOURCE_TABLE_ID = table_id


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

    def test_chinese_media_dimensions_collapse_to_one_type(self):
        records = [
            record("r1", source_id="qbitai", dimension="中文科技媒体"),
            record("r2", source_id="caixin", dimension="中文综合媒体"),
        ]
        payload = source_view.build_payload(records)
        by_id = {s["id"]: s for s in payload["sources"]}
        self.assertEqual(by_id["qbitai"]["type"], "中文媒体")
        self.assertEqual(by_id["caixin"]["type"], "中文媒体")
        self.assertEqual(payload["meta"]["types"], ["中文媒体"])

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


class SourceDetailTest(unittest.TestCase):
    def test_rule_fields_stay_out_of_the_public_payload(self):
        """公开站点的 sources.json 由 build_payload 导出，不能带筛选规则。"""
        rec = record(
            "r1",
            keyword_regex="(gpt|claude)",
            min_content_chars=300,
            extra_config='{"max_articles":8}',
        )
        public = source_view.build_payload([rec], writable=False)
        serialized = str(public)
        for leaked in ("keyword_regex", "keywordRegex", "extra_config", "max_articles"):
            self.assertNotIn(leaked, serialized)
        self.assertEqual(public["meta"]["writable"], [])

    def test_detail_exposes_full_config_and_effective_values(self):
        rec = record(
            "r1",
            keyword_regex="(gpt|claude)",
            min_content_chars=300,
            lookback_window="7d",
            extra_config='{"keyword_min_hits":2,"title_exclude_regex":"^广告"}',
        )
        detail = source_view.build_detail(rec)
        self.assertEqual(detail["config"]["keywordRegex"], "(gpt|claude)")
        self.assertEqual(detail["config"]["minContentChars"], 300)
        self.assertEqual(detail["effective"]["lookbackHours"], 168)
        self.assertEqual(detail["effective"]["keywordMinHits"], 2)
        self.assertEqual(detail["effective"]["titleExcludeRegex"], "^广告")
        self.assertTrue(detail["effective"]["keywordRegexValid"])

    def test_detail_flags_existing_broken_rules_instead_of_hiding_them(self):
        # 坏正则/坏 JSON 在采集时被静默兜底，配置台必须把它们标出来
        rec = record("r1", keyword_regex="(unclosed", extra_config="{oops}")
        detail = source_view.build_detail(rec)
        self.assertFalse(detail["effective"]["keywordRegexValid"])
        self.assertFalse(detail["effective"]["extraConfigValid"])
        self.assertEqual(detail["config"]["extraConfig"], "{oops}")

    def test_endpoint_is_written_back_as_a_url_field(self):
        fields, _ = source_view.normalize_config_patch(
            {"endpoint": "https://example.com/news"}, {}
        )
        self.assertEqual(
            fields["endpoint"],
            {"link": "https://example.com/news", "text": "https://example.com/news"},
        )

    def test_reformatting_extra_config_is_not_treated_as_a_rule_change(self):
        # 飞书里存的可能带空格或另一种键序；纯格式差异触发降级会把 active 源无故停掉
        current = {"extraConfig": '{"list_parser": "anthropic_news", "max_articles": 20}'}
        for same in (
            '{"list_parser":"anthropic_news","max_articles":20}',
            '{"max_articles": 20, "list_parser": "anthropic_news"}',
        ):
            self.assertEqual(
                source_view.normalize_config_patch({"extraConfig": same}, current),
                ({}, []),
            )
        _, changed = source_view.normalize_config_patch(
            {"extraConfig": '{"list_parser":"anthropic_news","max_articles":8}'}, current
        )
        self.assertEqual(changed, ["extraConfig"])

    def test_extra_config_is_stored_as_compact_single_line_json(self):
        fields, _ = source_view.normalize_config_patch(
            {"extraConfig": '{\n  "max_articles": 8\n}'}, {}
        )
        self.assertEqual(fields["extra_config"], '{"max_articles":8}')

    def test_link_behaviour_fields_are_marked_as_rules(self):
        # 决定「改完要不要重新验收」的清单，漏一个就会让改坏的源继续跑在正式流水线上
        self.assertEqual(
            source_view.RULE_FIELDS,
            {
                "endpoint",
                "fetchMethod",
                "format",
                "lookbackWindow",
                "keywordRegex",
                "minContentChars",
                "dedupKey",
                "extraConfig",
            },
        )
        for cosmetic in ("name", "notes", "tier", "dimension"):
            self.assertNotIn(cosmetic, source_view.RULE_FIELDS)


class SourcesApiTest(unittest.TestCase):
    def test_patch_translates_labels_to_feishu_codes(self):
        self.assertEqual(
            sources_api._patch_fields({"priority": "高"})[0], {"priority": "P0"}
        )
        self.assertEqual(
            sources_api._patch_fields({"status": "experimental"})[0],
            {"status": "experimental"},
        )

    def test_patch_rejects_unknown_priority(self):
        with self.assertRaises(sources_api.ApiError):
            sources_api._patch_fields({"priority": "紧急"})

    def test_patch_accepts_chinese_status_labels(self):
        self.assertEqual(
            sources_api._patch_fields({"status": "已暂停"})[0], {"status": "paused"}
        )

    def test_patch_rejects_unknown_status_instead_of_pausing(self):
        # normalize_status 兜底成 paused；写接口若沿用会把打错的值静默变成停用
        for bad in ("", "enabled", "开"):
            with self.assertRaises(sources_api.ApiError):
                sources_api._patch_fields({"status": bad})

    def test_status_sync_writes_matching_source_table_row(self):
        calls = []
        rows = [
            {"record_id": "s1", "fields": {"名称": "别的源", "自动化状态": "已接入"}},
            {"record_id": "s2", "fields": {"名称": "Demo", "自动化状态": "已接入"}},
        ]
        with _patched(rows, calls):
            sources_api.sync_source_table_status("tok", "Demo", "paused")
        self.assertEqual(calls, [("s2", {"自动化状态": "已暂停"})])

    def test_status_sync_skips_when_already_aligned(self):
        calls = []
        rows = [{"record_id": "s2", "fields": {"名称": "Demo", "自动化状态": "已暂停"}}]
        with _patched(rows, calls):
            sources_api.sync_source_table_status("tok", "Demo", "paused")
        self.assertEqual(calls, [])

    def test_status_sync_swallows_feishu_errors(self):
        # 信号源表只是人工清单，同步失败不该让开关操作整体失败
        def boom(_token, _table):
            raise sources_api.feishu.FeishuError("boom")

        calls = []
        with _patched([], calls):
            sources_api.feishu.read_all_records_with_ids = boom
            sources_api.sync_source_table_status("tok", "Demo", "paused")
        self.assertEqual(calls, [])

    def test_patch_rejects_body_without_known_fields(self):
        for body in ({}, {"nope": 1}):
            with self.assertRaises(sources_api.ApiError):
                sources_api._patch_fields(body)

    def test_patch_skips_fields_equal_to_current_value(self):
        # 与当前值相同的提交不该产生飞书写入，否则每次保存都会误触发状态降级
        current = {"name": "Demo", "lookbackWindow": "24h"}
        with self.assertRaises(sources_api.ApiError):
            sources_api._patch_fields({"name": "Demo", "lookbackWindow": "24h"}, current)
        fields, changed = sources_api._patch_fields({"lookbackWindow": "7d"}, current)
        self.assertEqual(fields, {"lookback_window": "7d"})
        self.assertEqual(changed, ["lookbackWindow"])

    def test_patch_surfaces_validation_errors_as_api_errors(self):
        for body in (
            {"keywordRegex": "(unclosed"},
            {"lookbackWindow": "三天"},
            {"extraConfig": "{not json}"},
            {"minContentChars": "abc"},
            {"endpoint": "ftp://example.com"},
            {"fetchMethod": "Telepathy"},
        ):
            with self.assertRaises(sources_api.ApiError):
                sources_api._patch_fields(body, {})

    def test_slugify_avoids_collisions(self):
        taken = {"machine-heart", "machine-heart-2"}
        self.assertEqual(sources_api.slugify_source_id("机器之心 AI", taken), "ai")
        self.assertEqual(
            sources_api.slugify_source_id("Machine Heart", taken), "machine-heart-3"
        )


@contextlib.contextmanager
def _patched_param_table(rec, updates, seed_runs):
    """替掉参数表读写与 seed 导出，让 apply_patch 可以离线跑。"""
    feishu = sources_api.feishu
    originals = (
        feishu.get_tenant_access_token,
        feishu.read_param_records,
        feishu.update_record,
        sources_api.export_seed_snapshot,
        sources_api.sync_source_table_status,
    )
    feishu.get_tenant_access_token = lambda: "tok"
    feishu.read_param_records = lambda _token: [rec]
    feishu.update_record = lambda _t, _table, rid, fields: (
        updates.append((rid, fields)),
        rec["fields"].update(fields),
        rec,
    )[-1]
    sources_api.export_seed_snapshot = lambda: (
        seed_runs.append(1),
        {"ok": True, "message": "stub"},
    )[-1]
    sources_api.sync_source_table_status = lambda *_a, **_kw: None
    try:
        yield
    finally:
        (
            feishu.get_tenant_access_token,
            feishu.read_param_records,
            feishu.update_record,
            sources_api.export_seed_snapshot,
            sources_api.sync_source_table_status,
        ) = originals


class ApplyPatchTest(unittest.TestCase):
    def test_changing_a_rule_field_demotes_the_source_and_refreshes_the_seed(self):
        rec = record("r1", status="active")
        updates, seeds = [], []
        with _patched_param_table(rec, updates, seeds):
            result = sources_api.apply_patch("r1", {"lookbackWindow": "7d"})
        _, fields = updates[0]
        self.assertEqual(fields["lookback_window"], "7d")
        self.assertEqual(fields["status"], "experimental")
        self.assertTrue(result["demoted"])
        self.assertEqual(result["ruleChanged"], ["lookbackWindow"])
        self.assertEqual(len(seeds), 1)

    def test_cosmetic_edits_neither_demote_nor_export(self):
        rec = record("r1", status="active")
        updates, seeds = [], []
        with _patched_param_table(rec, updates, seeds):
            result = sources_api.apply_patch("r1", {"notes": "只是备注"})
        _, fields = updates[0]
        self.assertNotIn("status", fields)
        self.assertFalse(result["demoted"])
        self.assertNotIn("seed", result)
        self.assertEqual(seeds, [])

    def test_explicit_status_wins_over_auto_demotion(self):
        # 手动点「已接入」验收通过时，不能被自动降级顶掉
        rec = record("r1", status="experimental")
        updates, seeds = [], []
        with _patched_param_table(rec, updates, seeds):
            result = sources_api.apply_patch(
                "r1", {"lookbackWindow": "7d", "status": "active"}
            )
        _, fields = updates[0]
        self.assertEqual(fields["status"], "active")
        self.assertFalse(result["demoted"])

    def test_already_experimental_source_is_not_re_demoted(self):
        rec = record("r1", status="experimental")
        updates, seeds = [], []
        with _patched_param_table(rec, updates, seeds):
            result = sources_api.apply_patch("r1", {"lookbackWindow": "7d"})
        _, fields = updates[0]
        self.assertNotIn("status", fields)
        self.assertFalse(result["demoted"])
        self.assertEqual(len(seeds), 1)

    def test_rejected_values_never_reach_feishu(self):
        rec = record("r1", status="active")
        updates, seeds = [], []
        with _patched_param_table(rec, updates, seeds):
            with self.assertRaises(sources_api.ApiError):
                sources_api.apply_patch("r1", {"keywordRegex": "(unclosed"})
        self.assertEqual(updates, [])
        self.assertEqual(seeds, [])

    def test_missing_record_is_reported_before_any_write(self):
        rec = record("other", status="active")
        updates, seeds = [], []
        with _patched_param_table(rec, updates, seeds):
            with self.assertRaises(sources_api.ApiError):
                sources_api.apply_patch("missing", {"lookbackWindow": "7d"})
        self.assertEqual(updates, [])


if __name__ == "__main__":
    unittest.main()
