"""清洗、过滤、去重键、飞书字段组装。

对应 n8n 节点：Process and Clean / Format for Feishu
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as date_parser

from . import config

log = logging.getLogger(__name__)

_DEFAULT_KEYWORD_RE = re.compile(config.DEFAULT_KEYWORD, re.I)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def normalize_url(url: Any) -> str:
    raw = url if isinstance(url, str) else (url or "")
    s = str(raw).strip()
    if not s:
        return ""
    s = s.split("#")[0]
    s = re.sub(r"([?&])(utm_[^=&]*|ref)=[^&]*", r"\1", s, flags=re.I)
    s = re.sub(r"[?&]+$", "", s)
    s = re.sub(r"/+$", "", s)
    return s.lower()


def parse_date_ms(raw: Any) -> int | None:
    if not raw:
        return now_ms()
    try:
        dt = date_parser.parse(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError, TypeError):
        return now_ms()


def strip_html(text: Any) -> str:
    s = _TAG_RE.sub("", str(text or ""))
    return _WS_RE.sub(" ", s).strip()


def build_dedup_key(url: str, title: str, feed: dict[str, Any]) -> str:
    strategy = feed.get("dedup_key") or "normalize(url)"
    if "arxiv_id" in strategy:
        m = re.search(r"arxiv\.org/abs/([^/?#]+)", url, re.I)
        if m:
            arxiv_id = re.sub(r"v\d+$", "", m.group(1), flags=re.I)
            return f"arxiv:{arxiv_id}"
    if "hash(model" in strategy:
        return f"release:{str(title or url).lower()}"[:240]
    return url


def infer_topics(title: str, summary: str) -> list[str]:
    text = f"{title} {summary}".lower()
    topics: list[str] = []
    if re.search(r"\b(ai|artificial intelligence)\b", text):
        topics.append("AI")
    if re.search(r"\bllm\b", text):
        topics.append("LLM")
    if re.search(r"\bagent\b", text):
        topics.append("Agent")
    if re.search(r"\brag\b", text):
        topics.append("RAG")
    if re.search(r"\breasoning\b", text):
        topics.append("推理")
    if re.search(r"\bopenai\b", text):
        topics.append("AI")
    if re.search(r"\bnvidia\b", text):
        topics.append("硬件")
    if re.search(r"\bmodel\b", text):
        topics.append("AI")
    seen: list[str] = []
    for t in topics:
        if t not in seen:
            seen.append(t)
    return seen[:5]


def _safe_regex(pattern: Any) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.I) if pattern else _DEFAULT_KEYWORD_RE
    except re.error:
        return _DEFAULT_KEYWORD_RE


def process_and_clean(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对应 Process and Clean：时间窗/关键词/最小长度过滤 + 本轮去重。"""
    now = now_ms()
    collected_ms = now
    seen: set[str] = set()
    per_feed: dict[str, int] = {}
    result: list[dict[str, Any]] = []

    for item in raw_items:
        feed = item.get("feed") or {}
        feed_id = feed.get("id") or ""
        feed_key = feed_id or feed.get("url") or "0"
        feed_hits = per_feed.get(feed_key, 0)
        if feed_hits >= config.MAX_ITEMS_PER_FEED:
            continue

        lookback_hours = max(feed.get("lookback_hours") or config.MIN_LOOKBACK_HOURS, config.MIN_LOOKBACK_HOURS)
        lookback_ms = lookback_hours * 3600000
        keyword_re = _safe_regex(feed.get("keyword_regex"))
        min_chars = feed.get("min_content_chars") or 100
        skip_keyword = (
            feed.get("fetch_method") == "Bridge"
            or feed_id.startswith("arxiv-")
            or (feed.get("extra_config") or {}).get("entity_type") == "paper"
        )

        url = normalize_url(item.get("url"))
        title = strip_html(item.get("title"))
        body_text = strip_html(item.get("body"))
        published_ms = parse_date_ms(item.get("published_raw"))
        duplicate_key = build_dedup_key(url, title, feed)

        if not title or not url:
            continue
        if published_ms is None or now - published_ms >= lookback_ms:
            continue
        if len(f"{title} {body_text}") < min_chars and not feed_id.startswith("arxiv-"):
            continue
        if not skip_keyword and not keyword_re.search(f"{title} {body_text}"):
            continue
        if duplicate_key in seen:
            continue
        seen.add(duplicate_key)
        per_feed[feed_key] = feed_hits + 1

        result.append(
            {
                "title": title,
                "url": url,
                "source": feed.get("name") or "Unknown",
                "source_id": feed_id,
                "source_type": feed.get("source_type") or "Other",
                "fetch_method": feed.get("fetch_method") or "",
                "category": feed.get("category") or "",
                "tier": feed.get("tier") or "",
                "published_ms": published_ms,
                "collected_ms": collected_ms,
                "raw_content": body_text[:15000],
                "topics": infer_topics(title, body_text),
                "duplicate_key": duplicate_key,
            }
        )
    return result


def _to_link(url: str, title: str) -> dict[str, str] | None:
    link = str(url or "").strip()
    if not link:
        return None
    return {"link": link, "text": str(title or link).strip() or link}


def format_for_feishu(item: dict[str, Any]) -> dict[str, Any]:
    """对应 Format for Feishu：转成飞书多维表字段。"""
    topics = item.get("topics") or []
    return {
        "标题": item["title"],
        "链接": _to_link(item["url"], item["title"]),
        "来源": item["source"],
        "来源类型": item["source_type"],
        "路由来源": item.get("fetch_method") or "",
        "分类": item["category"],
        "层级": item["tier"],
        "发布时间": item["published_ms"],
        "采集时间": item["collected_ms"],
        "原文": item.get("raw_content") or "",
        "中文摘要": "",
        "为何重要": "",
        "主题": topics if isinstance(topics, list) and topics else [],
        "影响分": 0,
        "新颖度": 0,
        "可行动性": 0,
        "紧迫度": "Pending",
        "状态": "待分析",
        "去重键": item["duplicate_key"],
        "source_id": item.get("source_id") or "",
    }


def build_dify_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item["title"],
        "url": item["url"],
        "source": item["source"],
        "source_id": item.get("source_id") or "",
        "category": item["category"],
        "raw_content": item.get("raw_content") or "",
        "duplicate_key": item["duplicate_key"],
    }
