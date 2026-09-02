"""spec_recheck 熔断 / 状态累计 / 降级接线 单测（不调 LLM、不连飞书）。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import capture_spec as cs
from src import feishu, spec_recheck


def _spec() -> dict:
    return {
        "version": 1,
        "enabled": True,
        "route": {"list": {"selector": ".card a"}},
        "expect": {"min_links": 5},
    }


class SpecRecheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "capture_specs.json"
        cs.save({"src1": _spec()}, self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _auto(self):
        return cs.load(self.path)["src1"]["_auto"]

    def test_failure_increments_and_trips_after_max(self):
        with mock.patch.object(spec_recheck, "run_probe", return_value=False) as probe:
            s1 = spec_recheck.recheck(["src1"], demote=False, spec_path=self.path)
            self.assertEqual(s1["failed"], ["src1"])
            self.assertEqual(self._auto()["failures"], 1)

            s2 = spec_recheck.recheck(["src1"], demote=False, spec_path=self.path)
            self.assertEqual(s2["failed"], ["src1"])
            self.assertEqual(self._auto()["failures"], 2)
            self.assertEqual(probe.call_count, 2)

            # 第三次：达到 MAX_AUTO_RETRIES，熔断——不再调 probe，只标记交人工
            s3 = spec_recheck.recheck(["src1"], demote=False, spec_path=self.path)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(s3["tripped"], ["src1"])
            self.assertEqual(s3["rechecked"], [])
            self.assertTrue(self._auto()["tripped"])

    def test_success_resets_failures(self):
        specs = cs.load(self.path)
        specs["src1"]["_auto"] = {"failures": 1, "last_recheck": "", "tripped": False}
        cs.save(specs, self.path)
        with mock.patch.object(spec_recheck, "run_probe", return_value=True):
            s = spec_recheck.recheck(["src1"], demote=False, spec_path=self.path)
        self.assertEqual(s["passed"], ["src1"])
        self.assertEqual(self._auto()["failures"], 0)

    def test_auto_state_survives_normalize(self):
        specs = cs.load(self.path)
        specs["src1"]["_auto"] = {"failures": 2, "last_recheck": "x", "tripped": True}
        cs.save(specs, self.path)
        self.assertEqual(cs.load(self.path)["src1"]["_auto"]["failures"], 2)

    def test_demote_called_for_every_violated_source_even_if_probe_passed(self):
        with mock.patch.object(spec_recheck, "run_probe", return_value=True), \
             mock.patch("src.config.validate"), \
             mock.patch.object(feishu, "get_tenant_access_token", return_value="t"), \
             mock.patch.object(feishu, "read_param_records", return_value=[]), \
             mock.patch.object(feishu, "demote_sources_to_experimental", return_value=["src1"]) as demote:
            s = spec_recheck.recheck(["src1"], demote=True, spec_path=self.path)
        demote.assert_called_once()
        self.assertEqual(demote.call_args.args[2], ["src1"])
        self.assertEqual(s["demoted"], ["src1"])

    def test_dry_run_touches_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        with mock.patch.object(spec_recheck, "run_probe") as probe, \
             mock.patch.object(feishu, "demote_sources_to_experimental") as demote:
            spec_recheck.recheck(["src1"], demote=True, dry_run=True, spec_path=self.path)
        probe.assert_not_called()
        demote.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


class DemoteTest(unittest.TestCase):
    def _records(self):
        return [
            {"record_id": "r1", "fields": {"source_id": "a", "status": "active", "notes": ""}},
            {"record_id": "r2", "fields": {"source_id": "b", "status": "experimental"}},
            {"record_id": "r3", "fields": {"source_id": "c", "status": "active", "notes": "old"}},
        ]

    def test_only_active_sources_demoted_and_note_appended(self):
        with mock.patch.object(feishu, "update_record") as upd:
            out = feishu.demote_sources_to_experimental(
                "t", self._records(), ["a", "b", "c", "zzz"], note="N"
            )
        self.assertEqual(sorted(out), ["a", "c"])
        self.assertEqual(upd.call_count, 2)
        calls = {c.args[2]: c.args[3] for c in upd.call_args_list}
        self.assertEqual(calls["r1"], {"status": "experimental", "notes": "N"})
        self.assertEqual(calls["r3"], {"status": "experimental", "notes": "old\nN"})

    def test_no_note_leaves_notes_untouched(self):
        with mock.patch.object(feishu, "update_record") as upd:
            feishu.demote_sources_to_experimental("t", self._records(), ["a"])
        self.assertEqual(upd.call_args.args[3], {"status": "experimental"})


if __name__ == "__main__":
    unittest.main()
