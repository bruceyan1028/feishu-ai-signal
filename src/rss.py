"""RSS 抓取，替代 n8n 的 RSS Read 节点（用 feedparser）。

产出统一的 RawItem 结构，并把对应 feed 直接挂在每条上——
不再需要 n8n 里的 pairedItem / feed 索引对齐那套逻辑。
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests

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


def _meta_image_from_html(html: str, page_url: str) -> str:
    """从原文 meta 标签提取与文章绑定的封面图。"""
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        attributes = {
            key.lower(): unescape(value)
            for key, _, value in re.findall(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", tag, re.I | re.S)
        }
        image_type = (attributes.get("property") or attributes.get("name") or "").lower()
        image_url = attributes.get("content", "").strip()
        if image_type in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"} and image_url:
            return urljoin(page_url, image_url)
    return ""


def fetch_article_image(page_url: str) -> str:
    """RSS 未提供图片时，读取原文的 OG/Twitter 图片；失败不阻断简报。"""
    if not page_url.startswith(("http://", "https://")):
        return ""
    try:
        response = requests.get(
            page_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Signal/1.0)"},
            timeout=12,
        )
        response.raise_for_status()
        html = response.content[:1_500_000].decode(response.encoding or "utf-8", errors="replace")
        return _meta_image_from_html(html, response.url)
    except requests.RequestException as exc:
        log.info("原文配图读取失败 %s: %s", page_url, exc)
        return ""


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
