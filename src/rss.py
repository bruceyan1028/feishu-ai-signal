"""RSS 抓取，替代 n8n 的 RSS Read 节点（用 feedparser）。

产出统一的 RawItem 结构，并把对应 feed 直接挂在每条上——
不再需要 n8n 里的 pairedItem / feed 索引对齐那套逻辑。
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any

import feedparser

log = logging.getLogger(__name__)


def _best_body(entry: Any) -> str:
    content = entry.get("content")
    if content and isinstance(content, list):
        value = content[0].get("value")
        if value:
            return value
    return entry.get("summary") or entry.get("description") or ""


def _best_image(entry: Any, body: str) -> str:
    for key in ("media_content", "media_thumbnail"):
        values = entry.get(key) or []
        if values and isinstance(values[0], dict) and values[0].get("url"):
            return str(values[0]["url"])
    for enclosure in entry.get("enclosures") or entry.get("links") or []:
        if not isinstance(enclosure, dict):
            continue
        media_type = str(enclosure.get("type") or "")
        if media_type.startswith("image/") and enclosure.get("href"):
            return str(enclosure["href"])
    match = re.search(r"<img[^>]+src=[\"']([^\"']+)", body or "", re.I)
    return unescape(match.group(1)) if match else ""


def fetch_feed_sources(feeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐个抓取 RSS 源，容错：单个源失败不影响其它源（对应 onError: continueRegularOutput）。"""
    raw_items: list[dict[str, Any]] = []
    for feed in feeds:
        url = feed["url"]
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # noqa: BLE001 - 与 n8n 容错行为一致
            log.warning("RSS 抓取失败 %s: %s", url, exc)
            continue
        if getattr(parsed, "bozo", False) and not parsed.entries:
            log.warning("RSS 无法解析或为空 %s", url)
            continue
        for entry in parsed.entries:
            body = _best_body(entry)
            raw_items.append(
                {
                    "title": entry.get("title", ""),
                    "url": entry.get("link") or entry.get("id") or "",
                    "body": body,
                    "image_url": _best_image(entry, body),
                    "published_raw": (
                        entry.get("published")
                        or entry.get("updated")
                        or entry.get("pubDate")
                        or ""
                    ),
                    "is_html": True,
                    "feed": feed,
                }
            )
        log.info("RSS %s → %d 条", feed.get("id") or url, len(parsed.entries))
    return raw_items
