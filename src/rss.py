"""RSS 抓取，替代 n8n 的 RSS Read 节点（用 feedparser）。

产出统一的 RawItem 结构，并把对应 feed 直接挂在每条上——
不再需要 n8n 里的 pairedItem / feed 索引对齐那套逻辑。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _media_assets(entry: Any, body: str, page_url: str) -> dict[str, Any]:
    images = []
    for raw in re.findall(r"<img[^>]+src=[\"']([^\"']+)", body or "", re.I):
        url = urljoin(page_url, unescape(raw))
        if url.startswith(("http://", "https://")) and url not in images:
            images.append(url)
    videos = []
    for raw in re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", body or "", re.I):
        url = urljoin(page_url, unescape(raw))
        match = re.search(r"(?:youtube\.com/embed/|youtu\.be/)([\w-]+)", url)
        if match:
            videos.append({"url": url, "embedUrl": f"https://www.youtube-nocookie.com/embed/{match.group(1)}"})
    return {"images": [{"url": url, "alt": ""} for url in images[:4]], "videos": videos[:1]}


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


# 只有 RSS 摘要、没有全文的条目：回源抓正文，否则前端只能显示一两句话
FULLTEXT_MIN_CHARS = 600
FULLTEXT_MAX_FETCH = 40
_ARTICLE_RE = re.compile(r"(?is)<article[^>]*>(.*?)</article>")
_PAGE_NOISE_RE = re.compile(r"(?is)<(nav|header|footer|aside|form|figure|noscript)[^>]*>.*?</\1>")


def _drop_leading_boilerplate(text: str, title: str = "") -> str:
    """去掉正文开头的站点标题、栏目名、作者与时间戳等样板段。

    这些内容混进正文后会被一起翻译，读者看到的开头就全是噪音。
    以「第一段足够长且不与标题重复」为正文起点；全部被判为样板时按原样返回。
    """
    def norm(value: str) -> str:
        return re.sub(r"\W+", "", value).lower()

    paragraphs = text.split("\n\n")
    title_norm = norm(title)
    start = 0
    for index, para in enumerate(paragraphs):
        stripped = para.strip()
        if not stripped:
            start = index + 1
            continue
        para_norm = norm(stripped)
        is_title_echo = bool(title_norm) and (title_norm in para_norm or para_norm in title_norm)
        if len(stripped) < 60 or is_title_echo:
            start = index + 1
            continue
        break
    return "\n\n".join(paragraphs[start:]).strip() or text.strip()


def fetch_article_text(page_url: str, limit: int = 15000, title: str = "") -> str:
    """读取原文页正文（保留段落）。失败返回空串，不阻断采集。"""
    if not str(page_url or "").startswith(("http://", "https://")):
        return ""
    try:
        response = requests.get(
            page_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Signal/1.0)"},
            timeout=12,
        )
        response.raise_for_status()
        html = response.content[:1_500_000].decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException as exc:
        log.info("原文正文读取失败 %s: %s", page_url, exc)
        return ""
    from . import scrape  # 延迟导入，避免采集模块之间的加载顺序耦合

    matches = _ARTICLE_RE.findall(html)
    # 页面可能有多个 <article>（如相关推荐），取最长的那个当正文
    chunk = max(matches, key=len) if matches else html
    text = scrape.html_to_text(_PAGE_NOISE_RE.sub(" ", chunk))
    return _drop_leading_boilerplate(text, title)[:limit]


def backfill_full_text(items: list[dict[str, Any]]) -> int:
    """给正文过短的条目补全原文，返回补全成功的条数。"""
    targets = [
        item
        for item in items
        if len(str(item.get("raw_content") or "")) < FULLTEXT_MIN_CHARS
        and str(item.get("url") or "").startswith(("http://", "https://"))
        # arXiv 的摘要就是合适的正文，抓 HTML 全文只会引入噪音
        and "arxiv.org/" not in str(item.get("url") or "")
    ][:FULLTEXT_MAX_FETCH]
    if not targets:
        return 0
    filled = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_article_text, str(item["url"]), title=str(item.get("title") or "")): item
            for item in targets
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                text = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整轮
                log.info("原文正文补全异常 %s: %s", item.get("url"), exc)
                continue
            # 抓回来的内容明显更长才替换，避免把正文换成导航栏碎片
            if len(text) > max(len(str(item.get("raw_content") or "")) * 2, FULLTEXT_MIN_CHARS):
                item["raw_content"] = text
                filled += 1
    log.info("正文补全：尝试 %d 条，成功 %d 条", len(targets), filled)
    return filled


def fetch_arxiv_figures(page_url: str, limit: int = 3) -> list[dict[str, str]]:
    """从 arXiv HTML 版提取论文图表；它们与 PDF 中的 figure 对应。"""
    match = re.search(r"arxiv\.org/(?:abs|pdf|html)/([^?#/]+)", page_url, re.I)
    if not match:
        return []
    paper_id = match.group(1).removesuffix(".pdf")
    for html_url in (f"https://arxiv.org/html/{paper_id}", f"https://ar5iv.labs.arxiv.org/html/{paper_id}"):
        try:
            response = requests.get(
                html_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Signal/1.0)"},
                timeout=30,
            )
            if response.status_code == 404:
                continue
            response.raise_for_status()
        except requests.RequestException:
            continue
        figures = []
        for block in re.findall(r"<figure\b[\s\S]*?</figure>", response.text, re.I):
            image = re.search(r"<img[^>]+src=[\"']([^\"']+)", block, re.I)
            if not image:
                continue
            caption = re.search(r"<figcaption\b[^>]*>([\s\S]*?)</figcaption>", block, re.I)
            alt = re.sub(r"<[^>]+>", " ", caption.group(1)) if caption else ""
            figures.append(
                {
                    "url": urljoin(response.url, unescape(image.group(1))),
                    "alt": re.sub(r"\s+", " ", unescape(alt)).strip(),
                }
            )
            if len(figures) >= limit:
                break
        if figures:
            return figures
    return []


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
            page_url = entry.get("link") or entry.get("id") or ""
            raw_items.append(
                {
                    "title": entry.get("title", ""),
                    "url": page_url,
                    "body": body,
                    "image_url": _best_image(entry, body),
                    "media_assets": _media_assets(entry, body, page_url),
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
