"""论文源诊断采集：只跑 arXiv/论文类型，记录每环节过滤原因，可选写入飞书。

用法：
  python -m src.diag_paper [--write] [--out output/paper-pipeline-diag.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, feishu, paper_enrich, process, rss, sources, typed_config as tcfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diag_paper")

REASON_LABELS = {
    "missing_title_url": "缺标题或链接",
    "lookback": "超出回看窗口",
    "min_content_chars": "正文过短（参数表）",
    "keyword_regex": "未命中关键词 regex",
    "per_feed_cap": "单源条数上限",
    "keyword_include": "未命中必含关键词",
    "keyword_exclude": "命中排除关键词",
    "min_chars": "摘要过短（论文配置）",
    "venue_whitelist": "录用场馆不在白名单",
    "venue_blacklist": "命中期刊/会议黑名单",
    "min_signal_score": "信号分不足",
    "min_quality_score": "质量分不足",
    "min_community_heat": "社区热度不足",
    "require_community_heat": "无社区热度（arXiv 长尾丢弃）",
    "require_acceptance": "要求已录用但未解析到",
    "require_code": "要求代码仓库但未找到",
    "exclude_preprint": "排除纯预印本",
    "dup_round": "本轮去重",
    "dup_existing": "飞书已存在",
    "arxiv_cap": "arXiv 名额截断（质量分排序后）",
    "kept": "通过清洗",
    "ingested": "已入库",
    "brief_candidate": "简报候选",
}


def _safe_regex(pattern: Any) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.I) if pattern else re.compile(config.DEFAULT_KEYWORD, re.I)
    except re.error:
        return re.compile(config.DEFAULT_KEYWORD, re.I)


def diagnose_clean(
    raw_items: list[dict[str, Any]],
    type_configs: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """带原因的清洗。返回 (通过列表, 丢弃明细, 分源清洗耗时)。"""
    now = process.now_ms()
    seen: set[str] = set()
    per_feed: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    # source_id -> enrich/local timing
    source_timing: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "enrich_calls": 0,
            "enrich_ms": 0.0,
            "local_filter_ms": 0.0,
            "items_seen": 0,
        }
    )

    def drop(item: dict, reason: str, extra: dict | None = None) -> None:
        feed = item.get("feed") or {}
        url = process.normalize_url(item.get("url"))
        title = process.strip_html(item.get("title"))
        row = {
            "reason": reason,
            "reason_label": REASON_LABELS.get(reason, reason),
            "source_id": feed.get("id") or "",
            "source": feed.get("name") or "",
            "title": title,
            "url": url,
        }
        if extra:
            row.update(extra)
        dropped.append(row)

    for item in raw_items:
        feed = item.get("feed") or {}
        feed_id = feed.get("id") or ""
        feed_key = feed_id or feed.get("url") or "0"
        st = source_timing[feed_id or feed_key]
        st["items_seen"] += 1
        item_t0 = time.perf_counter()
        enrich_delta_ms = 0.0
        feed_hits = per_feed.get(feed_key, 0)
        if feed_hits >= config.MAX_ITEMS_PER_FEED:
            drop(item, "per_feed_cap")
            st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000
            continue

        lookback_hours = int(feed.get("lookback_hours") or config.MIN_LOOKBACK_HOURS)
        lookback_ms = lookback_hours * 3600000
        keyword_re = _safe_regex(feed.get("keyword_regex"))
        min_chars = feed.get("min_content_chars") or 100

        url = process.normalize_url(item.get("url"))
        title = process.strip_html(item.get("title"))
        body_text = process.strip_html(item.get("body"))
        published_ms = process.parse_date_ms(item.get("published_raw"))
        duplicate_key = process.build_dedup_key(url, title, feed)
        combined = f"{title} {body_text}"

        if not title or not url:
            drop(item, "missing_title_url")
            st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000
            continue
        if published_ms is None or now - published_ms >= lookback_ms:
            drop(item, "lookback")
            st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000
            continue
        if len(combined) < min_chars:
            drop(item, "min_content_chars", {"value": len(combined), "threshold": min_chars})
            st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000
            continue
        if not keyword_re.search(combined):
            drop(item, "keyword_regex")
            st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000
            continue

        metrics: dict[str, Any] = dict(item.get("metrics") or {})
        type_cfg = type_configs.get(feed_id)
        is_paper = sources.is_paper_source(
            source_id=feed_id,
            source_type=str(feed.get("source_type") or ""),
            entity_type=(type_cfg or {}).get("entity_type"),
            endpoint=url,
            extra=feed.get("extra_config"),
        )
        quality_fields: dict[str, Any] = {}
        if is_paper:
            metrics.update(tcfg.infer_paper_metrics(title, body_text, url))
            params = (type_cfg or {}).get("params") or {}
            # 先用本地信号分硬过滤，减少外网富集次数
            min_sig = params.get("min_signal_score")
            if min_sig is not None and metrics.get("signal_score") is not None:
                if float(metrics["signal_score"]) < float(min_sig):
                    drop(
                        item,
                        "min_signal_score",
                        {"signal_score": metrics.get("signal_score"), "threshold": min_sig},
                    )
                    st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000
                    continue
            log.info("富集 %s | %s", feed_id, title[:60])
            enrich_t0 = time.perf_counter()
            enriched = paper_enrich.enrich_paper(
                url,
                signal_score=float(metrics.get("signal_score") or 50),
                venue_whitelist=params.get("venue_whitelist"),
                venue_blacklist=params.get("venue_blacklist"),
            )
            enrich_delta_ms = (time.perf_counter() - enrich_t0) * 1000
            st["enrich_calls"] += 1
            st["enrich_ms"] += enrich_delta_ms
            if enriched.get("accepted_venue"):
                metrics["is_preprint"] = False
            elif enriched.get("arxiv_id"):
                metrics["is_preprint"] = True
            for k in (
                "accepted_venue",
                "community_heat",
                "venue_score",
                "venue_reason",
                "quality_score",
            ):
                if enriched.get(k) is not None:
                    metrics[k] = enriched[k]
            quality_fields = {
                "quality_score": enriched.get("quality_score"),
                "accepted_venue": enriched.get("accepted_venue") or "",
                "community_heat": enriched.get("community_heat"),
                "paper_metrics_json": {
                    "arxiv_id": enriched.get("arxiv_id"),
                    "comment": enriched.get("arxiv_comment"),
                    "signal_score": metrics.get("signal_score"),
                    "quality_score": enriched.get("quality_score"),
                    "venue_reason": enriched.get("venue_reason"),
                    "accepted_venue": enriched.get("accepted_venue"),
                },
            }

        if type_cfg:
            keep, reason = tcfg.apply_typed_filter(
                type_cfg["entity_type"],
                type_cfg["params"],
                {"text": combined.lower(), "body_len": len(body_text), "metrics": metrics},
            )
            if not keep:
                drop(
                    item,
                    reason,
                    {
                        "signal_score": metrics.get("signal_score"),
                        "quality_score": metrics.get("quality_score"),
                        "accepted_venue": metrics.get("accepted_venue"),
                    },
                )
                st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000 - enrich_delta_ms
                continue

        if duplicate_key in seen:
            drop(item, "dup_round")
            st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000 - enrich_delta_ms
            continue
        seen.add(duplicate_key)
        per_feed[feed_key] = feed_hits + 1

        row = {
            "title": title,
            "url": url,
            "source": feed.get("name") or "Unknown",
            "source_id": feed_id,
            "source_type": feed.get("source_type") or sources.SIGNAL_FORMAT_PAPER,
            "fetch_method": feed.get("fetch_method") or "",
            "category": feed.get("category") or "",
            "tier": feed.get("tier") or "",
            "priority": feed.get("priority") or "P1",
            "published_ms": published_ms,
            "collected_ms": now,
            "raw_content": body_text[:15000],
            "image_url": str(item.get("image_url") or "").strip(),
            "media_assets": item.get("media_assets") or {"images": [], "videos": []},
            "topics": process.infer_topics(title, body_text),
            "duplicate_key": duplicate_key,
            "quality_score": float(quality_fields.get("quality_score") or 0),
            "signal_score": metrics.get("signal_score"),
            "accepted_venue": metrics.get("accepted_venue") or "",
            "keep_reason": "通过清洗：关键词+时间窗+质量门槛",
        }
        row.update(quality_fields)
        kept.append(row)
        st["local_filter_ms"] += (time.perf_counter() - item_t0) * 1000 - enrich_delta_ms

    return kept, dropped, dict(source_timing)


def run(write: bool, out_path: Path) -> dict[str, Any]:
    run_t0 = time.perf_counter()
    config.validate()
    token = feishu.get_tenant_access_token()
    feishu.ensure_entry_enrichment_fields(token)
    try:
        feishu.ensure_paper_config_fields(token)
    except feishu.FeishuError as exc:
        log.warning("论文配置字段: %s", exc)
    try:
        feishu.ensure_source_type_field(token, config.FEISHU_PARAM_TABLE_ID)
        feishu.ensure_source_type_field(token, config.FEISHU_SOURCE_TABLE_ID)
    except feishu.FeishuError as exc:
        log.warning("来源类型字段: %s", exc)

    t_setup = time.perf_counter()
    records = feishu.read_param_records(token)
    type_configs = tcfg.load_typed_configs(token)
    feeds = sources.map_feed_sources(records)
    paper_feeds = [
        f
        for f in feeds
        if sources.is_paper_source(
            source_id=str(f.get("id") or ""),
            source_type=str(f.get("source_type") or ""),
            entity_type=(type_configs.get(f.get("id") or {}) or {}).get("entity_type"),
            endpoint=str(f.get("url") or ""),
            extra=f.get("extra_config"),
        )
    ]
    for feed in paper_feeds:
        feed["source_type"] = sources.SIGNAL_FORMAT_PAPER

    setup_ms = (time.perf_counter() - t_setup) * 1000
    log.info(
        "论文 RSS 源 %d 个: %s",
        len(paper_feeds),
        [f["id"] for f in paper_feeds],
    )

    # 分源抓取，记录每源耗时与条数
    raw: list[dict[str, Any]] = []
    fetch_timing: dict[str, dict[str, Any]] = {}
    t_fetch = time.perf_counter()
    for feed in paper_feeds:
        sid = str(feed.get("id") or "")
        ft0 = time.perf_counter()
        items = rss.fetch_feed_sources([feed])
        elapsed_ms = (time.perf_counter() - ft0) * 1000
        fetch_timing[sid] = {
            "rss_ms": round(elapsed_ms, 1),
            "rss_entries": len(items),
            "lookback_hours": int(feed.get("lookback_hours") or 0),
            "error": None if items or elapsed_ms < 50 else None,
        }
        raw.extend(items)
        log.info("RSS %s → %d 条，耗时 %.0fms", sid, len(items), elapsed_ms)
    fetch_total_ms = (time.perf_counter() - t_fetch) * 1000
    log.info("RSS 原始合计 %d 条，抓取总耗时 %.0fms", len(raw), fetch_total_ms)

    by_source_raw = Counter(str((it.get("feed") or {}).get("id") or "") for it in raw)

    t_clean = time.perf_counter()
    kept, dropped, clean_timing = diagnose_clean(raw, type_configs)
    clean_total_ms = (time.perf_counter() - t_clean) * 1000
    log.info("清洗后 %d 条，丢弃 %d 条，清洗总耗时 %.0fms", len(kept), len(dropped), clean_total_ms)

    t_dedup = time.perf_counter()
    existing = feishu.read_existing_dedup_keys(token)
    after_dedup: list[dict[str, Any]] = []
    for item in kept:
        key = str(item.get("duplicate_key") or "")
        if key and key in existing:
            dropped.append(
                {
                    "reason": "dup_existing",
                    "reason_label": REASON_LABELS["dup_existing"],
                    "source_id": item.get("source_id"),
                    "source": item.get("source"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "quality_score": item.get("quality_score"),
                }
            )
            continue
        after_dedup.append(item)

    # 按质量分排序后截断（等同入库推送逻辑）
    after_dedup.sort(key=lambda it: (-float(it.get("quality_score") or 0), -int(it.get("published_ms") or 0)))
    ingested = after_dedup[: config.MAX_ARXIV_ITEMS]
    for item in after_dedup[config.MAX_ARXIV_ITEMS :]:
        dropped.append(
            {
                "reason": "arxiv_cap",
                "reason_label": REASON_LABELS["arxiv_cap"],
                "source_id": item.get("source_id"),
                "source": item.get("source"),
                "title": item.get("title"),
                "url": item.get("url"),
                "quality_score": item.get("quality_score"),
                "signal_score": item.get("signal_score"),
                "note": f"排名在 Top{config.MAX_ARXIV_ITEMS} 之外",
            }
        )

    for i, item in enumerate(ingested, 1):
        item["push_rank"] = i
        item["push_reason"] = (
            f"质量分 {item.get('quality_score')} 排名第 {i}/{len(after_dedup)}，"
            f"进入 MAX_ARXIV_ITEMS={config.MAX_ARXIV_ITEMS}；"
            f"signal={item.get('signal_score')} "
            f"venue={item.get('accepted_venue') or '无录用'}"
        )
    dedup_ms = (time.perf_counter() - t_dedup) * 1000

    wrote = 0
    write_ms = 0.0
    if write:
        t_write = time.perf_counter()
        if ingested:
            fields_list = [process.format_for_feishu(item) for item in ingested]
            wrote = feishu.batch_create_records(token, fields_list)
            log.info("已写入飞书 %d 条", wrote)
        # 无论是否写入 0 条，都回写采集统计
        try:
            feishu.sync_param_collect_stats(
                token,
                records,
                {f["id"] for f in paper_feeds},
                kept,
                ingested,
            )
        except feishu.FeishuError as exc:
            log.warning("回写统计失败: %s", exc)
        write_ms = (time.perf_counter() - t_write) * 1000

    # 按源汇总（含工作量与耗时）
    sources_summary = []
    for feed in paper_feeds:
        sid = feed["id"]
        raw_n = by_source_raw.get(sid, 0)
        drop_n = sum(1 for d in dropped if d.get("source_id") == sid)
        keep_n = sum(1 for k in kept if k.get("source_id") == sid)
        ingest_n = sum(1 for k in ingested if k.get("source_id") == sid)
        reasons = Counter(d["reason"] for d in dropped if d.get("source_id") == sid)
        ft = fetch_timing.get(sid) or {}
        ct = clean_timing.get(sid) or {}
        enrich_ms = float(ct.get("enrich_ms") or 0)
        local_ms = float(ct.get("local_filter_ms") or 0)
        rss_ms = float(ft.get("rss_ms") or 0)
        enrich_calls = int(ct.get("enrich_calls") or 0)
        total_ms = rss_ms + enrich_ms + local_ms
        sources_summary.append(
            {
                "source_id": sid,
                "name": feed.get("name"),
                "priority": feed.get("priority"),
                "lookback_hours": int(feed.get("lookback_hours") or 0),
                "raw": raw_n,
                "kept_clean": keep_n,
                "dropped": drop_n,
                "ingested": ingest_n,
                "drop_reasons": dict(reasons),
                "workload": {
                    "rss_entries": int(ft.get("rss_entries") or raw_n),
                    "items_seen": int(ct.get("items_seen") or raw_n),
                    "enrich_calls": enrich_calls,
                },
                "timing_ms": {
                    "rss": round(rss_ms, 1),
                    "local_filter": round(local_ms, 1),
                    "enrich": round(enrich_ms, 1),
                    "total": round(total_ms, 1),
                    "enrich_avg": round(enrich_ms / enrich_calls, 1) if enrich_calls else 0.0,
                },
            }
        )

    reason_totals = Counter(d["reason"] for d in dropped)
    # 每种原因保留最多 12 条样例，避免 canvas 体积爆炸
    drop_samples: dict[str, list[dict[str, Any]]] = {}
    for d in dropped:
        reason = str(d.get("reason") or "")
        bucket = drop_samples.setdefault(reason, [])
        if len(bucket) < 12:
            bucket.append(
                {
                    "title": d.get("title"),
                    "url": d.get("url"),
                    "source_id": d.get("source_id"),
                    "quality_score": d.get("quality_score"),
                    "signal_score": d.get("signal_score"),
                    "accepted_venue": d.get("accepted_venue"),
                    "note": d.get("note"),
                    "threshold": d.get("threshold"),
                    "value": d.get("value"),
                }
            )

    total_ms = (time.perf_counter() - run_t0) * 1000
    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "config": {
            "MAX_ARXIV_ITEMS": config.MAX_ARXIV_ITEMS,
            "PAPER_QUALITY_MIN_SCORE": config.PAPER_QUALITY_MIN_SCORE,
            "ARXIV_MIN_SIGNAL_SCORE": config.ARXIV_MIN_SIGNAL_SCORE,
            "PAPER_ENRICH_ENABLED": config.PAPER_ENRICH_ENABLED,
            "wrote_to_feishu": write,
            "wrote_count": wrote,
        },
        "timing": {
            "total_ms": round(total_ms, 1),
            "setup_ms": round(setup_ms, 1),
            "rss_fetch_ms": round(fetch_total_ms, 1),
            "clean_ms": round(clean_total_ms, 1),
            "dedup_ms": round(dedup_ms, 1),
            "write_ms": round(write_ms, 1),
            "enrich_ms": round(sum(float((clean_timing.get(s) or {}).get("enrich_ms") or 0) for s in fetch_timing), 1),
            "enrich_calls": sum(int((clean_timing.get(s) or {}).get("enrich_calls") or 0) for s in fetch_timing),
        },
        "funnel": {
            "rss_raw": len(raw),
            "after_clean": len(kept),
            "after_dedup": len(after_dedup),
            "ingested": len(ingested),
            "dropped_total": len(dropped),
        },
        "sources": sources_summary,
        "drop_reason_totals": [
            {"reason": r, "label": REASON_LABELS.get(r, r), "count": c}
            for r, c in reason_totals.most_common()
        ],
        "ingested_items": [
            {
                "rank": it.get("push_rank"),
                "title": it.get("title"),
                "url": it.get("url"),
                "source_id": it.get("source_id"),
                "source": it.get("source"),
                "quality_score": it.get("quality_score"),
                "signal_score": it.get("signal_score"),
                "accepted_venue": it.get("accepted_venue"),
                "push_reason": it.get("push_reason"),
            }
            for it in ingested
        ],
        "drop_samples": drop_samples,
        "dropped_items": dropped[:200],
        "dropped_items_truncated": max(0, len(dropped) - 200),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("诊断报告写入 %s（总耗时 %.1fs）", out_path, total_ms / 1000)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="论文源诊断采集")
    parser.add_argument("--write", action="store_true", help="将入选条目写入飞书")
    parser.add_argument(
        "--out",
        default="output/paper-pipeline-diag.json",
        help="诊断 JSON 输出路径",
    )
    args = parser.parse_args()
    run(write=args.write, out_path=Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
