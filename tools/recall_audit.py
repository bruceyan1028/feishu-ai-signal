"""召回审计：给每条被淘汰的原料标出确切原因，再按原因分层抽样成可人工判读的表。

目的是建 ground truth。换抽取器、调筛选参数之前，先知道现有参数误杀了多少——
没有这批标注，任何「A 比 B 好」都不可证。

归因办法是把条目**逐条**送进真实的 process.process_and_clean，读它的分源漏斗。
这样归因用的是生产代码路径，不会因为审计工具自己重写一遍过滤逻辑而失真。

两个已知边界，报告里也会写明：
  - per_feed_cap 与 dup_round 依赖批内状态，逐条跑不会触发，故不参与抽样
  - min_quality_score 在论文外网富集之后才判定，只对论文源抽样时开启富集

用法：
    python -m tools.recall_audit --fetch              # 抓一轮并缓存原料
    python -m tools.recall_audit                      # 复用缓存，重新抽样
    python -m tools.recall_audit --per-reason 30
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output"
RAW_CACHE = OUT_DIR / "raw-items-cache.json"
CSV_PATH = OUT_DIR / "recall-audit.csv"
HTML_PATH = OUT_DIR / "recall-audit.html"
CN_TZ = timezone(timedelta(hours=8))

# 批内状态相关，逐条归因跑不出来，不纳入抽样
STATEFUL_STAGES = {"per_feed_cap", "dup_round"}

REASON_LABELS = {
    "missing_title_url": "缺标题或链接",
    "title_exclude_regex": "标题排除正则",
    "missing_or_invalid_date": "缺发布时间",
    "lookback": "超出时间窗",
    "min_content_chars": "正文过短(补全后仍不足)",
    "min_chars": "正文过短(类型规则)",
    "keyword_regex": "未命中关键词",
    "keyword_exclude": "命中排除关键词",
    "min_signal_score": "本地信号分不足",
    "min_quality_score": "富集后质量分不足",
    "typed_filter": "类型规则其他",
}

# 判读时最该盯什么，直接写进表里省得来回问
REASON_HINTS = {
    "lookback": "看「发布时间原文」和「解析后」是否一致：解析错会把新文判成旧文",
    "missing_or_invalid_date": "看「发布时间原文」：是空的（源没给）还是格式没认出来",
    "keyword_regex": "看标题正文是否明显 AI 相关：是则正则漏了词",
    "keyword_exclude": "看是否被排除词误伤",
    "min_chars": "看正文是否本该更长：短说明抽取失败，长说明门槛偏高",
    "min_content_chars": "同上；这一步在回源补全之后判，逐条归因跑不到，一般不会出现",
    "min_signal_score": "论文本地打分：看标题是否明显重要工作",
    "min_quality_score": "论文富集后打分：看是否有会议录用或社区热度",
    "title_exclude_regex": "看标题排除正则是否过宽",
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


def slim(item: dict[str, Any]) -> dict[str, Any]:
    """缓存用的瘦身版：正文截到 4000 字，够判读也够重跑过滤。"""
    feed = item.get("feed") or {}
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "body": str(item.get("body") or "")[:4000],
        "published_raw": item.get("published_raw"),
        "is_html": item.get("is_html"),
        "entry_tags": item.get("entry_tags"),
        "heat_keep": item.get("heat_keep"),
        "metrics": item.get("metrics"),
        "media_assets": item.get("media_assets"),
        "feed": feed,
    }


def fetch_and_cache() -> list[dict[str, Any]]:
    from src import config, feishu, typed_config
    from tools.ab_pipeline import fetch_raw

    config.validate()
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    type_configs = typed_config.load_typed_configs(token)
    raw_items, _feeds = fetch_raw(records, type_configs, {"RSS", "Scrape", "Media"})

    payload = {
        "fetched_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "type_configs": type_configs,
        "items": [slim(i) for i in raw_items],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_CACHE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"已缓存 {len(raw_items)} 条原料到 {RAW_CACHE.relative_to(ROOT)}")
    return raw_items


def attribute(
    items: list[dict[str, Any]], type_configs: dict[str, Any], paper_sample: int
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """逐条归因。返回 (带 _reason 的条目, 原因计数)。

    论文源要开外网富集才判得出 min_quality_score，所以只抽样跑；其余源全量跑，
    富集关掉以免为不会入库的候选打外网。
    """
    from src import health, process

    paper_items: list[dict[str, Any]] = []
    plain_items: list[dict[str, Any]] = []
    for item in items:
        feed = item.get("feed") or {}
        cfg = type_configs.get(str(feed.get("id") or "")) or {}
        is_paper = cfg.get("entity_type") == "paper" or "arxiv.org/" in str(
            item.get("url") or ""
        )
        (paper_items if is_paper else plain_items).append(item)

    random.shuffle(paper_items)
    picked_papers = paper_items[:paper_sample]
    print(
        f"逐条归因：普通源 {len(plain_items)} 条（关闭富集）、"
        f"论文源抽 {len(picked_papers)}/{len(paper_items)} 条（开启富集）"
    )
    attribute.stats = {  # type: ignore[attr-defined]
        "plain_total": len(plain_items),
        "paper_total": len(paper_items),
        "paper_sampled": len(picked_papers),
    }

    reasons: Counter[str] = Counter()
    out: list[dict[str, Any]] = []

    def run_one(item: dict[str, Any], enrich: bool) -> str:
        prev = os.environ.get("PAPER_ENRICH_ENABLED")
        os.environ["PAPER_ENRICH_ENABLED"] = "1" if enrich else "0"
        from src import config as cfg_mod

        cfg_mod.PAPER_ENRICH_ENABLED = enrich
        try:
            funnel = health.Funnel()
            kept = process.process_and_clean([item], type_configs, {}, funnel)
        except Exception as exc:  # noqa: BLE001 - 单条异常不能中断审计
            return f"审计异常:{type(exc).__name__}"
        finally:
            if prev is None:
                os.environ.pop("PAPER_ENRICH_ENABLED", None)
            else:
                os.environ["PAPER_ENRICH_ENABLED"] = prev
        if kept:
            return "kept"
        per = funnel.for_source(str((item.get("feed") or {}).get("id") or ""))
        stages = [k for k in per if k not in {"raw", "kept"}]
        return stages[0] if stages else "unknown"

    total = len(plain_items) + len(picked_papers)
    for index, (item, enrich) in enumerate(
        [(i, False) for i in plain_items] + [(i, True) for i in picked_papers], 1
    ):
        reason = run_one(item, enrich)
        reasons[reason] += 1
        row = dict(item)
        row["_reason"] = reason
        out.append(row)
        if index % 400 == 0:
            print(f"  ...{index}/{total}")
    return out, reasons


def to_audit_row(
    item: dict[str, Any], param_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    from src import process

    feed = item.get("feed") or {}
    sid = str(feed.get("id") or "")
    param = param_by_id.get(sid, {})
    reason = item["_reason"]

    published_ms = process.parse_date_ms(item.get("published_raw"))
    if published_ms:
        parsed = datetime.fromtimestamp(published_ms / 1000, CN_TZ).strftime(
            "%Y-%m-%d %H:%M"
        )
        age_h = round((process.now_ms() - published_ms) / 3600000, 1)
    else:
        parsed, age_h = "解析失败", ""

    body = process.strip_html_body(item.get("body"))
    lookback = int(feed.get("lookback_hours") or 168)
    keyword_regex = str(feed.get("keyword_regex") or "")

    return {
        "判读": "",  # 填 该放行 / 该淘汰 / 不确定
        "备注": "",
        "淘汰原因": REASON_LABELS.get(reason, reason),
        "判读要点": REASON_HINTS.get(reason, ""),
        "源ID": sid,
        "源名称": str(param.get("name") or ""),
        "优先级": str(param.get("priority") or ""),
        "标题": process.strip_html(item.get("title")),
        "URL": process.normalize_url(item.get("url")),
        "发布时间原文": str(item.get("published_raw") or ""),
        "发布时间解析后": parsed,
        "距今小时": age_h,
        "时间窗小时": lookback,
        "超窗倍数": round(age_h / lookback, 2) if age_h and lookback else "",
        "正文字数": len(body),
        "正文前300字": body[:300].replace("\n", " "),
        "该源关键词正则": keyword_regex[:180],
    }


def stratified_sample(
    rows: list[dict[str, Any]], per_reason: int, seed: int
) -> list[dict[str, Any]]:
    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        reason = row["_reason"]
        if reason in {"kept", "unknown"} or reason in STATEFUL_STAGES:
            continue
        by_reason[reason].append(row)

    rng = random.Random(seed)
    picked: list[dict[str, Any]] = []
    for reason in sorted(by_reason, key=lambda r: -len(by_reason[r])):
        pool = by_reason[reason]
        rng.shuffle(pool)
        picked.extend(pool[:per_reason])
    return picked


def write_csv(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    if not rows:
        return
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["淘汰原因"]].append(row)

    blocks = []
    for reason in sorted(groups, key=lambda r: -len(groups[r])):
        items = groups[reason]
        hint = items[0]["判读要点"]
        # 论文源只抽样归因，所以这里只能说「归因命中」，不能说「全量」
        total = meta["reason_totals"].get(reason, len(items))
        cards = []
        for i, row in enumerate(items, 1):
            url = html.escape(row["URL"], quote=True)
            extra = ""
            if reason == "超出时间窗":
                extra = (
                    f'<div class="kv">发布原文 <code>{html.escape(row["发布时间原文"])}</code>'
                    f' → 解析 <b>{html.escape(row["发布时间解析后"])}</b>'
                    f' · 距今 {row["距今小时"]}h · 窗口 {row["时间窗小时"]}h'
                    f' · 超窗 {row["超窗倍数"]}×</div>'
                )
            elif reason == "缺发布时间":
                extra = (
                    f'<div class="kv">发布原文 <code>'
                    f'{html.escape(row["发布时间原文"]) or "(空)"}</code></div>'
                )
            elif reason == "未命中关键词":
                extra = (
                    f'<div class="kv">该源正则 <code>'
                    f'{html.escape(row["该源关键词正则"])}</code></div>'
                )
            cards.append(
                f"""<div class="card">
  <div class="hd"><span class="n">{i}</span>
    <a href="{url}" target="_blank" rel="noreferrer">{html.escape(row["标题"] or "(无标题)")}</a></div>
  <div class="meta">{html.escape(row["源ID"])} · {html.escape(row["源名称"])}
    · {html.escape(row["优先级"])} · 正文 {row["正文字数"]} 字</div>
  {extra}
  <div class="body">{html.escape(row["正文前300字"])}</div>
  <div class="judge">判读：
    <label><input type="radio" name="j{reason}{i}"> 该放行</label>
    <label><input type="radio" name="j{reason}{i}"> 该淘汰</label>
    <label><input type="radio" name="j{reason}{i}"> 不确定</label></div>
