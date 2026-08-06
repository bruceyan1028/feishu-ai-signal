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
from urllib.parse import urljoin, urlsplit

import feedparser
import requests

from . import sources

log = logging.getLogger(__name__)


# 厂商博客常在正文末尾挂推广卡 / 订阅区 / WordPress 的「The post ... appeared first on」。
# 这些块里的装饰插画会被当成正文配图，正文也会多出一段广告词。
_PROMO_MARKER_RE = re.compile(
    r"(?is)<div[^>]+class=[\"'][^\"']*"
    r"(?:promotional|promo[-_]|newsletter|subscribe|related-posts|read-next)"
    r"|<p>\s*The post\s+<a\b"
)
_PROMO_TAIL_FROM = 0.6
_DISPLAY_IMAGE_NOISE_RE = re.compile(
    r"(?i)(?:"
    r"author[-_/ ]?(?:avatar|photo|bio)|byline|contributor|profile[-_/ ]?(?:image|photo)"
    r"|avatar|headshot|gravatar|newsletter|subscribe|advertis(?:e|ement)|sponsor"
    r"|qrcode|qr[-_ ]?code|wechat|weixin|公众号|二维码|扫码|赞赏|打赏|联系(?:我们|作者)"
    r")"
)


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


def extract_pdf_documents(body: str, page_url: str, limit: int = 8) -> list[dict[str, str]]:
    """提取正文内同站 PDF 附件，避免把外部参考文献误当成本条官方文件。"""
    page_host = (urlsplit(page_url).hostname or "").lower().removeprefix("www.")
    documents: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag, label_html in re.findall(r"(?is)(<a\b[^>]*>)(.*?)</a>", body or ""):
        href_match = re.search(r"\bhref\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)
        if not href_match:
            continue
        url = urljoin(page_url, unescape(href_match.group(2)).strip())
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not parsed.path.lower().endswith(".pdf") or host != page_host:
            continue
        clean_url = url.split("#", 1)[0]
        key = clean_url.lower()
        if key in seen:
            continue
        seen.add(key)
        label = re.sub(r"<[^>]+>", " ", label_html)
        label = re.sub(r"\s+", " ", unescape(label)).strip()
        documents.append(
            {
                "url": clean_url,
                "title": label or parsed.path.rsplit("/", 1)[-1],
                "type": "application/pdf",
            }
        )
        if len(documents) >= limit:
            break
    return documents


def _media_assets(entry: Any, body: str, page_url: str) -> dict[str, Any]:
    videos = []
    for raw in re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", body or "", re.I):
        url = urljoin(page_url, unescape(raw))
        match = re.search(r"(?:youtube\.com/embed/|youtu\.be/)([\w-]+)", url)
        if match:
            videos.append({"url": url, "embedUrl": f"https://www.youtube-nocookie.com/embed/{match.group(1)}"})
    documents = extract_pdf_documents(body, page_url)
    for enclosure in entry.get("enclosures") or entry.get("links") or []:
        if not isinstance(enclosure, dict):
            continue
        href = str(enclosure.get("href") or "")
        media_type = str(enclosure.get("type") or "").lower()
        if href and (media_type == "application/pdf" or urlsplit(href).path.lower().endswith(".pdf")):
            documents += extract_pdf_documents(
                f'<a href="{href}">{enclosure.get("title") or "PDF"}</a>',
                page_url,
            )
    deduped_documents = list({doc["url"].lower(): doc for doc in documents}.values())[:8]
    return {
        "images": extract_article_images(body or "", page_url),
        "videos": videos[:1],
        "documents": deduped_documents,
    }


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
            candidate = urljoin(page_url, image_url)
            if _image_ok(candidate) and not _DISPLAY_IMAGE_NOISE_RE.search(candidate):
                return candidate
    # 微信文章常把封面放在脚本变量而不是标准 OG 标签里。
    for pattern in (
        r"""(?is)\bmsg_cdn_url\s*[:=]\s*["']([^"']+)""",
        r"""(?is)\bcdn_url_1_1\s*[:=]\s*["']([^"']+)""",
    ):
        match = re.search(pattern, html)
        if match:
            candidate = urljoin(page_url, unescape(match.group(1)).replace(r"\/", "/"))
            if _image_ok(candidate) and not _DISPLAY_IMAGE_NOISE_RE.search(candidate):
                return candidate
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


def image_asset_is_noise(asset: dict[str, Any]) -> bool:
    """识别作者头像、二维码和推广图等不应进入新闻正文的图片。"""
    url = str(asset.get("url") or "").strip()
    alt = str(asset.get("alt") or "").strip()
    kind = str(asset.get("kind") or "").strip()
    return not _image_ok(url) or bool(_DISPLAY_IMAGE_NOISE_RE.search(f"{url} {alt} {kind}"))


def curate_display_media(
    signal: dict[str, Any],
    article_cover: str = "",
) -> tuple[dict[str, Any], str]:
    """按载体整理网页展示图片。

    网页与公众号只保留原文声明的封面，避免把作者卡、广告和相关推荐混入正文。
    视频、播客使用平台自身封面；论文等其它载体保留专用图表链路。
    """
    media = dict(signal.get("mediaAssets") or signal.get("media_assets") or {})
    images = [
        dict(item)
        for item in media.get("images") or []
        if isinstance(item, dict) and not image_asset_is_noise(item)
    ]
    content_type = str(signal.get("contentType") or signal.get("source_type") or "")
    current = str(signal.get("imageUrl") or signal.get("image_url") or "").strip()

    if content_type == "视频":
        video = next(
            (item for item in media.get("videos") or [] if isinstance(item, dict)),
            {},
        )
        cover = str(video.get("thumbnailUrl") or current).strip()
        if cover and _image_ok(cover):
            media["images"] = [{"url": cover, "alt": str(signal.get("titleCn") or signal.get("title") or ""), "kind": "video-cover"}]
            return media, cover
        media["images"] = images[:1]
        return media, current

    if content_type == "播客":
        cover = current or (str(images[0].get("url") or "") if images else "")
        if cover and _image_ok(cover):
            media["images"] = [{"url": cover, "alt": str(signal.get("titleCn") or signal.get("title") or ""), "kind": "podcast-cover"}]
            return media, cover
        media["images"] = images[:1]
        return media, ""

    if content_type in {"文章", "公众号", sources.SIGNAL_FORMAT_WEB, sources.SIGNAL_FORMAT_WECHAT}:
        cover = str(article_cover or "").strip()
        if cover and _image_ok(cover) and not _DISPLAY_IMAGE_NOISE_RE.search(cover):
            media["images"] = [{"url": cover, "alt": str(signal.get("titleCn") or signal.get("title") or ""), "kind": "article-cover"}]
            return media, cover
        # 回源失败时宁缺毋滥：只留一个已经通过基础过滤的候选。
        fallback = current if current and _image_ok(current) and not _DISPLAY_IMAGE_NOISE_RE.search(current) else ""
        if not fallback and images:
            fallback = str(images[0].get("url") or "")
        media["images"] = (
            [{"url": fallback, "alt": str(signal.get("titleCn") or signal.get("title") or ""), "kind": "article-cover"}]
            if fallback
            else []
        )
        return media, fallback

    media["images"] = images
    return media, current


# 只有 RSS 摘要、没有全文的条目：回源抓正文，否则前端只能显示一两句话
FULLTEXT_MIN_CHARS = 600
FULLTEXT_MAX_FETCH = 40
_UPDATED_ONLY_DATE_SOURCES = {
    # 实测这些 Atom feed 没有 published/pubDate，updated 即条目对外发布日期。
    "opencompass",
    "nature-machine-intelligence",
    "nature-computational-science",
}
_ARTICLE_RE = re.compile(r"(?is)<article[^>]*>(.*?)</article>")
_MAIN_RE = re.compile(r"(?is)<main\b[^>]*>(.*?)</main>")
# 不要整块删 <figure>：正文表格与配图说明常包在里面
_PAGE_NOISE_RE = re.compile(r"(?is)<(nav|header|footer|aside|form|noscript)[^>]*>.*?</\1>")
_P_TEXT_RE = re.compile(r"(?is)<p\b[^>]*>(.*?)</p>")


def _published_raw(entry: dict[str, Any], feed: dict[str, Any]) -> str:
    published = entry.get("published") or entry.get("pubDate")
    if published:
        return str(published)
    extra = feed.get("extra_config") or {}
    allow_updated = bool(extra.get("allow_updated_as_published")) or str(
        feed.get("id") or ""
    ) in _UPDATED_ONLY_DATE_SOURCES
    return str(entry.get("updated") or "") if allow_updated else ""


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


_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DATE_ONLY_RE = re.compile(
    rf"^(?:published\s+)?(?:on\s+)?(?:"
    rf"\d{{4}}\s*[-/年.]\s*\d{{1,2}}\s*[-/月.]\s*\d{{1,2}}\s*日?"
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"
    rf")\s*$",
    re.I,
)
_BOILERPLATE_PARA_RE = re.compile(
    r"^(?:"
    r"(?:skip|jump)\s+to\b.{0,40}"
    r"|(?:by|作者|编辑|编译|撰文|责编|来源)\s*[:：|｜]?\s*.{0,30}"
    r"|\d+\s*(?:min(?:ute)?s?|分钟)\s*(?:read|阅读).{0,20}"
    r"|share(?:\s+this)?.{0,20}"
    r"|(?:read|learn)\s+more.{0,20}"
    r"|(?:subscribe|sign\s*(?:in|up)|log\s*in)\b.{0,40}"
    r"|(?:menu|home|blog|news(?:room)?|research|products?|company|about|careers?|contact|"
    r"privacy|terms|docs|pricing|download|support|overview|announcements?)"
    # 博客页的作者/互动条：Published / Update on GitHub / Upvote 138 / +132 / 某某 Follow
    r"|published|updated?\s+on\s+\w+|upvotes?\s*\d*|\+\d+"
    r"|\d+\s*(?:likes?|comments?|shares?|views?|min)"
    r"|.{0,60}\bfollow(?:ing)?"
    r")\s*$",
    re.I,
)
_SENTENCE_END_RE = re.compile(r"[.。!！?？;；]")
# 没有句末标点的短碎片基本都是栏目名/作者行/按钮文案，不会是正文段落
_BOILERPLATE_FRAGMENT_CHARS = 45


def _is_leading_boilerplate(para: str, title_norm: str) -> bool:
    """判断开头这一段是不是样板：标题回声、署名、日期、栏目名或按钮文案。"""
    text = para.strip()
    if not text:
        return True
    normalized = re.sub(r"\W+", "", text).lower()
    # 标题回声要长度也相当才算：正文第一句里恰好含标题词组的情况很常见，
    # 只按包含关系判断会把真正的开头当成重复的标题丢掉
    if title_norm and (
        normalized in title_norm
        or (title_norm in normalized and len(normalized) <= len(title_norm) * 1.6)
    ):
        return True
    if _DATE_ONLY_RE.match(text) or _BOILERPLATE_PARA_RE.match(text):
        return True
    # 没有句末标点又很短的碎片：栏目标签、面包屑、按钮文案
    return len(text) < _BOILERPLATE_FRAGMENT_CHARS and not _SENTENCE_END_RE.search(text)


def _drop_leading_boilerplate(text: str, title: str = "") -> str:
    """去掉正文开头的站点标题、栏目名、作者与时间戳等样板段。

    这些内容混进正文后会被一起翻译，读者看到的开头就全是噪音。
    只按「像样板」判断，不按长度：有的文章第一段本身就是一句短导语，
    单凭字数少就丢会把真正的开头砍掉。全部被判为样板时按原样返回。
    """
    paragraphs = text.split("\n\n")
    title_norm = re.sub(r"\W+", "", title).lower()
    start = 0
    for index, para in enumerate(paragraphs):
        if not _is_leading_boilerplate(para, title_norm):
            start = index
            break
        start = index + 1
    kept = paragraphs[start:]
    # 「副标题 + 发布日期」是博客页的常见开头，副标题留着有用，
    # 孤零零的日期行不是正文，往后几段里也要清掉
    kept = [
        para
        for index, para in enumerate(kept)
        if index > 2 or not _DATE_ONLY_RE.match(para.strip())
    ]
    return "\n\n".join(kept).strip() or text.strip()


_LABEL_NOISE_RE = re.compile(
    r"^(?:"
    r"read\s+more|see\s+more|learn\s+more|more\s+from\b.*|related\b.*|share\s+this\b.*"
    r"|follow\s+us\b.*|subscribe\b.*|sign\s*up\b.*|newsletter\b.*|copyright\b.*|©.*"
    r"|阅读更多|查看更多|了解更多|相关阅读|相关文章|推荐阅读|关注我们|订阅.*|版权所有.*"
    r")\s*$",
    re.I,
)
# 关联文章卡片上的跳转按钮
_CTA_LABEL_RE = re.compile(r"^(?:read|see|learn)\s+more|^(?:阅读更多|查看更多|了解更多)$", re.I)
_FOOTER_RE = re.compile(r"(?i)(©|\ball rights reserved\b|版权所有)")
_CHROME_LABEL_CHARS = 30


def _is_label_paragraph(para: str) -> bool:
    """按钮文案与栏目标签：够短或没有句末标点才算，避免误删正常长句。"""
    return bool(_LABEL_NOISE_RE.match(para)) and (
        len(para) < 40 or not _SENTENCE_END_RE.search(para)
    )


def _cut_related_card_grid(paragraphs: list[str]) -> list[str]:
    """砍掉文末的「关联文章」卡片网格。

    每张卡片都是「标题 + 一句摘要 + Read more」，正文里不会连着出现两个这种按钮。
    一旦出现，说明正文已经结束——否则读者会在正文末尾读到另外几篇文章的梗概。
    按钮间距就是每张卡片的段数，据此把第一张卡片的标题和摘要也一并切掉。
    """
    labels = [index for index, para in enumerate(paragraphs) if _CTA_LABEL_RE.match(para)]
    if len(labels) < 2 or labels[0] < len(paragraphs) * 0.5:
        return paragraphs
    lead = max(labels[1] - labels[0] - 1, 0)
    return paragraphs[: max(labels[0] - lead, 0)]


def _looks_like_nav_row(text: str) -> bool:
    """页脚导航常被压成一行标题式短语，如「Meta AI Assistant Media Generation」：
    每个词都大写开头，通篇没有标点。正常句子做不到这两条同时成立。
    """
    if len(text) > 80 or _SENTENCE_END_RE.search(text) or "," in text or "，" in text:
        return False
    words = text.split()
    if len(words) < 3:
        return False
    titled = sum(1 for word in words if word[:1].isupper())
    return titled >= len(words) * 0.7


def _drop_footer_lines(paragraphs: list[str]) -> list[str]:
    """剥掉正文末尾的页脚残渣：版权行、Mastodon / Cookies 这类短标签、整行导航。

    正文最后一段是完整句子，页脚不是。
    """
    while paragraphs:
        last = paragraphs[-1]
        copyright_line = _FOOTER_RE.search(last) and len(last) < 60
        bare_label = len(last) <= _CHROME_LABEL_CHARS and not _SENTENCE_END_RE.search(last)
        if not (copyright_line or bare_label or _looks_like_nav_row(last)):
            break
        paragraphs.pop()
    return paragraphs


def _drop_label_paragraphs(text: str) -> str:
    """清掉夹在正文里的链接标签段（Read more / 相关阅读）与紧邻的重复段。

    以前正文被截在三千字符内，这些文末残渣看不到；篇幅放开后就露出来了。
    """
    paragraphs = [para.strip() for para in text.split("\n\n") if para.strip()]
    kept: list[str] = []
    for para in _cut_related_card_grid(paragraphs):
        if _is_label_paragraph(para):
            continue
        if kept and para == kept[-1]:  # 源站自己重复的段落，读者看着像 bug
            continue
        kept.append(para)
    return "\n\n".join(_drop_footer_lines(kept))


_IMG_TAG_RE = re.compile(r"(?is)<img\b[^>]*>")
_SRC_ATTRS = ("src", "data-src", "data-original", "data-lazy-src")
# logo/图标/表情/追踪像素等非内容图
_IMG_SKIP_RE = re.compile(
    r"(?i)(logo|icon|avatar|sprite|pixel|spacer|badge|button|tracking|1x1|blank"
    r"|/emoji/|s\.w\.org|gravatar|wp-includes)"
)
# 光看 URL 认不出的站点装饰：/img/RSS.gif 这种要靠 alt / class 才知道是页头图标
_IMG_ATTR_SKIP_RE = re.compile(
    r"(?i)\b(logo|icon|avatar|rss|feed|sprite|navbar|nav|header|footer|share|social|subscribe)\b"
)
_SITE_LABEL_SKIP = frozenset(
    {"www", "com", "org", "net", "edu", "gov", "io", "ai", "co", "cn", "me", "dev", "blog"}
)
# WordPress 缩略图会把尺寸写进文件名，如 foo-150x150.png
_IMG_SIZE_SUFFIX_RE = re.compile(r"-(\d{2,4})x(\d{2,4})\.(?:jpe?g|png|webp|gif)$", re.I)
_MIN_IMG_SIDE = 200
_WIDE_IMG_SIDE = 600


def _attr(tag: str, name: str) -> str:
    match = re.search(rf"(?is)\b{name}\s*=\s*[\"']([^\"']*)[\"']", tag)
    return match.group(1).strip() if match else ""


_PIXEL_DIM_RE = re.compile(r"^\s*(\d{1,5})(?:\s*px)?\s*$", re.I)


def _img_too_small(tag: str, url: str) -> bool:
    """明显偏小的基本是图标而不是插图：先看标签尺寸，再看文件名里的尺寸后缀。

    尺寸只认纯像素值。width="100%" 这类响应式写法去掉非数字后会变成 100，
    当成 100px 判断就会把整张主图误当图标丢掉。
    """
    for dim in ("width", "height"):
        match = _PIXEL_DIM_RE.match(_attr(tag, dim))
        if match and int(match.group(1)) < _MIN_IMG_SIDE:
            return True
    return _url_too_small(url)


def _img_declared_wide(tag: str) -> bool:
    """标签上写明了大尺寸。装饰性图标不会声明自己有 600px 宽。"""
    for dim in ("width", "height"):
        match = _PIXEL_DIM_RE.match(_attr(tag, dim))
        if match and int(match.group(1)) >= _WIDE_IMG_SIDE:
            return True
    return False


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


def _is_site_logo(url: str, page_url: str) -> bool:
    """文件名正好是站点名的图基本都是页头 logo，例如 jmlr.org 上的 /img/jmlr.jpg。"""
    stem = urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    host = (urlsplit(page_url).hostname or "").lower()
    return bool(stem) and stem in {
        label for label in host.split(".") if label and label not in _SITE_LABEL_SKIP
    }


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
        # alt / class 只是弱信号：README 里常有把正文大图的 alt 照抄成 "logo" 的，
        # 所以明确声明了大尺寸的图不受这条约束
        if _IMG_ATTR_SKIP_RE.search(
            f'{_attr(tag, "alt")} {_attr(tag, "class")}'
        ) and not _img_declared_wide(tag):
            continue
        if _is_site_logo(url, page_url) or _img_too_small(tag, url):
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


def parse_jina_markdown(markdown: str, page_url: str, title: str, limit: int) -> dict[str, Any]:
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
    body = _drop_label_paragraphs(re.sub(r"\n{3,}", "\n\n", body).strip())
    return {"text": _drop_leading_boilerplate(body, title)[:limit], "images": images}


def parse_article_html(
    html: str, page_url: str = "", title: str = "", limit: int = 15000
) -> dict[str, Any]:
    """从整页 HTML 里择出正文与插图：选内容块 → 去推广/噪音 → 转文本 → 裁开头样板。

    直接对整页做去标签会把导航栏、栏目名、页脚一并当成正文。
    """
    from . import scrape  # 延迟导入，避免采集模块之间的加载顺序耦合

    if not html:
        return {"text": "", "images": []}
    chunk = strip_trailing_promo(_pick_content_chunk(html))
    # 配图和正文取自同一块去噪后的 HTML：页头页脚里的 logo 不该混进正文插图
    content = _PAGE_NOISE_RE.sub(" ", chunk)
    images = extract_article_images(content, page_url) if page_url else []
    text = _drop_label_paragraphs(scrape.html_to_text(content))
    return {"text": _drop_leading_boilerplate(text, title)[:limit], "images": images}


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
        result = parse_article_html(html, final_url, title, limit)
        if len(result["text"]) >= FULLTEXT_MIN_CHARS:
            return result
    # openai.com 这类站点对直连返回 403，或整页由 JS 渲染，交给 Jina Reader 兜底
    markdown = scrape._safe_jina_get(page_url, False)
    return parse_jina_markdown(markdown, page_url, title, limit) if markdown else empty


def fetch_article_text(page_url: str, limit: int = 15000, title: str = "") -> str:
    return str(fetch_article_content(page_url, title=title, limit=limit)["text"])


def _fill_missing_images(item: dict[str, Any], images: list[dict[str, str]]) -> None:
    """把回源抓到的正文插图补进条目，已有配图则保持不动。"""
    if not images:
        return
    media = item.get("media_assets")
    if not isinstance(media, dict):
        media = {"images": [], "videos": []}
        item["media_assets"] = media
    if not media.get("images"):
        media["images"] = images
    if not str(item.get("image_url") or "").strip():
        item["image_url"] = images[0]["url"]


def backfill_full_text(items: list[dict[str, Any]]) -> int:
    """给正文过短的条目补全原文，返回补全成功的条数。"""
    candidates = [
        item
        for item in items
        if len(str(item.get("raw_content") or "")) < FULLTEXT_MIN_CHARS
        and str(item.get("url") or "").startswith(("http://", "https://"))
        and item.get("fetch_method") not in {"Media", "Social", "Podcast"}
        # arXiv 的摘要就是合适的正文，抓 HTML 全文只会引入噪音
        and "arxiv.org/" not in str(item.get("url") or "")
    ]
    # 摘要型源的条目补不到全文就会被长度门槛丢掉，名额优先给它们
    candidates.sort(key=lambda item: not item.get("needs_fulltext"))
    targets = candidates[:FULLTEXT_MAX_FETCH]
    if not targets:
        return 0
    filled = 0
    imaged = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(
                fetch_article_content, str(item["url"]), title=str(item.get("title") or "")
            ): item
            for item in targets
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整轮
                log.info("原文正文补全异常 %s: %s", item.get("url"), exc)
                continue
            text = str(result.get("text") or "")
            # 抓回来的内容明显更长才替换，避免把正文换成导航栏碎片
            if len(text) > max(len(str(item.get("raw_content") or "")) * 2, FULLTEXT_MIN_CHARS):
                item["raw_content"] = text
                filled += 1
            # 同一次请求已经把正文插图抽出来了：RSS 摘要里基本没有图，
            # 丢掉这批就等于正文来自原文、配图却还停在空摘要上
            before = len((item.get("media_assets") or {}).get("images") or [])
            _fill_missing_images(item, result.get("images") or [])
            if len((item.get("media_assets") or {}).get("images") or []) > before:
                imaged += 1
    log.info("正文补全：尝试 %d 条，成功 %d 条，补配图 %d 条", len(targets), filled, imaged)
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
                    "entry_tags": [
                        str(tag.get("term") or "")
                        for tag in entry.get("tags") or []
                        if isinstance(tag, dict) and tag.get("term")
                    ],
                    "published_raw": _published_raw(entry, feed),
                    "is_html": True,
                    "feed": feed,
                }
            )
        log.info("RSS %s → %d 条", feed.get("id") or url, len(parsed.entries))
    return raw_items
