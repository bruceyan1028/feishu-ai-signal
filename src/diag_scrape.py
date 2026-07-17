"""Scrape 源诊断采集：只跑 fetch_method=Scrape，记录分源漏斗与耗时。

用法：
  python -m src.diag_scrape [--write] [--limit N] [--source-id id]
                            [--out output/scrape-pipeline-diag.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, feishu, process, scrape, sources, typed_config as tcfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diag_scrape")

REASON_LABELS = {
    "list_empty_or_failed": "列表页抓取失败/空",
    "no_links_extracted": "未抽出文章链接",
    "articles_failed": "正文抓取失败",
    "missing_title_url": "缺标题或链接",
    "lookback": "超出回看窗口",
    "min_content_chars": "正文过短",
    "keyword_regex": "未命中关键词 regex",
    "per_feed_cap": "单源条数上限",
    "keyword_include": "未命中必含关键词",
    "keyword_exclude": "命中排除关键词",
    "min_chars": "摘要过短",
    "min_signal_score": "信号分不足",
    "min_quality_score": "质量分不足",
    "require_community_heat": "无社区热度（arXiv 长尾丢弃）",
    "dup_round": "本轮去重",
    "dup_existing": "飞书已存在",
}


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
    # config 模块在 import 时已固化 env；此处刷新诊断相关字段
    config.FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
    config.FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
    config.FEISHU_BASE_ID = os.environ.get("FEISHU_BASE_ID", config.FEISHU_BASE_ID).strip() or config.FEISHU_BASE_ID
    config.FEISHU_PARAM_TABLE_ID = (
        os.environ.get("FEISHU_PARAM_TABLE_ID", config.FEISHU_PARAM_TABLE_ID).strip()
        or config.FEISHU_PARAM_TABLE_ID
    )
    config.FEISHU_ENTRY_TABLE_ID = (
        os.environ.get("FEISHU_ENTRY_TABLE_ID", config.FEISHU_ENTRY_TABLE_ID).strip()
        or config.FEISHU_ENTRY_TABLE_ID
    )
    config.JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()
    config.PAPER_ENRICH_ENABLED = os.environ.get("PAPER_ENRICH_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _require_jina() -> None:
    if not config.JINA_API_KEY:
        log.warning(
            "未配置 JINA_API_KEY，将使用 Jina 匿名调用（易限流）。"
            "建议在 .env 中设置 JINA_API_KEY 以提高稳定性。"
        )


def run(
    *,
    write: bool,
    out_path: Path,
    limit: int | None,
    source_ids: list[str] | None,
    engine: str = "auto",
) -> dict[str, Any]:
    _load_dotenv()
    _require_jina()

    # 诊断默认关闭论文外网富集，避免拖死
    if "PAPER_ENRICH_ENABLED" not in os.environ:
        os.environ["PAPER_ENRICH_ENABLED"] = "0"
        config.PAPER_ENRICH_ENABLED = False

    run_t0 = time.perf_counter()
    config.validate()
    token = feishu.get_tenant_access_token()

    t_setup = time.perf_counter()
    records = feishu.read_param_records(token)
    type_configs = tcfg.load_typed_configs(token)
    feeds = sources.map_scrape_sources_for_diag(records, include_b_class=True, allow_experimental=True)
    if source_ids:
        want = {s.strip() for s in source_ids if s.strip()}
        feeds = [f for f in feeds if f.get("id") in want]
    if limit is not None and limit > 0:
        feeds = feeds[:limit]

    for feed in feeds:
        cfg = type_configs.get(feed.get("id") or "") or {}
        if cfg.get("entity_type"):
            feed["source_type"] = sources.infer_signal_format(
                feed.get("id") or "",
                endpoint=feed.get("url") or "",
                extra=feed.get("extra_config"),
                fetch_method="Scrape",
                entity_type=cfg.get("entity_type"),
                explicit_type=feed.get("source_type"),
            )
        # GitHub 热榜：把配置表参数并入 feed，供抓取层纯沉淀型选仓/打分
        if cfg.get("entity_type") == "github":
            feed["github_config"] = cfg.get("params") or {}
        feed["cohort"] = sources.scrape_cohort(
            str(feed.get("id") or ""),
            category=str(feed.get("category") or ""),
            url=str(feed.get("url") or ""),
        )

    setup_ms = (time.perf_counter() - t_setup) * 1000
    log.info(
        "Scrape 诊断源 %d 个（含 B 类）：%s",
        len(feeds),
        [f["id"] for f in feeds],
    )

    t_fetch = time.perf_counter()
    raw, fetch_stats = scrape.fetch_scrape_sources_with_stats(feeds, engine=engine)
    fetch_ms = (time.perf_counter() - t_fetch) * 1000
    log.info("Scrape 原始条目 %d", len(raw))
    resolved_engine = next((v.get("engine") for v in fetch_stats.values()), engine)

    t_clean = time.perf_counter()
    # 正式清洗路径（论文富集已默认关）
    drop_stats: dict[str, int] = {}
    kept = process.process_and_clean(raw, type_configs, drop_stats)
    clean_ms = (time.perf_counter() - t_clean) * 1000

    # 从 funnel 日志外，自行按源统计清洗结果
    # process 不返回 drops；用 kept 对比 raw 做近似：按 source_id 计数
    raw_by = Counter(str((it.get("feed") or {}).get("id") or "") for it in raw)
    kept_by = Counter(str(it.get("source_id") or "") for it in kept)

    t_dedup = time.perf_counter()
    existing = feishu.read_existing_dedup_keys(token)
    after_dedup: list[dict[str, Any]] = []
    dup_existing_by: Counter[str] = Counter()
    for item in kept:
        key = str(item.get("duplicate_key") or "")
        if key and key in existing:
            dup_existing_by[str(item.get("source_id") or "")] += 1
            continue
        after_dedup.append(item)
    dedup_ms = (time.perf_counter() - t_dedup) * 1000

    wrote = 0
    write_ms = 0.0
    if write:
        t_write = time.perf_counter()
        if after_dedup:
            fields_list = [process.format_for_feishu(item) for item in after_dedup]
            wrote = feishu.batch_create_records(token, fields_list)
            write_ms = (time.perf_counter() - t_write) * 1000
            log.info("已写入飞书 %d 条", wrote)
        # 无论是否写入 0 条，都回写采集统计
        try:
            feishu.sync_param_collect_stats(
                token,
                records,
                {str(f.get("id") or "") for f in feeds},
                kept,
                after_dedup,
                drop_stats,
            )
        except feishu.FeishuError as exc:
            log.warning("回写源采集统计失败: %s", exc)
        if not after_dedup:
            write_ms = (time.perf_counter() - t_write) * 1000
            log.info("本轮无新条目可写，已回写采集统计")

    # 分源汇总
    sources_summary: list[dict[str, Any]] = []
    cohort_agg: dict[str, dict[str, int]] = {}
    for feed in feeds:
        sid = str(feed["id"])
        fs = fetch_stats.get(sid) or {}
        cohort = str(feed.get("cohort") or "其它")
        raw_n = raw_by.get(sid, 0)
        kept_n = kept_by.get(sid, 0)
        dup_n = dup_existing_by.get(sid, 0)
        new_n = max(0, kept_n - dup_n)
        # after_dedup count for this source
        ingest_n = sum(1 for it in after_dedup if it.get("source_id") == sid)
        row = {
            "source_id": sid,
            "name": feed.get("name"),
            "status": feed.get("status"),
            "cohort": cohort,
            "b_class": bool(feed.get("b_class")),
            "source_type": feed.get("source_type"),
            "category": feed.get("category"),
            "url": feed.get("url"),
            "list_ok": bool(fs.get("list_ok")),
            "list_chars": int(fs.get("list_chars") or 0),
            "links": int(fs.get("links") or 0),
            "articles_built": int(fs.get("article_ok") or 0),
            "articles_failed": int(fs.get("article_fail") or 0),
            "raw": raw_n,
            "kept_clean": kept_n,
            "dup_existing": dup_n,
            "new_after_dedup": ingest_n,
            "fetch_error": fs.get("error"),
            "timing_ms": fs.get("timing_ms") or {"list": 0, "articles": 0, "total": 0},
        }
        sources_summary.append(row)
        bucket = cohort_agg.setdefault(
            cohort,
            {
                "sources": 0,
                "list_ok": 0,
                "with_links": 0,
                "with_articles": 0,
                "kept_clean": 0,
                "new_after_dedup": 0,
            },
        )
        bucket["sources"] += 1
        bucket["list_ok"] += int(row["list_ok"])
        bucket["with_links"] += int(row["links"] > 0)
        bucket["with_articles"] += int(row["articles_built"] > 0)
        bucket["kept_clean"] += kept_n
        bucket["new_after_dedup"] += ingest_n

    # 失败原因分布（抓取层）
    fetch_errors = Counter(
        str(s.get("fetch_error") or "ok") for s in sources_summary if s.get("fetch_error")
    )

    total_ms = (time.perf_counter() - run_t0) * 1000
    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "config": {
            "engine": resolved_engine,
            "engine_requested": engine,
            "JINA_CONCURRENCY": config.JINA_CONCURRENCY,
            "DEFAULT_MAX_ARTICLES": config.DEFAULT_MAX_ARTICLES,
            "PAPER_ENRICH_ENABLED": config.PAPER_ENRICH_ENABLED,
            "wrote_to_feishu": write,
            "wrote_count": wrote,
            "limit": limit,
            "source_id_filter": source_ids or [],
        },
        "timing": {
            "total_ms": round(total_ms, 1),
            "setup_ms": round(setup_ms, 1),
            "fetch_ms": round(fetch_ms, 1),
            "clean_ms": round(clean_ms, 1),
            "dedup_ms": round(dedup_ms, 1),
            "write_ms": round(write_ms, 1),
        },
        "funnel": {
            "sources": len(feeds),
            "list_ok": sum(1 for s in sources_summary if s["list_ok"]),
            "with_links": sum(1 for s in sources_summary if s["links"] > 0),
            "articles_built": sum(s["articles_built"] for s in sources_summary),
            "rss_raw_equiv": len(raw),
            "after_clean": len(kept),
            "after_dedup": len(after_dedup),
            "wrote": wrote,
        },
        "cohorts": [
            {"cohort": k, **v} for k, v in sorted(cohort_agg.items(), key=lambda x: -x[1]["sources"])
        ],
        "fetch_error_totals": [
            {"reason": r, "label": REASON_LABELS.get(r, r), "count": c}
            for r, c in fetch_errors.most_common()
        ],
        "sources": sources_summary,
        "sample_kept": [
            {
                "title": it.get("title"),
                "url": it.get("url"),
                "source_id": it.get("source_id"),
                "source_type": it.get("source_type"),
            }
            for it in after_dedup[:30]
        ],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("诊断报告写入 %s（总耗时 %.1fs）", out_path, total_ms / 1000)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape 源诊断采集（不跑 RSS）")
    parser.add_argument("--write", action="store_true", help="将去重后条目写入飞书")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个源（0=全部）")
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="只跑指定 source_id，可重复",
    )
    parser.add_argument(
        "--engine",
        default="auto",
        choices=("auto", "jina", "direct"),
        help="抓取引擎：auto=探测 Jina，不可达则 direct",
    )
    parser.add_argument(
        "--out",
        default="output/scrape-pipeline-diag.json",
        help="诊断 JSON 输出路径",
    )
    args = parser.parse_args()
    run(
        write=args.write,
        out_path=Path(args.out),
        limit=args.limit or None,
        source_ids=args.source_id or None,
        engine=args.engine,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
