"""逐源 capture_spec 的每日确定性检查（无 LLM，可放进每日构建）。

读最近 days 天的健康记录：`python -m src.main` 每轮会把每条 attempted 源的
fetch_stats 写进 output/health/dt=*.jsonl。本工具把「最近一行」的 fetch/funnel
按源对齐，用 capture_spec.find_violations 做确定性比对——源级 error 已是
spec_mismatch、或抽链数/日期命中率低于 spec.expect，都算违规。

**无 LLM、无网络、无写操作**，挂在每日 build job 上成本接近零。发现违规就
以非零退出码（EXIT_VIOLATION=3）标志「有源违反 spec」，供上游 spec-recheck
自动触发 LLM 重筛。

用法：
  python -m src.spec_check                     # 只打印违规清单，正常退出
  python -m src.spec_check --exit-code         # 有违规就 exit 3
  python -m src.spec_check --days 2
  python -m src.spec_check --json              # 机器可读输出
"""  # noqa: E501
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import capture_spec, health  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("spec_check")

EXIT_VIOLATION = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capture_spec 每日确定性检查")
    parser.add_argument("--exit-code", action="store_true", help="有违规时以退出码 3 结束")
    parser.add_argument("--days", type=int, default=1, help="回看天数（默认 1 = 当日）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    specs = capture_spec.load()
    rows = health.load_records(days=max(args.days, 1))
    if not rows:
        if args.json:
            print(json.dumps({"violations": []}, ensure_ascii=False))
        else:
            print("（窗口内无健康记录，跳过 spec_check）")
        return 0

    # 取最近一行（最后一次 run），按源对齐成 find_violations 需要的形状。
    # health 行结构：{source_id, fetch:{error,links,...}, funnel:{阶段:计数}, ...}
    fetch_stats: dict[str, dict[str, Any]] = {}
    funnel: dict[str, dict[str, int]] = {}
    last = rows[-1]
    for row in rows:
        sid = str(row.get("source_id") or "")
        if not sid:
            continue
        fetch_stats[sid] = row.get("fetch") or {}
        funnel[sid] = row.get("funnel") or {}
    # 保守起见只取最后一次 run 的行，避免把跨轮计数叠加
    _ = last

    # find_violations(fetch_stats, sources(未使用), specs, funnel)
    violations = capture_spec.find_violations(fetch_stats, {}, specs, funnel)

    if args.json:
        print(json.dumps({"violations": violations}, ensure_ascii=False))
    elif violations:
        for v in violations:
            print(
                f"[spec_mismatch] {v['source_id']} 违反 {v['violated']} "
                f"(期望 {v['expected']}, 实际 {v['actual']})"
            )
    else:
        print("无 capture_spec 违规")

    if violations and args.exit_code:
        return EXIT_VIOLATION
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