</div>"""
            )
        blocks.append(
            f"""<section>
<h2>{html.escape(reason)} <small>抽 {len(items)} 条 / 归因命中 {total} 条</small></h2>
<p class="hint">{html.escape(hint)}</p>
{"".join(cards)}
</section>"""
        )

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>召回审计 · {html.escape(meta["fetched_at"])}</title>
<style>
  body {{ font:15px/1.65 -apple-system,"PingFang SC","Hiragino Sans GB",sans-serif;
         max-width:960px; margin:32px auto; padding:0 20px; color:#1a1a1a; }}
  h1 {{ font-size:22px; margin-bottom:4px; }}
  .top {{ color:#777; margin-bottom:8px; }}
  .note {{ background:#fffbe6; border:1px solid #ffe58f; padding:10px 14px;
           border-radius:6px; margin:16px 0 28px; font-size:14px; }}
  h2 {{ font-size:18px; margin:36px 0 4px; padding-bottom:6px;
        border-bottom:2px solid #1a1a1a; }}
  h2 small {{ font-weight:400; color:#888; font-size:14px; }}
  .hint {{ color:#0b62d0; background:#f0f7ff; padding:8px 12px;
           border-radius:5px; margin:8px 0 18px; font-size:14px; }}
  .card {{ border:1px solid #e5e5e5; border-radius:6px; padding:12px 14px;
           margin-bottom:12px; }}
  .card:hover {{ border-color:#0b62d0; }}
  .hd {{ font-weight:600; margin-bottom:4px; }}
  .hd .n {{ display:inline-block; min-width:24px; color:#aaa; }}
  .meta {{ color:#888; font-size:13px; }}
  .kv {{ font-size:13px; margin-top:5px; }}
  .kv code {{ background:#f5f5f5; padding:1px 5px; border-radius:3px;
              word-break:break-all; }}
  .body {{ color:#555; font-size:13.5px; margin-top:7px;
           border-left:3px solid #eee; padding-left:10px; }}
  .judge {{ margin-top:9px; font-size:13px; color:#666; }}
  .judge label {{ margin-right:14px; cursor:pointer; }}
  a {{ color:#0b62d0; }}
</style></head><body>
<h1>召回审计 · 被淘汰条目分层抽样</h1>
<div class="top">原料抓取于 {html.escape(meta["fetched_at"])} ·
  共 {meta["raw_total"]} 条 · 归因 {meta["attributed"]} 条 ·
  每个原因抽样上限 {meta["per_reason"]} 条 · 随机种子 {meta["seed"]}</div>
<div class="note">
  <b>判读方法</b>：每条只回答一个问题——<b>这条本该进今天的简报候选池吗</b>。
  按「判读要点」的提示看关键字段即可，不必读完正文。
  单选框只是屏上标记，正式记录请填 <code>output/recall-audit.csv</code> 的「判读」列
  （填 <code>该放行</code> / <code>该淘汰</code> / <code>不确定</code>）。
  <br><br>
  <b>两个不参与抽样的原因</b>：<code>per_feed_cap</code> 与 <code>dup_round</code>
  依赖批内状态，逐条归因跑不出来，需要另外的方法核查。
  <br><br>
  <b>「归因命中」不等于全量</b>：普通源 {meta["plain_total"]} 条全部逐条归因；
  论文源共 {meta["paper_total"]} 条，只抽 {meta["paper_sampled"]} 条开启外网富集后归因
  （否则要为不会入库的候选打上千次外网）。所以
  <code>本地信号分不足</code> 与 <code>富集后质量分不足</code> 的计数是抽样内的，
  按比例放大才是全量估计。
</div>
{"".join(blocks)}
</body></html>
"""
    HTML_PATH.write_text(doc, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="重新抓取并覆盖缓存")
    parser.add_argument("--per-reason", type=int, default=20)
    parser.add_argument("--paper-sample", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260831)
    args = parser.parse_args()

    load_dotenv()

    if args.fetch or not RAW_CACHE.exists():
        if not args.fetch:
            print("没有缓存，先抓一轮")
        fetch_and_cache()

    payload = json.loads(RAW_CACHE.read_text(encoding="utf-8"))
    items = payload["items"]
    type_configs = payload.get("type_configs") or {}
    print(f"原料 {len(items)} 条（抓取于 {payload['fetched_at']}）")

    t0 = time.perf_counter()
    attributed, reasons = attribute(items, type_configs, args.paper_sample)
    print(f"归因耗时 {time.perf_counter() - t0:.0f}s")
    print("\n逐条归因结果：")
    for reason, count in reasons.most_common():
        print(f"  {count:5d}  {REASON_LABELS.get(reason, reason)}")

    from src import config, feishu

    config.validate()
    token = feishu.get_tenant_access_token()
    param_by_id: dict[str, dict[str, Any]] = {}
    for record in feishu.read_param_records(token):
        fields = record.get("fields") or {}
        sid = fields.get("source_id")
        if isinstance(sid, str) and sid:
            param_by_id[sid] = fields

    picked = stratified_sample(attributed, args.per_reason, args.seed)
    rows = [to_audit_row(item, param_by_id) for item in picked]
    for row, item in zip(rows, picked):
        row["_reason"] = item["_reason"]

    reason_totals = {
        REASON_LABELS.get(r, r): n
        for r, n in reasons.items()
        if r not in {"kept", "unknown"} and r not in STATEFUL_STAGES
    }
    meta = {
        "fetched_at": payload["fetched_at"],
        "raw_total": len(items),
        "attributed": len(attributed),
        "per_reason": args.per_reason,
        "seed": args.seed,
        "reason_totals": reason_totals,
        **getattr(attribute, "stats", {"plain_total": 0, "paper_total": 0, "paper_sampled": 0}),
    }

    write_html(rows, meta)
    for row in rows:
        row.pop("_reason", None)
    write_csv(rows)

    print(f"\n抽样 {len(rows)} 条待判读，覆盖 {len(reason_totals)} 个淘汰原因")
    print(f"{CSV_PATH.relative_to(ROOT)}")
    print(f"{HTML_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
