"""每轮采集的分源健康记录：谁抓到了、谁在哪一步被淘汰、谁连续多久没产出。

参数表回写的四个字段（最近采集时间 / 条目数 / 查重过滤 / 时间窗过滤）是覆盖式
快照，答不了两类最常问的问题：

- 这个源连续几天零产出了？
- 上周还好好的，是哪天开始死的、当时改了什么？

清洗漏斗的淘汰原因原先只按原因聚合、不按源归属，所以「本轮 keyword_regex 淘汰
40 条」看得见，「是哪个源被自己的正则卡死」看不见。这里按轮留存分源漏斗。

落盘与去处解耦：write_records 先写本地按天分片的 JSONL（output/ 不入 git），要换
成对象存储或数据库时只改这一个函数。

    python -m src.health              # 体检报告：零产出与断流排行
    python -m src.health --days 30
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
HEALTH_DIR = ROOT / "output" / "health"

# 与 process.process_and_clean 里的淘汰点一一对应，顺序即漏斗顺序。
# 改那边的原因名必须同步改这里，否则报告会漏掉一整类淘汰。
FUNNEL_STAGES = (
    "raw",
    "per_feed_cap",
    "missing_title_url",
    "title_exclude_regex",
    "missing_or_invalid_date",
    "lookback",
    "keyword_regex",
    "min_signal_score",
    "typed_filter",
    "dup_round",
    # 回源补全之后才判，见 process.drop_too_short；放在 dup_round 后面是它真实的顺序
    "min_content_chars",
    "kept",
)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def today() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def new_run_id() -> str:
    """带上 Actions 的 run id，好让一行健康数据能反查到当时的 CI 日志。"""
    ci = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    stamp = datetime.now(CN_TZ).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-gh{ci}" if ci else f"{stamp}-local"


class Funnel:
    """分源漏斗计数器。

    同时维护总计，这样原来那行「清洗漏斗 raw=… kept=… drops=…」的日志不变，
    调用方不传 funnel 时行为与改造前完全一致。
    """

    def __init__(self) -> None:
        self.by_source: dict[str, dict[str, int]] = {}
        self.totals: dict[str, int] = {}

    def bump(self, source_id: str, stage: str, count: int = 1) -> None:
        self.totals[stage] = self.totals.get(stage, 0) + count
        per = self.by_source.setdefault(str(source_id or ""), {})
        per[stage] = per.get(stage, 0) + count

    def for_source(self, source_id: str) -> dict[str, int]:
        return dict(self.by_source.get(str(source_id or "")) or {})

    def drops(self) -> dict[str, int]:
        return {
            stage: n
            for stage, n in sorted(self.totals.items(), key=lambda kv: -kv[1])
            if stage not in {"raw", "kept"}
        }


def _blocking_stage(funnel: dict[str, int]) -> str:
    """抓到了原始条目却一条没留下时，指出淘汰最多的那一步。"""
    if not funnel.get("raw") or funnel.get("kept"):
        return ""
    drops = {s: n for s, n in funnel.items() if s not in {"raw", "kept"} and n > 0}
    return max(drops, key=lambda s: drops[s]) if drops else ""


def build_records(
    *,
    run_id: str,
    param_records: list[dict[str, Any]],
    attempted: dict[str, dict[str, Any]],
    funnel: Funnel,
    fetch_stats: dict[str, dict[str, Any]] | None = None,
    cleaned_items: list[dict[str, Any]] | None = None,
    final_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """给本轮尝试过的每个源生成一行健康记录。

    attempted：source_id -> 该源的 feed 配置（映射后的），决定报告里出现哪些源。
    cleaned_items / final_items：清洗后与跨轮去重后，用来算真正入库量和去重损耗。
    即使一条都没抓到也要出一行，否则「彻底静默的源」在数据里根本不存在。
    """
    from . import sources as sources_mod

    fetch_stats = fetch_stats or {}
    meta: dict[str, dict[str, Any]] = {}
    for rec in param_records or []:
        fields = rec.get("fields") or {}
        sid = str(sources_mod.cell(fields.get("source_id")) or "").strip()
        if sid:
            meta[sid] = fields

    cleaned_counts: dict[str, int] = {}
    for item in cleaned_items or []:
        sid = str(item.get("source_id") or "")
        cleaned_counts[sid] = cleaned_counts.get(sid, 0) + 1
    final_counts: dict[str, int] = {}
    for item in final_items or []:
        sid = str(item.get("source_id") or "")
        final_counts[sid] = final_counts.get(sid, 0) + 1

    ts = now_ms()
    dt = today()
    rows: list[dict[str, Any]] = []
    for source_id, feed in sorted(attempted.items()):
        fields = meta.get(source_id) or {}
        source_funnel = funnel.for_source(source_id)
        cleaned = cleaned_counts.get(source_id, 0)
        written = final_counts.get(source_id, 0)
        rows.append(
            {
                "run_id": run_id,
                "ts_ms": ts,
                "dt": dt,
                "source_id": source_id,
                "name": str(sources_mod.cell(fields.get("name")) or feed.get("name") or ""),
                "fetch_method": str(
                    sources_mod.cell(fields.get("fetch_method")) or feed.get("fetch_method") or ""
                ),
                "status": str(sources_mod.cell(fields.get("status")) or ""),
                "tier": str(sources_mod.cell(fields.get("tier")) or ""),
                "priority": str(sources_mod.cell(fields.get("priority")) or ""),
                "fetch": dict(fetch_stats.get(source_id) or {}),
                "funnel": source_funnel,
                "cleaned": cleaned,
                "written": written,
                # 清洗通过却被跨轮去重掉的量：一直是老新闻的源在这里会很显眼
                "dedup_dropped": max(0, cleaned - written),
                "blocked_at": _blocking_stage(source_funnel),
            }
        )
    return rows


def write_records(rows: list[dict[str, Any]], *, base_dir: Path | None = None) -> Path | None:
    """按天分片追加落盘。

    分片而非单文件：一天一个文件，追加不会互相打断，也方便按天丢弃或上传。
    这里是唯一的落盘点，换外部 sink 只改这个函数。
    """
    if not rows:
        return None
    directory = Path(base_dir or HEALTH_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"dt={rows[0].get('dt') or today()}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def load_records(
    *, days: int = 14, base_dir: Path | None = None
) -> list[dict[str, Any]]:
    """读最近 days 天的健康记录，坏行跳过（宁可少几行也别让报告起不来）。"""
    directory = Path(base_dir or HEALTH_DIR)
    if not directory.is_dir():
        return []
    cutoff = (datetime.now(CN_TZ) - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("dt=*.jsonl")):
        if path.name[len("dt=") : -len(".jsonl")] < cutoff:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按源汇总：入库总量、断流天数、最常见的卡点。

    断流天数（dry_days）是这份数据最主要的产出——覆盖式回写算不出它。
    """
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = str(row.get("source_id") or "")
        if not sid:
            continue
        agg = by_source.setdefault(
            sid,
            {
                "source_id": sid,
                "name": row.get("name") or sid,
                "status": row.get("status") or "",
                "tier": row.get("tier") or "",
                "priority": row.get("priority") or "",
                "fetch_method": row.get("fetch_method") or "",
                "runs": 0,
                "raw": 0,
                "written": 0,
                "dedup_dropped": 0,
                "last_written_dt": "",
                "last_run_dt": "",
                "blocked_counts": {},
                "fetch_errors": {},
            },
        )
        agg["runs"] += 1
        agg["status"] = row.get("status") or agg["status"]
        agg["raw"] += int((row.get("funnel") or {}).get("raw") or 0)
        written = int(row.get("written") or 0)
        agg["written"] += written
        agg["dedup_dropped"] += int(row.get("dedup_dropped") or 0)
        dt = str(row.get("dt") or "")
        if dt > agg["last_run_dt"]:
            agg["last_run_dt"] = dt
        if written and dt > agg["last_written_dt"]:
            agg["last_written_dt"] = dt
        blocked = str(row.get("blocked_at") or "")
        if blocked:
            agg["blocked_counts"][blocked] = agg["blocked_counts"].get(blocked, 0) + 1
        error = str((row.get("fetch") or {}).get("error") or "")
        if error:
            agg["fetch_errors"][error] = agg["fetch_errors"].get(error, 0) + 1

    today_str = today()
    out: list[dict[str, Any]] = []
    for agg in by_source.values():
        if agg["last_written_dt"]:
            delta = datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(
                agg["last_written_dt"], "%Y-%m-%d"
            )
            agg["dry_days"] = delta.days
        else:
            # 观测窗口内从未入库；用窗口长度当下限，别假装知道具体断了多久
            agg["dry_days"] = None
        agg["top_block"] = (
            max(agg["blocked_counts"], key=lambda k: agg["blocked_counts"][k])
            if agg["blocked_counts"]
            else ""
        )
        agg["top_fetch_error"] = (
            max(agg["fetch_errors"], key=lambda k: agg["fetch_errors"][k])
            if agg["fetch_errors"]
            else ""
        )
        out.append(agg)
    # 最该处理的排在前面：从未入库 > 断流久 > 抓得多但留不下
    out.sort(
        key=lambda a: (
            0 if a["dry_days"] is None else 1,
            -(a["dry_days"] or 0),
            -a["raw"],
        )
    )
    return out


