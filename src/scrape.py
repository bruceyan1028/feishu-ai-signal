"""Scrape 取值来源：Jina Reader 抓列表 → 抽文章链接 → 抓正文 → 组装 RawItem。

对应 n8n 节点：Scrape List Fetch / Extract Article Links / Scrape Article Fetch / Build Scrape Items
并发 3、失败重试 4 次（间隔 5s），与原 batching/retryOnFail 配置一致。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_fixed

from . import config

log = logging.getLogger(__name__)

_SOCIAL = re.compile(
    r"(twitter\.com|x\.com|linkedin\.com|facebook\.com|youtube\.com|instagram\.com|"
    r"mailto:|tel:|/_next/image|\.(pdf|zip|jpg|jpeg|png|gif|svg|webp|mp4|css|js)(\?|$))",
    re.I,
)
_NAV = re.compile(
    r"^(read more|learn more|home|blog|news|newsroom|research|policy|products?|company|"
    r"about|careers?|contact|privacy|terms|sign in|log ?in|subscribe|share|menu|docs|"
    r"pricing|download|support|next|previous|prev|overview|commitments|learn|try claude)$",
    re.I,
)
_LINK_RE = re.compile(r"\[([^\]]*?)\]\((https?://[^\s)]+)\)")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _host(u: str) -> str:
    m = re.match(r"^https?://([^/?#]+)", str(u or ""), re.I)
    return re.sub(r"^www\.", "", m.group(1), flags=re.I).lower() if m else ""


def _path_of(u: str) -> str:
    m = re.match(r"^https?://[^/?#]+([^?#]*)", str(u or ""), re.I)
    return re.sub(r"/+$", "", m.group(1) if m else "")


@retry(stop=stop_after_attempt(config.HTTP_MAX_TRIES), wait=wait_fixed(config.HTTP_WAIT_SECONDS))
def _jina_get(url: str, list_mode: bool) -> str:
    headers = {"Authorization": f"Bearer {config.JINA_API_KEY}"}
    if list_mode:
        headers.update(
            {
                "x-engine": "browser",
                "x-timeout": "20",
                "x-with-links-summary": "true",
                "x-respond-with": "markdown",
            }
        )
    resp = requests.get(
        f"https://r.jina.ai/{url}", headers=headers, timeout=config.JINA_TIMEOUT
    )
    resp.raise_for_status()
    return resp.text


def _safe_jina_get(url: str, list_mode: bool) -> str:
    try:
        return _jina_get(url, list_mode)
    except Exception as exc:  # noqa: BLE001 - 对应 onError: continueRegularOutput
        log.warning("Jina 抓取失败 %s: %s", url, exc)
        return ""


def _extract_links(md: str, feed: dict[str, Any]) -> list[dict[str, str]]:
    """对应 Extract Article Links：从列表页 markdown 中抽出同域文章链接。"""
    src_url = feed["url"]
    src_host = _host(src_url)
    list_path = _path_of(src_url)
    strict = len(list_path) > 1
    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)

    md = _IMG_RE.sub("", md)
    seen: set[str] = set()
    cand: list[dict[str, str]] = []
    for m in _LINK_RE.finditer(md):
        title = re.sub(r"\s+", " ", (m.group(1) or "")).strip()
        url = re.sub(r"[).,]+$", "", m.group(2).strip())
        if _SOCIAL.search(url):
            continue
        if _host(url) != src_host:
            continue
        path = _path_of(url)
        if not path:
            continue
        if url.rstrip("/") == src_url.rstrip("/"):
            continue
        if _NAV.match(title):
            continue
        if strict:
            if not (path + "/").startswith(list_path + "/"):
                continue
            if path == list_path:
                continue
        else:
            segs = [s for s in path.split("/") if s]
            if len(segs) < 2 and not re.search(r"\d", path):
                continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        cand.append({"url": key, "title": title})
    return cand[:max_n]


def _strip_md(s: str) -> str:
    s = _IMG_RE.sub("", str(s or ""))
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^[#>*`\-\s]+", "", s, flags=re.M)
    s = re.sub(r"[`*_]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _build_item(article_md: str, link: dict[str, str], feed: dict[str, Any]) -> dict[str, Any] | None:
    """对应 Build Scrape Items：把 Jina 正文组装成统一 RawItem。"""
    body = str(article_md or "")
    if not body:
        return None
    title = link.get("title") or ""
    published = ""
    content = body

    mt = re.search(r"^Title:\s*(.+)$", body, re.M)
    if mt and mt.group(1).strip():
        title = mt.group(1).strip()
    mp = re.search(r"^Published Time:\s*(.+)$", body, re.M)
    if mp:
        published = mp.group(1).strip()
    marker = body.find("Markdown Content:")
    if marker >= 0:
        content = body[marker + len("Markdown Content:") :]

    content = _strip_md(content)[:15000]
    url = link.get("url") or ""
    if not title or not url:
        return None
    return {
        "title": title,
        "url": url,
        "body": content,
        "published_raw": published,
        "is_html": False,
        "feed": feed,
    }


def fetch_scrape_sources(feeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """完整 Scrape 流水线：并发抓列表页 → 抽链接 → 并发抓正文 → 组装。"""
    if not feeds:
        return []

    with ThreadPoolExecutor(max_workers=config.JINA_CONCURRENCY) as pool:
        list_pages = list(pool.map(lambda f: _safe_jina_get(f["url"], True), feeds))

    tasks: list[tuple[dict[str, str], dict[str, Any]]] = []
    for feed, md in zip(feeds, list_pages):
        if not md:
            continue
        for link in _extract_links(md, feed):
            tasks.append((link, feed))
    log.info("Scrape 待抓正文 %d 篇", len(tasks))

    with ThreadPoolExecutor(max_workers=config.JINA_CONCURRENCY) as pool:
        articles = list(pool.map(lambda t: _safe_jina_get(t[0]["url"], False), tasks))

    raw_items: list[dict[str, Any]] = []
    for (link, feed), article_md in zip(tasks, articles):
        item = _build_item(article_md, link, feed)
        if item:
            raw_items.append(item)
    return raw_items
