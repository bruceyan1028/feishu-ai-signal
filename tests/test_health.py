import datetime
import json
import tempfile
import unittest
from pathlib import Path

from src import health, process


def _entry(source_id, title, body, *, hours_ago=2, published=True, **feed_extra):
    stamp = datetime.datetime.utcfromtimestamp(
        (health.now_ms() - hours_ago * 3600000) / 1000
    ).strftime("%a, %d %b %Y %H:%M:%S GMT")
    feed = {
        "id": source_id,
        "name": source_id,
        "url": f"https://{source_id}.example.com",
        "fetch_method": "RSS",
        "lookback_hours": 24,
        "keyword_regex": "(gpt|claude|model)",
        "min_content_chars": 50,
        "source_type": "纯网页",
    }
    feed.update(feed_extra)
    return {
        "title": title,
        "url": f"https://{source_id}.example.com/{title.replace(' ', '-')}",
        "body": body,
        "published_raw": stamp if published else None,
        "feed": feed,
    }


ON_TOPIC = "A new model release from the lab, claude and gpt scale up together. " * 4
OFF_TOPIC = "Quarterly revenue grew across every business segment this period. " * 4


class FunnelAttributionTest(unittest.TestCase):
    """淘汰原因必须归属到源。

    原先漏斗只按原因聚合，「本轮 keyword_regex 淘汰 40 条」看得见，
    「是哪个源被自己的正则卡死」看不见——这正是要解决的问题。
    """

    def setUp(self):
        self.funnel = health.Funnel()
        self.raw = [
            _entry("good", "New model launch A", ON_TOPIC),
            _entry("good", "New model launch B", ON_TOPIC),
            _entry("stale", "Old model news", ON_TOPIC, hours_ago=400),
            _entry("offtopic", "Earnings report", OFF_TOPIC),
            _entry("nodate", "Undated model post", ON_TOPIC, published=False),
        ]
        self.cleaned = process.process_and_clean(self.raw, {}, {}, self.funnel)

    def test_each_source_gets_its_own_blocking_stage(self):
        self.assertEqual(self.funnel.for_source("good"), {"raw": 2, "kept": 2})
        self.assertEqual(self.funnel.for_source("stale"), {"raw": 1, "lookback": 1})
        self.assertEqual(self.funnel.for_source("offtopic"), {"raw": 1, "keyword_regex": 1})
        self.assertEqual(
            self.funnel.for_source("nodate"), {"raw": 1, "missing_or_invalid_date": 1}
        )

    def test_totals_still_add_up_across_sources(self):
        self.assertEqual(self.funnel.totals["raw"], len(self.raw))
        self.assertEqual(self.funnel.totals["kept"], len(self.cleaned))
        self.assertEqual(
            self.funnel.drops(),
            {"lookback": 1, "keyword_regex": 1, "missing_or_invalid_date": 1},
        )

    def test_omitting_the_funnel_keeps_the_old_behaviour(self):
        without = process.process_and_clean(self.raw, {})
        self.assertEqual(
            [i["url"] for i in without], [i["url"] for i in self.cleaned]
        )

    def test_every_declared_stage_exists_in_process(self):
        # health 的阶段名与漏斗的淘汰点必须对齐，否则报告会漏掉一整类淘汰。
        # 论文分支的淘汰点（min_signal_score）在 paper_enrich.evaluate_paper 里。
        source = "\n".join(
            Path(p).read_text(encoding="utf-8")
            for p in ("src/process.py", "src/paper_enrich.py")
        )
        for stage in health.FUNNEL_STAGES:
            if stage in {"raw", "typed_filter"}:
                continue
            self.assertIn(f'"{stage}"', source, f"漏斗里找不到淘汰点 {stage}")


