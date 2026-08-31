"""只读诊断 X 源或单个账号，不写条目或推进 cursor。"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from . import config, feishu, social, sources, typed_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diag-social")


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
    config.LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
    config.LLM_BASE_URL = (os.environ.get("LLM_BASE_URL") or config.LLM_BASE_URL).strip()
    config.LLM_MODEL = (os.environ.get("LLM_MODEL") or config.LLM_MODEL).strip()


def _kept_payload(item: dict) -> dict:
    metrics = item.get("metrics") or {}
    return {
        "url": item.get("url"),
        "gate": metrics.get("gate"),
        "score": metrics.get("social_score"),
        "own_chars": metrics.get("own_chars"),
        "is_quote": metrics.get("is_quote"),
        "has_media": metrics.get("has_media"),
        "has_article": metrics.get("has_article"),
        "event_and_evidence": metrics.get("event_and_evidence"),
        "core_fact": metrics.get("llm_core_fact") or "",
        "title": item.get("title"),
    }


def run_username(username: str, lookback_hours: int) -> int:
    _load_dotenv()
    kept, funnel, batch = social.preview_account(username, lookback_hours=lookback_hours)
    dropped = [
        {
            "url": item.get("url"),
            "reason": (item.get("metrics") or {}).get("drop_reason"),
            "title": item.get("title"),
            "own_chars": (item.get("metrics") or {}).get("own_chars"),
            "is_quote": (item.get("metrics") or {}).get("is_quote"),
            "core_fact": (item.get("metrics") or {}).get("llm_core_fact") or "",
            "confidence": (item.get("metrics") or {}).get("llm_confidence"),
        }
        for item in batch.items
        if (item.get("metrics") or {}).get("drop_reason")
    ]
    payload = {
        "account": username.lstrip("@").lower(),
        "lookback_hours": lookback_hours,
        "successful_accounts": sorted(batch.successful_accounts),
        "read_counts": batch.read_counts,
        "funnel": funnel,
        "urls": [item.get("url") for item in kept],
        "kept": [_kept_payload(item) for item in kept],
        "dropped": dropped,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if batch.successful_accounts else 1


def run(source_id: str = "") -> int:
    _load_dotenv()
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
        "urls": [item.get("url") for item in kept],
        "samples": [_kept_payload(item) for item in kept[:20]],
        "cursor_preview": batch.cursor_states,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if batch.successful_accounts else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="诊断 X 白名单采集与筛选漏斗（只读）")
    parser.add_argument("--source-id", default="", help="只诊断指定 source_id")
    parser.add_argument("--username", default="", help="只回放一个账号，不读飞书")
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=2160,
        help="单账号回放窗口，默认 2160=90 天",
    )
    args = parser.parse_args()
    if args.username:
        raise SystemExit(run_username(args.username, args.lookback_hours))
    raise SystemExit(run(args.source_id))
