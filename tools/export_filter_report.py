"""把 A/B 干跑的分源筛选结果导成可逐行查看的表格。

输入：tools.ab_pipeline 产出的 output/ab-pipeline.json
     + 飞书一级参数表（时间窗、采集端点、优先级、分类）

输出：
    output/filter-report.csv    UTF-8 BOM，Excel / Numbers 直接打开
    output/filter-report.html   官网与端点可点击，按 raw 降序，整源归零的行标红

只读飞书，不写任何表。

用法：
    python -m tools.export_filter_report
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
AB_JSON = ROOT / "output" / "ab-pipeline.json"
CSV_PATH = ROOT / "output" / "filter-report.csv"
HTML_PATH = ROOT / "output" / "filter-report.html"

# 展示顺序：先按漏斗先后，再按今天的量级
DROP_COLUMNS = (
    "missing_title_url",
    "title_exclude_regex",
    "missing_or_invalid_date",
    "lookback",
    "min_content_chars",
    "min_chars",
    "keyword_regex",
    "keyword_exclude",
    "not_ai_policy",
    "min_signal_score",
    "min_quality_score",
    "typed_filter",
    "per_feed_cap",
    "dup_round",
)

DROP_LABELS = {
    "missing_title_url": "缺标题或链接",
    "title_exclude_regex": "标题排除正则",
    "missing_or_invalid_date": "缺发布时间",
    "lookback": "超出时间窗",
    "min_content_chars": "正文过短(补全后仍不足)",
    "min_chars": "正文过短(类型规则)",
    "keyword_regex": "未命中关键词",
    "keyword_exclude": "命中排除关键词",
    "not_ai_policy": "非 AI 政策",
    "min_signal_score": "本地信号分不足",
    "min_quality_score": "富集后质量分不足",
    "typed_filter": "类型规则其他",
    "per_feed_cap": "单源条数上限",
    "dup_round": "本轮内重复",
}


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def cell_text(value: Any) -> str:
    """飞书单元格取纯文本：链接字段是 dict，多选是 list。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "").strip()
    if isinstance(value, list):
        parts = [cell_text(v) for v in value]
        return " / ".join(p for p in parts if p)
    return str(value).strip()


def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return ""


_FEED_TAIL = re.compile(
    r"(?:/(?:rss|atom|feed|feeds)(?:\.xml)?/?|/index\.xml|\.atom|\.rss|\.xml)$", re.I
)
_ARXIV_RSS = re.compile(r"^https?://rss\.arxiv\.org/rss/([\w.\-]+)", re.I)


def derive_site(endpoint: str) -> str:
    """从采集端点推导官网页面。

    不按主域名做模糊匹配：qwenlm.github.io 的主域名是 github.io，会撞到别的
    github.io 源；youtube 频道之间也会互相串。端点本身最可靠。
    """
    if not endpoint.startswith(("http://", "https://")):
        return ""
    arxiv = _ARXIV_RSS.match(endpoint)
    if arxiv:
        return f"https://arxiv.org/list/{arxiv.group(1)}/recent"
    parsed = urlparse(endpoint)
    host = parsed.netloc
    for prefix in ("rss.", "feeds.", "feed."):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
            break
    path = _FEED_TAIL.sub("", parsed.path or "")
    return f"{parsed.scheme}://{host}{path}" or f"{parsed.scheme}://{host}"


def origin_of(url: str) -> str:
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    except ValueError:
        return ""


def lookback_hours(param: dict[str, Any]) -> int:
    """配置了就按配置解析，没配回落 config.MIN_LOOKBACK_HOURS，与 process 一致。"""
    from src import config, sources

    raw = cell_text(param.get("lookback_window"))
    if not raw:
        return config.MIN_LOOKBACK_HOURS
    try:
        return int(sources.parse_lookback_hours(raw) or config.MIN_LOOKBACK_HOURS)
    except Exception:  # noqa: BLE001 - 认不出的写法按默认算
        return config.MIN_LOOKBACK_HOURS


