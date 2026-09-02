"""capture_spec 自动重筛编排（spec-recheck workflow 的入口）。

流程（每天一次，晚于 daily-brief 构建）：

  1. spec_check 找出违约源（确定性、无 LLM）。没有 → 立即退出，零成本。
  2. 对每个违约源：
     - 熔断判定：该源连续自动重筛失败次数 >= MAX_AUTO_RETRIES(2) → 不再调 LLM，
       只降级 experimental，留给管理员手动 `python -m src.probe --source X --apply`。
     - 否则调 `src.probe --apply` 让 LLM 重生成 spec，并在真实页面验收。
  3. 无论重筛成败，违约源一律降为 experimental：spec 违约意味着这条源的抓取链路
     已不可信，新 spec 也要人确认能抓到再手动改回 active。这与 sources_api 的
     「改了采集规则就退回待测」是同一约定。
  4. 熔断计数存 src/capture_specs.json 每源的 `_auto` 字段（随 spec 一起被 commit）：
     重筛验收通过 → 清零；失败 → +1。

状态回写飞书需要 FEISHU_* 凭据；--no-demote 可只跑重筛不动飞书（本地调试用）。

用法：
  python -m src.spec_recheck                      # 按健康记录自动找违约源
  python -m src.spec_recheck --source anthropic-news   # 强制重筛指定源
  python -m src.spec_recheck --dry-run            # 只打印会做什么
"""  # noqa: E501
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import capture_spec, health  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("spec_recheck")

MAX_AUTO_RETRIES = 2
CN_TZ = timezone(timedelta(hours=8))


def find_violated_sources(days: int = 1) -> list[dict[str, Any]]:
    """复用 spec_check 的判定：最近一次 run 的健康记录 vs 当前 spec。"""
    specs = capture_spec.load()
    rows = health.load_records(days=max(days, 1))
    fetch_stats: dict[str, dict[str, Any]] = {}
    funnel: dict[str, dict[str, int]] = {}
    for row in rows:
        sid = str(row.get("source_id") or "")
        if sid:
            fetch_stats[sid] = row.get("fetch") or {}
            funnel[sid] = row.get("funnel") or {}
    return capture_spec.find_violations(fetch_stats, {}, specs, funnel)


def _auto_state(spec: dict[str, Any]) -> dict[str, Any]:
    state = spec.get("_auto")
    return dict(state) if isinstance(state, dict) else {"failures": 0, "last_recheck": "", "tripped": False}


def run_probe(source_id: str) -> bool:
    """调 src.probe --apply；返回是否验收通过（退出码 0）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "src.probe", "--source", source_id, "--apply"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    tail = (proc.stdout or "")[-3000:]
    if tail:
        log.info("probe %s 输出：\n%s", source_id, tail)
    if proc.returncode != 0 and proc.stderr:
        log.warning("probe %s stderr：\n%s", source_id, proc.stderr[-2000:])
    return proc.returncode == 0


def recheck(
    source_ids: list[str],
    *,
    demote: bool = True,
    dry_run: bool = False,
    spec_path: Path | str = capture_spec.SPEC_FILE,
) -> dict[str, Any]:
    """对一组违约源执行：熔断判定 → 重筛 → 记状态 → 降级。返回汇总。"""
    summary: dict[str, Any] = {"rechecked": [], "passed": [], "failed": [], "tripped": [], "demoted": []}
    if not source_ids:
        return summary

    specs = capture_spec.load(spec_path)
    now = datetime.now(CN_TZ).isoformat(timespec="seconds")

    for sid in source_ids:
        spec = specs.get(sid) or capture_spec.normalize_spec({})
        state = _auto_state(spec)
        if state.get("failures", 0) >= MAX_AUTO_RETRIES:
            log.warning("源 %s 已连续自动重筛失败 %d 次，熔断：不再调 LLM，只降级交人工", sid, state["failures"])
            state["tripped"] = True
            summary["tripped"].append(sid)
        else:
            summary["rechecked"].append(sid)
            if dry_run:
                log.info("[dry-run] 将对 %s 跑 src.probe --apply", sid)
                ok = False
            else:
                ok = run_probe(sid)
                # probe --apply 可能已改写 specs 文件，重读再合并状态
                specs = capture_spec.load(spec_path)
                spec = specs.get(sid) or capture_spec.normalize_spec({})
            if ok:
                state = {"failures": 0, "last_recheck": now, "tripped": False}
                summary["passed"].append(sid)
            else:
                state = {
                    "failures": int(state.get("failures", 0)) + 1,
                    "last_recheck": now,
                    "tripped": False,
                }
                summary["failed"].append(sid)
        spec["_auto"] = state
        specs[sid] = spec

    if not dry_run:
        capture_spec.save(specs, spec_path)

    if demote and not dry_run:
        from src import config, feishu

        config.validate()
        token = feishu.get_tenant_access_token()
        records = feishu.read_param_records(token)
        note = f"[{now[:10]}] capture_spec 违约，自动降为待测；重筛后请确认能抓到再改回 active"
        summary["demoted"] = feishu.demote_sources_to_experimental(token, records, source_ids, note=note)
    elif demote:
        log.info("[dry-run] 将把这些源降为 experimental：%s", source_ids)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capture_spec 自动重筛编排")
    parser.add_argument("--source", action="append", help="强制重筛指定源（可重复）")
    parser.add_argument("--days", type=int, default=1, help="健康记录回看天数")
    parser.add_argument("--no-demote", action="store_true", help="不回写飞书 status（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印会做什么")
    args = parser.parse_args(argv)

    if args.source:
        source_ids = [s for s in args.source if s]
    else:
        violations = find_violated_sources(days=args.days)
        source_ids = [v["source_id"] for v in violations]
        if not source_ids:
            print("无 capture_spec 违约源，跳过重筛")
            return 0
        for v in violations:
            log.info("违约：%s %s 期望 %s 实际 %s", v["source_id"], v["violated"], v["expected"], v["actual"])

    summary = recheck(source_ids, demote=not args.no_demote, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
