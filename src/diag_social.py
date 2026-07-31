"""只读诊断 experimental/active X 源，不写条目或推进 cursor。"""
from __future__ import annotations

import argparse
import json
import logging

from . import config, feishu, social, sources, typed_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diag-social")


def run(source_id: str = "") -> int:
    config.validate()
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    typed = typed_config.load_typed_configs(token)
    social_params = {
        sid: cfg.get("params") or {}
        for sid, cfg in typed.items()
        if cfg.get("entity_type") == "social"
    }
    feeds = sources.map_social_sources(
        records,
        social_params,
        allow_experimental=True,
    )
    if source_id:
        feeds = [feed for feed in feeds if feed.get("id") == source_id]
    if not feeds:
        log.error("没有可诊断的 Social 源（需 active/experimental 且账号白名单非空）")
        return 2
    batch = social.fetch_social_sources(feeds)
    recent = feishu.read_all_records(
        token,
        config.FEISHU_ENTRY_TABLE_ID,
        ["标题", "原文", "来源", "发布时间"],
    )
    texts, counts = social.recent_context(recent)
    kept, funnel = social.filter_social_items(
        batch.items,
        recent_texts=texts,
        existing_account_counts=counts,
    )
    payload = {
        "sources": [feed.get("id") for feed in feeds],
        "accounts": sum(len(feed.get("accounts") or []) for feed in feeds),
        "successful_accounts": sorted(batch.successful_accounts),
        "read_counts": batch.read_counts,
        "funnel": funnel,
        "samples": [
            {
                "account": (item.get("metrics") or {}).get("account"),
                "score": (item.get("metrics") or {}).get("social_score"),
                "title": item.get("title"),
                "url": item.get("url"),
            }
            for item in kept[:20]
        ],
        "cursor_preview": batch.cursor_states,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if batch.successful_accounts else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="诊断 X 白名单采集与筛选漏斗（只读）")
    parser.add_argument("--source-id", default="", help="只诊断指定 source_id")
    args = parser.parse_args()
    raise SystemExit(run(args.source_id))
