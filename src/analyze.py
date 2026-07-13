"""源贡献分析：统计「信号条目表」里每条记录来自「参数表」哪个源，
生成排名报表 + 自包含 HTML 可视化（横向柱状图），并列出零产出的源。

运行：python -m src.analyze
输出：output/contribution.html、output/contribution.csv
"""
from __future__ import annotations

import csv
import html
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, feishu, sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("analyze")

OUTPUT_DIR = Path("output")


def _ms_to_date(v: Any) -> str:
    try:
        ms = int(float(v))
    except (ValueError, TypeError):
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _build_param_map(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    param_map: dict[str, dict[str, Any]] = {}
    for rec in records:
        f = rec.get("fields") or {}
        sid = sources.cell(f.get("source_id"))
        if not sid:
            continue
        tier_raw = sources.cell(f.get("tier")) or ""
        param_map[str(sid)] = {
            "name": sources.cell(f.get("name")) or sid,
            "tier": config.TIER_LABEL.get(tier_raw, tier_raw),
            "dimension": sources.cell(f.get("dimension")) or "",
            "fetch_method": sources.cell(f.get("fetch_method")) or "",
            "status": sources.cell(f.get("status")) or "",
        }
    return param_map


def analyze(token: str) -> dict[str, Any]:
    param_records = feishu.read_param_records(token)
    param_map = _build_param_map(param_records)
    log.info("参数表 %d 个源", len(param_map))

    entries = feishu.read_all_records(
        token,
        config.FEISHU_ENTRY_TABLE_ID,
        field_names=["source_id", "来源", "采集时间", "层级", "分类"],
    )
    log.info("条目表 %d 条记录", len(entries))

    counts: Counter[str] = Counter()
    latest: dict[str, int] = {}
    name_from_entry: dict[str, str] = {}
    for f in entries:
        sid = sources.cell(f.get("source_id")) or ""
        display = sources.cell(f.get("来源")) or ""
        key = str(sid) if sid else f"(名称){display}"
        counts[key] += 1
        if display and key not in name_from_entry:
            name_from_entry[key] = display
        try:
            ts = int(float(sources.cell(f.get("采集时间")) or 0))
        except (ValueError, TypeError):
            ts = 0
        if ts > latest.get(key, 0):
            latest[key] = ts

    total = sum(counts.values())
    all_keys = set(counts) | set(param_map)
    rows: list[dict[str, Any]] = []
    for key in all_keys:
        meta = param_map.get(key, {})
        count = counts.get(key, 0)
        rows.append(
            {
                "source_id": key,
                "name": meta.get("name") or name_from_entry.get(key) or key,
                "tier": meta.get("tier", ""),
                "dimension": meta.get("dimension", ""),
                "status": meta.get("status", ""),
                "count": count,
                "share": (count / total * 100) if total else 0.0,
                "last_collected": _ms_to_date(latest.get(key, 0)),
                "in_param": key in param_map,
            }
        )
    rows.sort(key=lambda r: (-r["count"], r["name"]))

    zero_output = [r for r in rows if r["in_param"] and r["count"] == 0]
    orphans = [r for r in rows if not r["in_param"] and r["count"] > 0]

    return {
        "total": total,
        "source_total": len(param_map),
        "producing": len([r for r in rows if r["count"] > 0 and r["in_param"]]),
        "rows": rows,
        "zero_output": zero_output,
        "orphans": orphans,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _render_html(data: dict[str, Any]) -> str:
    rows = data["rows"]
    max_count = max((r["count"] for r in rows), default=1) or 1
    esc = html.escape

    row_html = []
    for i, r in enumerate(rows, 1):
        width = r["count"] / max_count * 100
        badge = "" if r["in_param"] else '<span class="tag orphan">不在参数表</span>'
        zero = ' class="zero"' if (r["in_param"] and r["count"] == 0) else ""
        row_html.append(
            f"""<tr{zero}>
  <td class="rank">{i}</td>
  <td><div class="name">{esc(str(r['name']))} {badge}</div>
      <div class="sid">{esc(str(r['source_id']))}</div></td>
  <td class="tier">{esc(str(r['tier']))}</td>
  <td class="num">{r['count']}</td>
  <td class="bar"><div class="track"><div class="fill" style="width:{width:.1f}%"></div></div>
      <span class="pct">{r['share']:.1f}%</span></td>
  <td class="date">{esc(str(r['last_collected']))}</td>
</tr>"""
        )

    zero_names = "、".join(esc(str(r["name"])) for r in data["zero_output"]) or "无"

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>源贡献分析</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 32px; background: #0f1115; color: #e6e8eb; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .meta {{ color: #8b929e; font-size: 13px; margin-bottom: 24px; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
  .card {{ background: #1a1e26; border: 1px solid #2a2f3a; border-radius: 12px;
          padding: 16px 20px; min-width: 130px; }}
  .card .k {{ font-size: 12px; color: #8b929e; }}
  .card .v {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; color: #8b929e; font-weight: 500; font-size: 12px;
       padding: 8px 10px; border-bottom: 1px solid #2a2f3a; }}
  td {{ padding: 10px; border-bottom: 1px solid #1e232c; vertical-align: middle; }}
  tr.zero {{ opacity: .5; }}
  .rank {{ color: #8b929e; width: 32px; }}
  .name {{ font-weight: 600; }}
  .sid {{ color: #6b7280; font-size: 12px; font-family: ui-monospace, monospace; }}
  .tier {{ color: #a9b1bd; font-size: 12px; white-space: nowrap; }}
  .num {{ font-variant-numeric: tabular-nums; font-weight: 700; text-align: right; width: 56px; }}
  .bar {{ width: 42%; }}
  .track {{ background: #232833; border-radius: 6px; height: 14px; display: inline-block;
           width: calc(100% - 52px); vertical-align: middle; overflow: hidden; }}
  .fill {{ background: linear-gradient(90deg,#4f8cff,#8b5cf6); height: 100%; border-radius: 6px; }}
  .pct {{ color: #8b929e; font-size: 12px; margin-left: 8px; font-variant-numeric: tabular-nums; }}
  .date {{ color: #8b929e; font-size: 12px; white-space: nowrap; }}
  .tag {{ font-size: 11px; padding: 1px 6px; border-radius: 5px; }}
  .tag.orphan {{ background: #4a2233; color: #ff9db5; }}
  .note {{ margin-top: 24px; color: #8b929e; font-size: 13px; line-height: 1.6; }}
</style></head>
<body>
  <h1>源贡献分析</h1>
  <div class="meta">生成于 {data['generated_at']}</div>
  <div class="cards">
    <div class="card"><div class="k">条目总数</div><div class="v">{data['total']}</div></div>
    <div class="card"><div class="k">参数表源数</div><div class="v">{data['source_total']}</div></div>
    <div class="card"><div class="k">有产出的源</div><div class="v">{data['producing']}</div></div>
    <div class="card"><div class="k">零产出的源</div><div class="v">{len(data['zero_output'])}</div></div>
  </div>
  <table>
    <thead><tr><th>#</th><th>源</th><th>层级</th><th>条数</th><th>占比</th><th>最近采集</th></tr></thead>
    <tbody>
      {''.join(row_html)}
    </tbody>
  </table>
  <div class="note">零产出的源（在参数表但一条未入库）：{zero_names}</div>
</body></html>"""


def _write_csv(data: dict[str, Any], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source_id", "名称", "层级", "分类", "条数", "占比%", "最近采集", "在参数表"])
        for r in data["rows"]:
            writer.writerow(
                [r["source_id"], r["name"], r["tier"], r["dimension"], r["count"],
                 f"{r['share']:.1f}", r["last_collected"], "是" if r["in_param"] else "否"]
            )


def _print_summary(data: dict[str, Any]) -> None:
    log.info("=== 源贡献排名（Top 15）===")
    for i, r in enumerate(data["rows"][:15], 1):
        log.info("%2d. %-28s %4d 条 (%.1f%%)  %s", i, r["name"][:28], r["count"], r["share"], r["tier"])
    if data["zero_output"]:
        log.info("零产出的源 %d 个：%s", len(data["zero_output"]),
                 "、".join(r["name"] for r in data["zero_output"]))
    if data["orphans"]:
        log.info("不在参数表但有产出的源 %d 个：%s", len(data["orphans"]),
                 "、".join(r["name"] for r in data["orphans"]))


def run() -> int:
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        raise config.ConfigError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
    token = feishu.get_tenant_access_token()
    data = analyze(token)

    OUTPUT_DIR.mkdir(exist_ok=True)
    html_path = OUTPUT_DIR / "contribution.html"
    csv_path = OUTPUT_DIR / "contribution.csv"
    html_path.write_text(_render_html(data), encoding="utf-8")
    _write_csv(data, csv_path)

    _print_summary(data)
    log.info("报表已生成：%s", html_path.resolve())
    log.info("CSV 已生成：%s", csv_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
