"""单源时间窗试跑：拉列表→正文→清洗，看扩窗后能留下多少条。

默认测 anthropic-news、近 30 天。会抬高 max_articles，避免列表截断先于 lookback。
不写飞书。抓取一次，可对比「表内原窗」vs「试跑窗」。

用法：
  python -m tools.probe_source_window
  python -m tools.probe_source_window --source-id anthropic-news --lookback-hours 720
  python -m tools.probe_source_window --seed --max-articles 60 --engine auto
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, config_store, feishu, health, process, scrape, sources, typed_config as tcfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("probe_source_window")


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
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
    config.FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
    config.FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
    config.FEISHU_BASE_ID = (
        os.environ.get("FEISHU_BASE_ID", config.FEISHU_BASE_ID).strip() or config.FEISHU_BASE_ID
    )
    config.JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()


def _load_feed(source_id: str, *, use_seed: bool) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    if use_seed:
        records = config_store.read_param_records()
        type_configs = config_store.load_typed_configs()
        origin = "seed"
    else:
        config.validate()
        token = feishu.get_tenant_access_token()
        records = feishu.read_param_records(token)
        type_configs = tcfg.load_typed_configs(token)
        origin = "feishu"

    feeds = sources.map_scrape_sources_for_diag(
        records, include_b_class=True, allow_experimental=True
    )
    matched = [f for f in feeds if f.get("id") == source_id]
    if not matched:
        raise SystemExit(f"找不到 Scrape 源 {source_id!r}（来自 {origin}）")
    feed = matched[0]
    cfg = type_configs.get(source_id) or {}
    if cfg.get("entity_type"):
        feed["source_type"] = sources.infer_signal_format(
            source_id,
            endpoint=feed.get("url") or "",
            extra=feed.get("extra_config"),
            fetch_method="Scrape",
            entity_type=cfg.get("entity_type"),
            explicit_type=feed.get("source_type"),
        )
    return feed, type_configs, origin


def _format_published(item: dict[str, Any]) -> tuple[str, bool]:
    """清洗后条目通常只剩 published_ms；raw 侧还有 published_raw。"""
    raw = item.get("published_raw")
    if raw:
        return str(raw), process.is_date_only_published(raw)
    ms = item.get("published_ms") or item.get("published")
    if isinstance(ms, (int, float)) and ms > 0:
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            False,
        )
    return "", False


def _summarize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        published, date_only = _format_published(item)
        rows.append(
            {
                "title": (item.get("title") or "")[:120],
                "url": item.get("url") or "",
                "published_raw": published,
                "chars": len(str(item.get("body") or item.get("content") or "")),
                "date_only": date_only,
            }
        )
    return rows


def _clean(
    raw: list[dict[str, Any]],
    type_configs: dict[str, dict[str, Any]],
    lookback_hours: int,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """对同一批 raw 改 feed.lookback_hours 后清洗，返回 kept + 淘汰计数。"""
    adjusted: list[dict[str, Any]] = []
    for item in raw:
        feed = dict(item.get("feed") or {})
        feed["lookback_hours"] = lookback_hours
        clone = dict(item)
        clone["feed"] = feed
        adjusted.append(clone)
    funnel = health.Funnel()
    kept = process.process_and_clean(adjusted, type_configs, funnel_out=funnel)
    drops = Counter(funnel.drops())
    drops["kept"] = len(kept)
    drops["raw"] = len(adjusted)
    return kept, drops


def run(
    *,
    source_id: str,
    lookback_hours: int,
    max_articles: int,
    engine: str,
    use_seed: bool,
    out_path: Path | None,
) -> dict[str, Any]:
    _load_dotenv()
    if "PAPER_ENRICH_ENABLED" not in os.environ:
        os.environ["PAPER_ENRICH_ENABLED"] = "0"
        config.PAPER_ENRICH_ENABLED = False

    feed, type_configs, origin = _load_feed(source_id, use_seed=use_seed)
    configured_lookback = int(feed.get("lookback_hours") or config.MIN_LOOKBACK_HOURS)
    configured_max = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)

    probe_feed = dict(feed)
    probe_feed["lookback_hours"] = lookback_hours
    probe_feed["max_articles"] = max(max_articles, configured_max)

    log.info(
        "源=%s origin=%s url=%s | 表内 lookback=%dh max_articles=%d → 试跑 %dh / %d",
        source_id,
        origin,
        probe_feed.get("url"),
        configured_lookback,
        configured_max,
        lookback_hours,
        probe_feed["max_articles"],
    )

    raw, fetch_stats = scrape.fetch_scrape_sources_with_stats([probe_feed], engine=engine)
    st = fetch_stats.get(source_id) or {}
    log.info(
        "抓取 engine=%s links=%s article_ok=%s article_fail=%s error=%s",
        st.get("engine"),
        st.get("links"),
        st.get("article_ok"),
        st.get("article_fail"),
        st.get("error"),
    )

    kept_probe, drops_probe = _clean(raw, type_configs, lookback_hours)
    kept_cfg, drops_cfg = _clean(raw, type_configs, configured_lookback)

    # 清洗后丢了 published_raw，用 raw 侧日期回填展示
    raw_by_url = {
        process.normalize_url(it.get("url")): it for it in raw if it.get("url")
    }

    def _kept_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for item in items:
            src = raw_by_url.get(process.normalize_url(item.get("url"))) or item
            published, date_only = _format_published(src)
            rows.append(
                {
                    "title": (item.get("title") or src.get("title") or "")[:120],
                    "url": item.get("url") or "",
                    "published_raw": published,
                    "chars": len(str(item.get("body") or item.get("content") or "")),
                    "date_only": date_only,
                }
            )
        return rows

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_id": source_id,
        "origin": origin,
        "url": probe_feed.get("url"),
        "engine": st.get("engine"),
        "fetch": {
            "links": st.get("links"),
            "article_ok": st.get("article_ok"),
            "article_fail": st.get("article_fail"),
            "error": st.get("error"),
            "timing_ms": st.get("timing_ms"),
        },
        "configured": {
            "lookback_hours": configured_lookback,
            "max_articles": configured_max,
            "funnel": dict(drops_cfg),
            "kept_count": len(kept_cfg),
            "kept": _kept_rows(kept_cfg),
        },
        "probe": {
            "lookback_hours": lookback_hours,
            "max_articles": probe_feed["max_articles"],
            "funnel": dict(drops_probe),
            "kept_count": len(kept_probe),
            "kept": _kept_rows(kept_probe),
        },
        "raw_preview": _summarize_items(raw),
    }

    print()
    print(f"=== {source_id} 时间窗试跑 ===")
    print(f"列表链接 {st.get('links')} · 正文成功 {st.get('article_ok')} · 失败 {st.get('article_fail')}")
    if int(st.get("links") or 0) < probe_feed["max_articles"]:
        print(
            f"注意：列表只抽出 {st.get('links')} 条（max_articles={probe_feed['max_articles']}），"
            "多半是首页可见条目上限，不是时间窗卡掉的。"
        )
    print(
        f"表内 {configured_lookback}h：保留 {len(kept_cfg)}  "
        f"lookback淘汰 {drops_cfg.get('lookback', 0)}"
    )
    print(
        f"试跑 {lookback_hours}h：保留 {len(kept_probe)}  "
        f"lookback淘汰 {drops_probe.get('lookback', 0)}"
    )
    if drops_probe:
        other = {k: v for k, v in drops_probe.items() if k not in {"raw", "kept", "lookback"} and v}
        if other:
            print(f"试跑其它淘汰：{other}")
    print()
    print(f"--- 试跑窗保留 {len(kept_probe)} 条 ---")
    for i, row in enumerate(report["probe"]["kept"], 1):
        mark = " [date-only]" if row["date_only"] else ""
        print(f"{i:2d}. {row['published_raw']:<16} {row['title']}{mark}")
        print(f"    {row['url']}")

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已写 %s", out_path)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="单源扩窗试跑（不写飞书）")
    parser.add_argument("--source-id", default="anthropic-news")
    parser.add_argument("--lookback-hours", type=int, default=720, help="试跑时间窗，默认 720=30天")
    parser.add_argument(
        "--max-articles",
        type=int,
        default=50,
        help="列表最多抽几条（需 ≥ 窗内条数，默认 50）",
    )
    parser.add_argument("--engine", default="auto", choices=["auto", "jina", "direct"])
    parser.add_argument("--seed", action="store_true", help="不读飞书，用 seed_default.json")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "output" / "probe-source-window.json",
    )
    parser.add_argument("--no-out", action="store_true")
    args = parser.parse_args()
    run(
        source_id=args.source_id.strip(),
        lookback_hours=max(1, args.lookback_hours),
        max_articles=max(1, args.max_articles),
        engine=args.engine,
        use_seed=args.seed,
        out_path=None if args.no_out else args.out,
    )


if __name__ == "__main__":
    main()