def _cell(text: str, width: int) -> str:
    """按显示宽度截断/补齐：中文占两列，否则表格会错位。"""
    out, used = "", 0
    for char in str(text):
        step = 2 if ord(char) > 0x2E7F else 1
        if used + step > width:
            break
        out += char
        used += step
    return out + " " * (width - used)


def report(days: int = 14, *, base_dir: Path | None = None) -> int:
    rows = load_records(days=days, base_dir=base_dir)
    if not rows:
        print(
            f"没有健康记录（{Path(base_dir or HEALTH_DIR)}）。"
            "跑一次 python -m src.main 之后再看。"
        )
        return 0
    summary = summarize(rows)
    runs = len({str(r.get("run_id") or "") for r in rows})
    print(f"最近 {days} 天 · {runs} 轮采集 · {len(summary)} 个源\n")
    header = (
        _cell("源", 26)
        + _cell("状态", 14)
        + _cell("断流", 7)
        + _cell("抓取", 7)
        + _cell("入库", 6)
        + _cell("去重损耗", 10)
        + "卡点 / 抓取错误"
    )
    print(header)
    print("-" * 104)
    for agg in summary:
        dry = "从未" if agg["dry_days"] is None else f"{agg['dry_days']}天"
        blocked = agg["top_block"] or ""
        error = agg["top_fetch_error"] or ""
        note = " / ".join(x for x in (blocked, error) if x) or "—"
        print(
            _cell(agg["name"] or agg["source_id"], 26)
            + _cell(f"{agg['status']} {agg['priority']}".strip(), 14)
            + _cell(dry, 7)
            + _cell(str(agg["raw"]), 7)
            + _cell(str(agg["written"]), 6)
            + _cell(str(agg["dedup_dropped"]), 10)
            + note
        )
    silent = [a for a in summary if a["dry_days"] is None and a["status"] == "active"]
    if silent:
        print(
            f"\n{len(silent)} 个 active 源在这段时间内一条都没入库："
            + "、".join(a["source_id"] for a in silent[:12])
            + ("…" if len(silent) > 12 else "")
        )
    return 0


def run() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="分源采集健康体检")
    parser.add_argument("--days", type=int, default=14, help="回看天数，默认 14")
    args = parser.parse_args()
    return report(args.days)


if __name__ == "__main__":
    raise SystemExit(run())