def build_rows(
    ab: dict[str, Any],
    params: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan_b = ab["plans"]["B 现状"]
    plan_a = ab["plans"]["A 新抽取"]

    # read_param_records 返回原始 items（{record_id, fields}），要先取 fields
    param_by_id: dict[str, dict[str, Any]] = {}
    for record in params:
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
        sid = cell_text(fields.get("source_id"))
        if sid:
            param_by_id[sid] = fields

    source_ids = sorted(
        set(plan_b["by_source"]) | set(plan_a["by_source"]),
        key=lambda s: -(plan_b["by_source"].get(s, {}).get("raw", 0)),
    )

    rows: list[dict[str, Any]] = []
    for sid in source_ids:
        if not sid:
            continue
        per_b = plan_b["by_source"].get(sid, {})
        per_a = plan_a["by_source"].get(sid, {})
        raw = per_b.get("raw", 0)
        kept_b = plan_b["final_by_source"].get(sid, 0)
        kept_a = plan_a["final_by_source"].get(sid, 0)

        param = param_by_id.get(sid, {})
        endpoint = cell_text(param.get("endpoint"))
        # 端点推导优先：github.com / youtube.com / modelscope.cn 这类平台域名下挂着
        # 多个源，只取域名根会让它们全指向同一处。端点逐源唯一。
        derived = derive_site(endpoint)
        official = derived or origin_of(endpoint)
        site_from = "端点推导" if derived else "端点域名"

        drops_b = {k: per_b.get(k, 0) for k in DROP_COLUMNS}
        top = max(drops_b.items(), key=lambda kv: kv[1]) if any(drops_b.values()) else ("", 0)

        if raw == 0:
            verdict = "抓取阶段就没拿到条目"
        elif kept_b == 0:
            verdict = f"整源归零 · {DROP_LABELS.get(top[0], top[0])}"
        elif top[1] > kept_b:
            verdict = f"淘汰多于保留 · {DROP_LABELS.get(top[0], top[0])}"
        else:
            verdict = "正常产出"

        rows.append(
            {
                "源ID": sid,
                "名称": cell_text(param.get("name")),
                "官网": official,
                "官网来源": site_from,
                "采集端点": endpoint,
                "采集方式": cell_text(param.get("fetch_method")),
                "优先级": cell_text(param.get("priority")),
                "层级": cell_text(param.get("tier")),
                "分类": cell_text(param.get("dimension")),
                "状态": cell_text(param.get("status")),
                "时间窗(配置)": cell_text(param.get("lookback_window")) or "未配置",
                "时间窗(生效小时)": lookback_hours(param),
                "raw": raw,
                "入库(B现状)": kept_b,
                "入库(A新抽取)": kept_a,
                "入库差(A-B)": kept_a - kept_b,
                "淘汰合计": sum(drops_b.values()),
                "主要淘汰原因": DROP_LABELS.get(top[0], top[0]) if top[1] else "",
                "诊断": verdict,
                **{f"淘汰·{DROP_LABELS[k]}": v for k, v in drops_b.items()},
                "A方案淘汰·本地信号分不足": per_a.get("min_signal_score", 0),
                "A方案淘汰·富集后质量分不足": per_a.get("min_quality_score", 0),
                "备注": cell_text(param.get("notes")),
            }
        )
    return rows


def write_csv(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 打开中文 CSV 不乱码
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    link_cols = {"官网", "采集端点"}

    def cell(row: dict[str, Any], col: str) -> str:
        value = row[col]
        if col in link_cols and str(value).startswith("http"):
            safe = html.escape(str(value), quote=True)
            # 去掉协议头显示，官网与端点才能一眼区分（两者常同域）
            short = str(value).split("://", 1)[-1]
            if len(short) > 48:
                short = short[:47] + "…"
            return (
                f'<a href="{safe}" target="_blank" rel="noreferrer" '
                f'title="{safe}">{html.escape(short)}</a>'
            )
        text = html.escape(str(value))
        if col.startswith("淘汰·") and value == 0:
            return '<span class="zero">0</span>'
        return text

    body = []
    for row in rows:
        cls = []
        if row["raw"] == 0:
            cls.append("nofetch")
        elif row["入库(B现状)"] == 0:
            cls.append("zerokept")
        if row["入库差(A-B)"]:
            cls.append("diff")
        tds = "".join(f"<td>{cell(row, c)}</td>" for c in cols)
        body.append(f'<tr class="{" ".join(cls)}">{tds}</tr>')

    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>分源筛选报告 {html.escape(str(meta.get("generated_at") or ""))}</title>
<style>
  :root {{ --line:#e5e5e5; --muted:#8a8a8a; --warn:#fff4f4; --gray:#fafafa; }}
  body {{ font:14px/1.5 -apple-system,"PingFang SC","Hiragino Sans GB",sans-serif;
         margin:24px; color:#1a1a1a; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .meta {{ color:var(--muted); margin-bottom:16px; }}
  .legend span {{ display:inline-block; margin-right:16px; padding:2px 8px; border-radius:4px; }}
  table {{ border-collapse:collapse; font-size:13px; }}
  th,td {{ border:1px solid var(--line); padding:5px 8px; white-space:nowrap; }}
  thead th {{ position:sticky; top:0; background:#fff; box-shadow:0 1px 0 var(--line);
             text-align:left; font-weight:600; }}
  tbody tr:hover {{ background:#f0f7ff; }}
  tr.zerokept {{ background:var(--warn); }}
  tr.nofetch {{ background:#f3f0ff; }}
  tr.diff td {{ font-weight:600; }}
  .zero {{ color:#d0d0d0; }}
  a {{ color:#0b62d0; }}
  /* 最后两列是「主要看什么」和「备注」，让它们换行而不是把表撑宽 */
  td:last-child, td:nth-last-child(2) {{ white-space:normal; max-width:260px; }}
</style>
</head>
<body>
<h1>分源筛选报告 · 两套抽取方案对比</h1>
<div class="meta">
  生成于 {html.escape(str(meta.get("generated_at") or ""))} ·
  通道 {html.escape(", ".join(meta.get("methods") or []))} ·
  原始条目 {meta.get("raw_total")} 条 ·
  两套方案均按 day1 计算（不读历史去重键）
</div>
<div class="legend">
  <span style="background:var(--warn)">整源归零</span>
  <span style="background:#f3f0ff">抓取阶段就没拿到条目</span>
  <span style="background:#fff;border:1px solid var(--line);font-weight:600">两套方案入库数不同</span>
</div>
<table>
<thead><tr>{head}</tr></thead>
<tbody>
{chr(10).join(body)}
</tbody>
</table>
</body>
</html>
"""
    HTML_PATH.write_text(doc, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ab-json", default=str(AB_JSON))
    args = parser.parse_args()

    load_dotenv()
    path = Path(args.ab_json)
    if not path.exists():
        print(f"找不到 {path}，先跑 python -m tools.ab_pipeline")
        return 1
    ab = json.loads(path.read_text(encoding="utf-8"))

    from src import config, feishu

    config.validate()
    token = feishu.get_tenant_access_token()
    params = feishu.read_param_records(token)
    print(f"一级参数 {len(params)} 条（只读）")

    rows = build_rows(ab, params)
    write_csv(rows)
    write_html(rows, ab)

    zero = [r for r in rows if r["raw"] and not r["入库(B现状)"]]
    nofetch = [r for r in rows if not r["raw"]]
    print(f"\n共 {len(rows)} 个源")
    print(f"  正常产出 {sum(1 for r in rows if r['入库(B现状)'])} 个")
    print(f"  整源归零 {len(zero)} 个")
    print(f"  抓取阶段就没拿到条目 {len(nofetch)} 个")
    print(f"  两套方案入库数不同 {sum(1 for r in rows if r['入库差(A-B)'])} 个")
    print(f"\n{CSV_PATH.relative_to(ROOT)}")
    print(f"{HTML_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
