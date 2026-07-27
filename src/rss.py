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


# 厂商博客常在正文末尾挂推广卡 / 订阅区 / WordPress 的「The post ... appeared first on」。
# 这些块里的装饰插画会被当成正文配图，正文也会多出一段广告词。
_PROMO_MARKER_RE = re.compile(
    r"(?is)<div[^>]+class=[\"'][^\"']*"
    r"(?:promotional|promo[-_]|newsletter|subscribe|related-posts|read-next)"
    r"|<p>\s*The post\s+<a\b"
)
_PROMO_TAIL_FROM = 0.6


def strip_trailing_promo(body: str) -> str:
    """截掉正文尾部的推广/订阅区块。

    只认落在尾部的标记：正文中段也可能出现同类容器（内嵌按钮、引用卡），
    从那里截断会砍掉真正文。
    """
    body = str(body or "")
    for match in _PROMO_MARKER_RE.finditer(body):
        if match.start() >= len(body) * _PROMO_TAIL_FROM:
            return body[: match.start()]
    return body


def _best_body(entry: Any) -> str:
    content = entry.get("content")
    if content and isinstance(content, list):
        value = content[0].get("value")
        if value:
            return strip_trailing_promo(value)
    return strip_trailing_promo(entry.get("summary") or entry.get("description") or "")


def _best_image(entry: Any, body: str, page_url: str = "") -> str:
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
    images = extract_article_images(body or "", page_url, limit=1)
    return images[0]["url"] if images else ""


def _media_assets(entry: Any, body: str, page_url: str) -> dict[str, Any]:
    videos = []
    for raw in re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", body or "", re.I):
        url = urljoin(page_url, unescape(raw))
        match = re.search(r"(?:youtube\.com/embed/|youtu\.be/)([\w-]+)", url)
        if match:
            videos.append({"url": url, "embedUrl": f"https://www.youtube-nocookie.com/embed/{match.group(1)}"})
    return {"images": extract_article_images(body or "", page_url), "videos": videos[:1]}


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
_MAIN_RE = re.compile(r"(?is)<main\b[^>]*>(.*?)</main>")
# 不要整块删 <figure>：正文表格与配图说明常包在里面
_PAGE_NOISE_RE = re.compile(r"(?is)<(nav|header|footer|aside|form|noscript)[^>]*>.*?</\1>")
_P_TEXT_RE = re.compile(r"(?is)<p\b[^>]*>(.*?)</p>")


def _paragraph_score(html_chunk: str) -> int:
    """用 <p> 里的文字量衡量一个容器像不像正文。"""
    return sum(len(re.sub(r"<[^>]+>", "", para).strip()) for para in _P_TEXT_RE.findall(html_chunk))


def _pick_content_chunk(html: str) -> str:
    """挑出正文容器。

    有的站点（如 Azure 博客）把「相关文章」做成 <article> 卡片，正文却在外面，
    只取最长的 <article> 会抓到一堆推荐位。所以按段落文字量打分，
    候选都明显少于整页时退回整页，再交给噪音过滤和开头样板裁剪。
    """
    candidates = _ARTICLE_RE.findall(html) + _MAIN_RE.findall(html)
    if not candidates:
        return html
    best = max(candidates, key=_paragraph_score)
    return best if _paragraph_score(best) >= _paragraph_score(html) * 0.4 else html


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


_IMG_TAG_RE = re.compile(r"(?is)<img\b[^>]*>")
_SRC_ATTRS = ("src", "data-src", "data-original", "data-lazy-src")
# logo/图标/表情/追踪像素等非内容图
_IMG_SKIP_RE = re.compile(
    r"(?i)(logo|icon|avatar|sprite|pixel|spacer|badge|button|tracking|1x1|blank"
    r"|/emoji/|s\.w\.org|gravatar|wp-includes)"
)
# WordPress 缩略图会把尺寸写进文件名，如 foo-150x150.png
_IMG_SIZE_SUFFIX_RE = re.compile(r"-(\d{2,4})x(\d{2,4})\.(?:jpe?g|png|webp|gif)$", re.I)
_MIN_IMG_SIDE = 200


def _attr(tag: str, name: str) -> str:
    match = re.search(rf"(?is)\b{name}\s*=\s*[\"']([^\"']*)[\"']", tag)
    return match.group(1).strip() if match else ""


def _img_too_small(tag: str, url: str) -> bool:
    """明显偏小的基本是图标而不是插图：先看标签尺寸，再看文件名里的尺寸后缀。"""
    for dim in ("width", "height"):
        raw = re.sub(r"\D", "", _attr(tag, dim))
        if raw and int(raw) < _MIN_IMG_SIDE:
            return True
    return _url_too_small(url)


