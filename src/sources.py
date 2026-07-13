"""解析飞书「源配置表」记录 → 内部 feed 对象。

对应 n8n 节点：Map Feed Sources / Map Scrape Sources
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import config

FEED_METHODS = {"RSS"}
_B_SET = {"chatbot-arena", "artificial-analysis", "papers-with-code-sota"}


def cell(value: Any) -> Any:
    """对应各 Code 节点里的 cell()：把飞书字段值归一成标量。"""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        if not value:
            return None
        x = value[0]
        if isinstance(x, dict) and "text" in x:
            return x["text"]
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or value.get("link")
    return str(value)


def parse_lookback_hours(raw: Any) -> int:
    if not raw:
        return 168
    s = str(raw).strip().lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    if m:
        return int(float(m.group(1)))
    if "每日" in s or s == "24h":
        return 24
    if "每周" in s:
        return 168
    return 168


def normalize_endpoint(endpoint: Any) -> str:
    url = str(endpoint or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    return url


def _parse_extra(f: dict[str, Any]) -> dict[str, Any] | None:
    try:
        raw = cell(f.get("extra_config"))
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


def _infer_feed_source_type(source_id: str, dimension: str, extra: Any, fetch_method: str) -> str:
    if fetch_method == "Bridge" or "wechat" in str(source_id):
        return "Social"
    if (extra or {}).get("entity_type") == "paper" or str(source_id).startswith("arxiv-"):
        return "Research"
    if "github" in str(source_id):
        return "Tech News"
    if dimension == "技术研究开源":
        return "Research"
    return "Company Blog"


def _infer_scrape_source_type(dimension: str) -> str:
    if dimension == "技术研究开源":
        return "Research"
    if dimension and "政策" in dimension:
        return "Policy"
    if dimension and "产品化" in dimension:
        return "Product"
    return "Company Blog"


def _is_active(f: dict[str, Any]) -> bool:
    status = cell(f.get("status"))
    if status and status != "active":
        return False
    if f.get("n8n_enabled") is False:
        return False
    return True


def _base_feed(f: dict[str, Any], extra: dict[str, Any] | None, fetch_method: str) -> dict[str, Any]:
    source_id = cell(f.get("source_id")) or ""
    dimension = cell(f.get("dimension")) or ""
    tier_raw = cell(f.get("tier")) or "L2"
    min_from_extra = (extra or {}).get("min_abstract_chars")
    return {
        "id": source_id,
        "name": cell(f.get("name")) or source_id,
        "url": normalize_endpoint(cell(f.get("endpoint"))),
        "category": dimension,
        "tier": config.TIER_LABEL.get(tier_raw, tier_raw),
        "fetch_method": fetch_method,
        "priority": cell(f.get("priority")) or "P1",
        "lookback_hours": parse_lookback_hours(cell(f.get("lookback_window"))),
        "keyword_regex": cell(f.get("keyword_regex")) or config.DEFAULT_KEYWORD,
        "dedup_key": cell(f.get("dedup_key")) or "normalize(url)",
        "extra_config": extra,
        "_min_from_extra": min_from_extra,
        "_source_id": source_id,
        "_dimension": dimension,
    }


def map_feed_sources(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """筛出已启用的 RSS 来源。Demo 不采集 Bridge、API 或 Scrape。"""
    out: list[dict[str, Any]] = []
    for rec in records:
        f = rec.get("fields") or {}
        fetch_method = cell(f.get("fetch_method"))
        if fetch_method not in FEED_METHODS:
            continue
        if not _is_active(f):
            continue
        extra = _parse_extra(f)
        feed = _base_feed(f, extra, fetch_method)
        if not feed["url"]:
            continue
        feed["source_type"] = _infer_feed_source_type(
            feed["_source_id"], feed["_dimension"], extra, fetch_method
        )
        feed["min_content_chars"] = (
            int(cell(f.get("min_content_chars")) or 0)
            or int(feed["_min_from_extra"] or 0)
            or (200 if feed["_source_id"].startswith("arxiv-") else 100)
        )
        out.append(feed)
    return out


def _is_b_class(f: dict[str, Any]) -> bool:
    ex = str(cell(f.get("extra_config")) or "").replace(" ", "").replace("\n", "").replace("\t", "")
    if '"snapshot_mode":true' in ex or '"diff_mode":true' in ex:
        return True
    if cell(f.get("source_id")) in _B_SET:
        return True
    return False


def map_scrape_sources(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对应 Map Scrape Sources：筛出 Scrape 取值来源（排除 B 类榜单快照）。"""
    out: list[dict[str, Any]] = []
    for rec in records:
        f = rec.get("fields") or {}
        if cell(f.get("fetch_method")) != "Scrape":
            continue
        if _is_b_class(f):
            continue
        if not _is_active(f):
            continue
        extra = _parse_extra(f)
        feed = _base_feed(f, extra, "Scrape")
        if not feed["url"]:
            continue
        feed["source_type"] = _infer_scrape_source_type(feed["_dimension"])
        feed["min_content_chars"] = (
            int(cell(f.get("min_content_chars")) or 0)
            or int(feed["_min_from_extra"] or 0)
            or 100
        )
        feed["max_articles"] = int((extra or {}).get("max_articles") or 0) or config.DEFAULT_MAX_ARTICLES
        out.append(feed)
    return out