class BuildRecordsTest(unittest.TestCase):
    def _rows(self, funnel, attempted, **kw):
        params = [
            {
                "record_id": "r1",
                "fields": {
                    "source_id": sid,
                    "name": sid.upper(),
                    "status": "active",
                    "tier": "L1",
                    "priority": "P0",
                    "fetch_method": "RSS",
                },
            }
            for sid in attempted
        ]
        return health.build_records(
            run_id="T1",
            param_records=params,
            attempted={sid: {"id": sid} for sid in attempted},
            funnel=funnel,
            **kw,
        )

    def test_a_source_that_fetched_nothing_still_produces_a_row(self):
        # 彻底静默的源如果不出行，在数据里根本不存在，也就永远不会被发现
        rows = self._rows(
            health.Funnel(),
            ["silent"],
            fetch_stats={"silent": {"error": "unparseable_or_empty"}},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["written"], 0)
        self.assertEqual(rows[0]["fetch"]["error"], "unparseable_or_empty")

    def test_blocked_at_names_the_stage_that_ate_everything(self):
        funnel = health.Funnel()
        for _ in range(5):
            funnel.bump("src", "raw")
        funnel.bump("src", "keyword_regex", 4)
        funnel.bump("src", "lookback")
        rows = self._rows(funnel, ["src"])
        self.assertEqual(rows[0]["blocked_at"], "keyword_regex")

    def test_blocked_at_is_empty_when_something_got_through(self):
        funnel = health.Funnel()
        funnel.bump("src", "raw", 3)
        funnel.bump("src", "keyword_regex", 2)
        funnel.bump("src", "kept")
        rows = self._rows(funnel, ["src"])
        self.assertEqual(rows[0]["blocked_at"], "")

    def test_dedup_loss_separates_clean_from_written(self):
        funnel = health.Funnel()
        funnel.bump("src", "raw", 9)
        cleaned = [{"source_id": "src"} for _ in range(7)]
        final = [{"source_id": "src"}]
        rows = self._rows(funnel, ["src"], cleaned_items=cleaned, final_items=final)
        self.assertEqual(rows[0]["cleaned"], 7)
        self.assertEqual(rows[0]["written"], 1)
        self.assertEqual(rows[0]["dedup_dropped"], 6)


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        today = datetime.datetime.now(health.CN_TZ)
        for back in range(8, 0, -1):
            dt = (today - datetime.timedelta(days=back - 1)).strftime("%Y-%m-%d")
            rows = [
                self._row(dt, "good", raw=6, written=3),
                # 5 天前开始断流
                self._row(
                    dt,
                    "dying",
                    raw=4,
                    written=0 if back <= 5 else 2,
                    blocked="lookback" if back <= 5 else "",
                ),
                self._row(dt, "offtopic", raw=8, written=0, blocked="keyword_regex"),
                self._row(dt, "dead", raw=0, written=0, error="unparseable_or_empty"),
            ]
            (self.dir / f"dt={dt}.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )

    def _row(self, dt, sid, *, raw, written, blocked="", error=None):
        return {
            "run_id": dt,
            "ts_ms": health.now_ms(),
            "dt": dt,
            "source_id": sid,
            "name": sid,
            "fetch_method": "RSS",
            "status": "active",
            "tier": "L1",
            "priority": "P0",
            "fetch": {"error": error},
            "funnel": {"raw": raw, "kept": written},
            "cleaned": written,
            "written": written,
            "dedup_dropped": 0,
            "blocked_at": blocked,
        }

    def test_dry_days_is_measurable_only_because_history_is_kept(self):
        summary = {a["source_id"]: a for a in health.summarize(health.load_records(days=14, base_dir=self.dir))}
        self.assertEqual(summary["good"]["dry_days"], 0)
        self.assertEqual(summary["dying"]["dry_days"], 5)
        # 观测窗口内从未入库：不假装知道具体断了多久
        self.assertIsNone(summary["offtopic"]["dry_days"])
        self.assertIsNone(summary["dead"]["dry_days"])

    def test_rule_problems_and_link_problems_are_distinguishable(self):
        summary = {a["source_id"]: a for a in health.summarize(health.load_records(days=14, base_dir=self.dir))}
        # 抓到很多却一条不留 = 规则问题
        self.assertEqual(summary["offtopic"]["top_block"], "keyword_regex")
        self.assertGreater(summary["offtopic"]["raw"], 0)
        self.assertEqual(summary["offtopic"]["top_fetch_error"], "")
        # 一条都没抓到 = 链路问题
        self.assertEqual(summary["dead"]["top_fetch_error"], "unparseable_or_empty")
        self.assertEqual(summary["dead"]["raw"], 0)

    def test_worst_sources_sort_first(self):
        order = [a["source_id"] for a in health.summarize(health.load_records(days=14, base_dir=self.dir))]
        self.assertEqual(set(order[:2]), {"offtopic", "dead"})
        self.assertEqual(order[-1], "good")

    def test_load_skips_corrupt_lines_instead_of_failing(self):
        path = next(self.dir.glob("dt=*.jsonl"))
        path.write_text(path.read_text(encoding="utf-8") + "{not json}\n", encoding="utf-8")
        self.assertTrue(health.load_records(days=14, base_dir=self.dir))

    def test_report_runs_on_an_empty_directory(self):
        self.assertEqual(health.report(days=7, base_dir=Path(tempfile.mkdtemp())), 0)


class WriteRecordsTest(unittest.TestCase):
    def test_records_append_into_one_file_per_day(self):
        directory = Path(tempfile.mkdtemp())
        row = {"dt": "2026-08-27", "source_id": "a", "written": 1}
        health.write_records([row], base_dir=directory)
        health.write_records([dict(row, source_id="b")], base_dir=directory)
        files = list(directory.glob("dt=*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual(len(files[0].read_text(encoding="utf-8").strip().splitlines()), 2)

    def test_empty_batch_writes_nothing(self):
        directory = Path(tempfile.mkdtemp())
        self.assertIsNone(health.write_records([], base_dir=directory))
        self.assertEqual(list(directory.glob("*")), [])


if __name__ == "__main__":
    unittest.main()