def _url_too_small(url: str) -> bool:
    suffix = _IMG_SIZE_SUFFIX_RE.search(url.split("?")[0])
    return bool(suffix) and min(int(suffix.group(1)), int(suffix.group(2))) < _MIN_IMG_SIDE


def _image_ok(url: str) -> bool:
    return (
        url.startswith(("http://", "https://"))
        and not url.lower().split("?")[0].endswith(".svg")
        and not _IMG_SKIP_RE.search(url)
        and not _url_too_small(url)
    )


def extract_article_images(html_chunk: str, page_url: str, limit: int = 4) -> list[dict[str, str]]:
    """按文档顺序取正文插图，过滤掉 logo/图标/追踪像素。"""
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag in _IMG_TAG_RE.findall(html_chunk):
        url = next((_attr(tag, attr) for attr in _SRC_ATTRS if _attr(tag, attr)), "")
        if not url:
            srcset = _attr(tag, "srcset")
            url = srcset.split(",")[0].strip().split(" ")[0] if srcset else ""
        if not url or url.startswith("data:"):
            continue
        url = urljoin(page_url, unescape(url))
        if not url.startswith(("http://", "https://")):
            continue
        if url.lower().split("?")[0].endswith(".svg") or _IMG_SKIP_RE.search(url):
            continue
        if _img_too_small(tag, url):
            continue
        key = url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        images.append({"url": url, "alt": unescape(_attr(tag, "alt"))[:120]})
        if len(images) >= limit:
            break
    return images


_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)")


def _from_jina_markdown(markdown: str, page_url: str, title: str, limit: int) -> dict[str, Any]:
    """把 Jina Reader 的 markdown 还原成正文与插图。"""
    body = markdown.split("Markdown Content:", 1)[-1]
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for alt, raw in _MD_IMAGE_RE.findall(body):
        url = urljoin(page_url, raw)
        key = url.split("?")[0]
        if key in seen or not _image_ok(url):
            continue
        seen.add(key)
        images.append({"url": url, "alt": alt[:120]})
        if len(images) >= 4:
            break
    body = _MD_IMAGE_RE.sub(" ", body)
    body = _MD_LINK_RE.sub(r"\1", body)
    body = re.sub(r"(?m)^#{1,6}\s*", "", body)
    body = re.sub(r"(?m)^\s*[-*=_]{3,}\s*$", "", body)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return {"text": _drop_leading_boilerplate(body, title)[:limit], "images": images}


def fetch_article_content(page_url: str, title: str = "", limit: int = 15000) -> dict[str, Any]:
    """一次请求同时取回原文正文与正文插图。失败返回空结果，不阻断采集。"""
    empty: dict[str, Any] = {"text": "", "images": []}
    if not str(page_url or "").startswith(("http://", "https://")):
        return empty
    from . import scrape  # 延迟导入，避免采集模块之间的加载顺序耦合

    html = ""
    final_url = page_url
    try:
        response = requests.get(
            page_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Signal/1.0)"},
            timeout=12,
        )
        response.raise_for_status()
        html = response.content[:1_500_000].decode(response.encoding or "utf-8", errors="replace")
        final_url = response.url or page_url
    except requests.RequestException as exc:
        log.info("原文直连读取失败 %s: %s", page_url, exc)

    if html:
        chunk = strip_trailing_promo(_pick_content_chunk(html))
        images = extract_article_images(chunk, final_url)
        text = scrape.html_to_text(_PAGE_NOISE_RE.sub(" ", chunk))
        result = {"text": _drop_leading_boilerplate(text, title)[:limit], "images": images}
        if len(result["text"]) >= FULLTEXT_MIN_CHARS:
            return result
    # openai.com 这类站点对直连返回 403，或整页由 JS 渲染，交给 Jina Reader 兜底
    markdown = scrape._safe_jina_get(page_url, False)
    return _from_jina_markdown(markdown, page_url, title, limit) if markdown else empty


def fetch_article_text(page_url: str, limit: int = 15000, title: str = "") -> str:
    return str(fetch_article_content(page_url, title=title, limit=limit)["text"])


def backfill_full_text(items: list[dict[str, Any]]) -> int:
    """给正文过短的条目补全原文，返回补全成功的条数。"""
    candidates = [
        item
        for item in items
        if len(str(item.get("raw_content") or "")) < FULLTEXT_MIN_CHARS
        and str(item.get("url") or "").startswith(("http://", "https://"))
        # arXiv 的摘要就是合适的正文，抓 HTML 全文只会引入噪音
        and "arxiv.org/" not in str(item.get("url") or "")
    ]
    # 摘要型源的条目补不到全文就会被长度门槛丢掉，名额优先给它们
    candidates.sort(key=lambda item: not item.get("needs_fulltext"))
    targets = candidates[:FULLTEXT_MAX_FETCH]
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
                    "image_url": _best_image(entry, body, page_url),
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
