"""config_store 读适配器，以及对真实配置文件本身的校验。

配置进了仓库才可能有这后半部分：以前配置活在飞书里，坏正则、认不出的时间窗、
非法 JSON 只能等采集时被静默兜底成默认值，源看着正常但规则已经失效。现在这些
都是能在 CI 里拦住的静态错误。
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from src import config_store, source_view, sources, typed_config


def _write(bundle: dict) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "seed.json"
    tmp.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return tmp


class ReadParamRecordsTest(unittest.TestCase):
    def test_shape_matches_feishu_read_param_records(self):
        path = _write({"一级参数": [{"source_id": "a", "name": "A", "status": "active"}]})
        records = config_store.read_param_records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {"record_id", "fields"})
        self.assertEqual(records[0]["fields"]["name"], "A")

    def test_source_id_doubles_as_record_id(self):
        path = _write({"一级参数": [{"source_id": "openai-news"}]})
        self.assertEqual(config_store.read_param_records(path)[0]["record_id"], "openai-news")

    def test_rows_without_source_id_are_skipped(self):
        path = _write({"一级参数": [{"name": "无主"}, {"source_id": "ok"}]})
        records = config_store.read_param_records(path)
        self.assertEqual([r["record_id"] for r in records], ["ok"])

    def test_duplicate_source_id_keeps_the_first(self):
        path = _write({"一级参数": [{"source_id": "x", "name": "先"}, {"source_id": "x", "name": "后"}]})
        records = config_store.read_param_records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["fields"]["name"], "先")

    def test_fields_are_copies_so_callers_cannot_corrupt_the_store(self):
        path = _write({"一级参数": [{"source_id": "x", "name": "原"}]})
        config_store.read_param_records(path)[0]["fields"]["name"] = "改了"
        self.assertEqual(config_store.read_param_records(path)[0]["fields"]["name"], "原")

    def test_missing_file_raises_instead_of_returning_empty(self):
        # 静默返回空会让流水线以为「一个源都没配」，跑完 0 条还显示成功
        with self.assertRaises(config_store.ConfigStoreError):
            config_store.read_param_records(Path("/nonexistent/seed.json"))

    def test_broken_json_raises(self):
        tmp = Path(tempfile.mkdtemp()) / "seed.json"
        tmp.write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(config_store.ConfigStoreError):
            config_store.read_param_records(tmp)


class TypedConfigTest(unittest.TestCase):
    def test_matches_build_typed_configs_on_equivalent_rows(self):
        rows = [{"source_id": "arxiv-cs-ai", "必含关键词": "llm, agent", "最低质量分": 60}]
        path = _write({"二级参数-论文": rows})
        self.assertEqual(
            config_store.load_typed_configs(path),
            typed_config.build_typed_configs([("paper", rows)]),
        )

    def test_missing_tables_are_tolerated(self):
        path = _write({"一级参数": [{"source_id": "a"}]})
        self.assertEqual(config_store.load_typed_configs(path), {})


class SeedFileIntegrityTest(unittest.TestCase):
    """直接校验仓库里那份真实配置。坏配置应该在这里挂，而不是采集时静默失效。"""

    @classmethod
    def setUpClass(cls):
        cls.records = config_store.read_param_records()
        cls.rows = [r["fields"] for r in cls.records]
        cls.typed = config_store.load_typed_configs()

    def test_source_ids_are_unique_and_nonempty(self):
        ids = [str(row.get("source_id") or "") for row in self.rows]
        self.assertNotIn("", ids)
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(dupes, set(), f"source_id 重复：{sorted(dupes)}")

    def test_status_values_are_known(self):
        bad = {
            row["source_id"]: row.get("status")
            for row in self.rows
            if str(row.get("status") or "active") not in source_view.STATUS_ORDER
        }
        self.assertEqual(bad, {}, f"未知 status：{bad}")

    def test_fetch_methods_are_known(self):
        bad = {
            row["source_id"]: row.get("fetch_method")
            for row in self.rows
            if row.get("fetch_method")
            and str(sources.cell(row["fetch_method"])) not in source_view.FETCH_METHODS
        }
        self.assertEqual(bad, {}, f"未知 fetch_method：{bad}")

    def test_lookback_windows_are_explicitly_recognized(self):
        """认不出的写法会被 parse_lookback_hours 静默回落成 168h。

        比对的是解析器真正认识的形式，不是写入侧那条更严的正则——存量数据里
        「每周」「每日」是 parse_lookback_hours 显式支持的。注意「每天」不在其中，
        会静默变成 168h，正是这个测试要拦的东西。
        """
        bad = {}
        for row in self.rows:
            raw = row.get("lookback_window")
            if not raw:
                continue
            text = str(sources.cell(raw)).strip().lower()
            recognized = (
                re.search(r"\d+(?:\.\d+)?\s*[hd]", text)
                or "每日" in text
                or "每周" in text
            )
            if not recognized:
                bad[row["source_id"]] = raw
        self.assertEqual(bad, {}, f"认不出的时间窗写法（会静默变 168h）：{bad}")

    def test_keyword_regexes_compile(self):
        bad = {}
        for row in self.rows:
            raw = row.get("keyword_regex")
            if not raw:
                continue
            try:
                re.compile(str(sources.cell(raw)))
            except re.error as exc:
                bad[row["source_id"]] = f"{raw} -> {exc}"
        self.assertEqual(bad, {}, f"编译不了的正则：{bad}")

    def test_extra_config_is_a_json_object(self):
        bad = {}
        for row in self.rows:
            raw = row.get("extra_config")
            if not raw:
                continue
            try:
                parsed = json.loads(str(sources.cell(raw)))
            except ValueError as exc:
                bad[row["source_id"]] = f"非法 JSON：{exc}"
                continue
            if not isinstance(parsed, dict):
                bad[row["source_id"]] = f"应是对象，实际是 {type(parsed).__name__}"
        self.assertEqual(bad, {}, f"extra_config 有问题：{bad}")

    def test_every_active_source_is_picked_up_by_some_mapper(self):
        """active 却没有任何 mapper 认领 = 配了等于没配，最难发现的一类坏配置。

        两类例外是设计如此，不算孤儿：Manual/API 本就没有自动采集路径；
        B 类榜单快照被 map_scrape_sources 有意排除，只走 diag_scrape。
        """
        claimed = set()
        for mapper in (
            sources.map_feed_sources,
            sources.map_media_sources,
            sources.map_podcast_sources,
            sources.map_social_sources,
            sources.map_scrape_sources,
        ):
            claimed |= {feed["id"] for feed in mapper(self.records)}
        orphans = sorted(
            row["source_id"]
            for row in self.rows
            if str(row.get("status") or "active") == source_view.STATUS_ACTIVE
            and row["source_id"] not in claimed
            and str(sources.cell(row.get("fetch_method")) or "") not in {"Manual", "API"}
            and not sources._is_b_class(row)
        )
        self.assertEqual(orphans, [], f"active 但没有 mapper 认领：{orphans}")

    def test_typed_config_rows_reference_existing_sources(self):
        known = {row["source_id"] for row in self.rows}
        orphans = sorted(set(self.typed) - known)
        self.assertEqual(orphans, [], f"二级参数指向不存在的源：{orphans}")


if __name__ == "__main__":
    unittest.main()
