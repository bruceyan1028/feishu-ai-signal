"""诊断 active/experimental 播客源，可选写入完整摘要条目。"""
from __future__ import annotations

import argparse
import json
import logging

from . import config, feishu, main, podcast, process, sources

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diag-podcast")


def run(source_id: str = "", *, write: bool = False) -> int:
    config.validate()
    token = feishu.get_tenant_access_token()
    feishu.ensure_entry_enrichment_fields(token)
    records = feishu.read_param_records(token)
    feeds = sources.map_podcast_sources(records, allow_experimental=True)
    if source_id:
        feeds = [feed for feed in feeds if feed.get("id") == source_id]
    if not feeds:
        log.error("没有可诊断的 Podcast 源（需 active/experimental 且 endpoint 为 RSS）")
        return 2

    raw_items = podcast.fetch_podcast_sources(feeds)
    drop_stats: dict[str, int] = {}
    cleaned = process.process_and_clean(raw_items, drop_stats=drop_stats)
    existing = feishu.read_existing_dedup_keys(token)
    candidates = main.filter_new_items(cleaned, existing)
    enriched, enrich_stats = podcast.enrich_podcast_items(candidates)

    if write and enriched:
        feishu.batch_create_records(
            token,
            [process.format_for_feishu(item) for item in enriched],
        )
    feishu.sync_param_collect_stats(
        token,
        records,
        {str(feed.get("id") or "") for feed in feeds},
        cleaned,
        enriched,
        drop_stats,
    )

    payload = {
        "write": write,
        "sources": [feed.get("id") for feed in feeds],
        "raw": len(raw_items),
        "cleaned": len(cleaned),
        "new": len(candidates),
        "enriched": len(enriched),
        "enrichment": enrich_stats,
        "samples": [
            {
                "source_id": item.get("source_id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "transcript_source": (item.get("metrics") or {}).get("transcript_source"),
                "summary": (item.get("podcast_analysis") or {}).get("summary_cn"),
            }
            for item in enriched[:10]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if enriched or not candidates else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="诊断播客 RSS、转录与完整摘要")
    parser.add_argument("--source-id", default="", help="只诊断指定 source_id")
    parser.add_argument("--write", action="store_true", help="把诊断通过的新 episode 写入条目表")
    args = parser.parse_args()
    raise SystemExit(run(args.source_id, write=args.write))
