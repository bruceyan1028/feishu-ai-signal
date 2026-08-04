"""Scrape 取值来源：Jina Reader 抓列表 → 抽文章链接 → 抓正文 → 组装 RawItem。

对应 n8n 节点：Scrape List Fetch / Extract Article Links / Scrape Article Fetch / Build Scrape Items
并发 3、失败重试 4 次（间隔 5s），与原 batching/retryOnFail 配置一致。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_fixed

from . import config

log = logging.getLogger(__name__)

_SOCIAL = re.compile(
    r"(twitter\.com|x\.com|linkedin\.com|facebook\.com|youtube\.com|instagram\.com|"
    r"mailto:|tel:|/_next/(?:static|image)|/static/media/|"
    r"\.(pdf|zip|jpg|jpeg|png|gif|svg|webp|mp4|css|js|woff2?|ttf|ico|map)(\?|$))",
    re.I,
)
_NAV = re.compile(
    r"^(read more|learn more|home|blog|news|newsroom|research|policy|products?|company|"
    r"about|careers?|contact|privacy|terms|sign in|log ?in|subscribe|share|menu|docs|"
    r"pricing|download|support|next|previous|prev|overview|commitments|learn|try claude|"
    r"sitemap|imprint|accessibility|search|login|footer)$",
    re.I,
)
_DEFAULT_PATH_EXCLUDE = re.compile(
    r"/(?:footer|utils|login|search|cdn-cgi|_upload/tpl|_next/static|static/media|"
    r"wp-admin|wp-includes|assets/|fonts?/)|"
    r"\.(?:css|js|woff2?|ttf|ico|map|svg|png|jpe?g|gif|webp)$",
    re.I,
)
_LINK_RE = re.compile(r"\[([^\]]*?)\]\((https?://[^\s)]+)\)")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 含相对路径（如 Apache 目录列表 24-07-xx.html、xwgg/xwdt/...），由 urljoin 拼成绝对 URL
_HREF_RE = re.compile(
    r"""href=["'](?!javascript:|mailto:|tel:|#)([^"'#?\s]+(?:\?[^"'#\s]*)?)["']""",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_UA = "Mozilla/5.0 (compatible; AI-Signal/1.0; +https://github.com/)"

# HF Papers / Papers with Code（后者已 302 到 HF）：只认 /papers/<arxiv_id>
_HF_PWC_SOURCE_IDS = frozenset(
    {
        "hf-papers-trending",
        "papers-with-code-trending",
        "papers-with-code-sota",
        "papers-with-code-api",
    }
)
_ARXIV_PAPER_ID_RE = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$", re.I)
_PAPER_URL_RE = re.compile(
    r"(?:https?://(?:www\.)?(?:huggingface\.co|paperswithcode\.com|paperswithcode\.co))?/papers/"
    r"(\d{4}\.\d{4,5})(?:v\d+)?",
    re.I,
)
_DAILY_PAPERS_PROPS_RE = re.compile(
    r'data-target="DailyPapers"\s+data-props="([^"]*)"',
    re.I,
)
_PWC_CO_TRENDING_API = "https://paperswithcode.co/api/v1/papers/trending"
_MODELSCOPE_OPENAPI_MODELS = "https://www.modelscope.cn/openapi/v1/models"
_MODELSCOPE_SOURCE_IDS = frozenset({"modelscope-home", "qwen-modelscope"})
_SEED_ARTICLE_LIST_API = "https://seed.bytedance.com/api/get_article_list_v2"
_SEED_ARTICLE_DETAIL_API = "https://seed.bytedance.com/api/get_article_detail"
_SEED_SOURCE_IDS = frozenset({"bytedance-seed"})
_SEED_ARTICLE_TYPE_BLOG = 2
_GITHUB_SEARCH_API = "https://api.github.com/search/repositories"
_GITHUB_SOURCE_IDS = frozenset({"github-trending"})
# README 正文摘录上限：1500 字符只够放完徽章和安装步骤，真正的项目介绍会被切掉
README_EXCERPT_CHARS = 4000
_DEFAULT_RECENT_DAYS = 7
_DEFAULT_HIGH_UPVOTES = 100
_DEFAULT_HIGH_STARS_24H = 100
_DEFAULT_MIN_UPVOTES = 0
# 高热例外的年龄上限：再热的论文过了这个天数也不算「近期发布」
_DEFAULT_MAX_HEAT_AGE_DAYS = 30


def probe_jina(timeout: float = 12.0) -> bool:
    """探测 Jina Reader 是否可达。"""
    try:
        resp = requests.get(
            "https://r.jina.ai/https://example.com",
            headers={"Authorization": f"Bearer {config.JINA_API_KEY}"} if config.JINA_API_KEY else {},
            timeout=timeout,
        )
        return resp.status_code < 500 and bool(resp.text)
    except Exception:  # noqa: BLE001
        return False


def _direct_get(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        timeout=min(config.JINA_TIMEOUT, 30),
        allow_redirects=True,
    )
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
    return resp.text


def _safe_direct_get(url: str) -> str:
    try:
        return _direct_get(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("直连抓取失败 %s: %s", url, exc)
        return ""


_BLOCK_BOUNDARY_RE = re.compile(
    r"(?is)</(?:p|div|section|article|header|footer|li|ul|ol|h[1-6]|blockquote|pre|table|tr|figure|figcaption)\s*>"
    r"|<br\s*/?>"
)


def _normalize_paragraphs(text: str) -> str:
    """压平行内空白但保留段落边界（空行）。"""
    text = re.sub(r"[ \t\u00a0\u3000]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


_TABLE_RE = re.compile(r"(?is)<table\b[^>]*>(.*?)</table>")
_ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
_CELL_RE = re.compile(r"(?is)<t[hd]\b[^>]*>(.*?)</t[hd]>")


def _table_to_text(match: re.Match[str]) -> str:
    """表格按「单元格 | 单元格」逐行铺开。

    直接去标签会把整张表压成一串没有行列边界的词，读者根本对不上数值。
    """
    rows = []
    for row_html in _ROW_RE.findall(match.group(1)):
        cells = [
            _one_line(_TAG_RE.sub(" ", re.sub(r"(?is)<br\s*/?>", " ", cell)))
            for cell in _CELL_RE.findall(row_html)
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(" | ".join(cells))
    return "\n\n" + "\n".join(rows) + "\n\n" if rows else " "


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    # 正文块是从整页切出来的，闭合标签可能被切在外面，剩下的脚本要连尾巴一起丢
    text = re.sub(r"(?is)<(script|style)[^>]*>.*$", " ", text)
    # 表格要在通用去标签之前单独处理，保住行列结构
    text = _TABLE_RE.sub(_table_to_text, text)
    # 先把块级边界落成换行，再去标签：否则段落结构会被整体压成一行，前端只能渲染出字墙。
    text = _BLOCK_BOUNDARY_RE.sub("\n\n", text)
    text = _TAG_RE.sub(" ", text)
    # 去标签后再解实体，否则 &nbsp;/&#8217; 会原样出现在正文里
    return _normalize_paragraphs(unescape(text))


# 模块内沿用旧私有名，同时对外暴露给 rss/process 复用同一套段落保留逻辑
_html_to_text = html_to_text


def _norm_arxiv_paper_id(raw: str) -> str:
    m = _ARXIV_PAPER_ID_RE.match(str(raw or "").strip())
    return m.group(1) if m else ""


def _feed_extra(feed: dict[str, Any]) -> dict[str, Any]:
    extra = feed.get("extra_config")
    return extra if isinstance(extra, dict) else {}


def _as_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,|，、;\s]+", raw)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _path_allowed(path: str, feed: dict[str, Any]) -> bool:
    """extra_config.link_path_include / link_path_exclude（子串或正则）。"""
    extra = _feed_extra(feed)
    path_l = (path or "").lower()
    if _DEFAULT_PATH_EXCLUDE.search(path_l):
        return False
    for pat in _as_str_list(extra.get("link_path_exclude")):
        try:
            if re.search(pat, path_l, re.I):
                return False
        except re.error:
            if pat.lower() in path_l:
                return False
    includes = _as_str_list(extra.get("link_path_include"))
    if not includes:
        return True
    for pat in includes:
        try:
            if re.search(pat, path_l, re.I):
                return True
        except re.error:
            if pat.lower() in path_l:
                return True
    return False


def _list_prefix_path(list_path: str) -> str:
    """列表页若是 /foo/news.html，子文常在 /foo/news/2026/... —— 去掉 .html 再做前缀匹配。"""
    p = (list_path or "").rstrip("/")
    if re.search(r"\.(?:html?)$", p, re.I):
        return re.sub(r"\.(?:html?)$", "", p, flags=re.I)
    return p


def _link_depth_ok(path: str, feed: dict[str, Any], *, strict: bool, list_path: str) -> bool:
    """默认深度启发式；allow_shallow_html=true 时允许 /blog.html 这类单段页面。"""
    extra = _feed_extra(feed)
    if strict:
        prefix = _list_prefix_path(list_path)
        if not (path + "/").startswith(prefix + "/"):
            return False
        if path == prefix or path == list_path.rstrip("/"):
            return False
        return True
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 2 or re.search(r"\d", path):
        return True
    if extra.get("allow_shallow_html") and re.search(
        r"\.(?:html?)$|(?:news|blog|press|post|article|story|publication)",
        path,
        re.I,
    ):
        return True
    return False


def _link_recency_key(url: str) -> tuple[int, str]:
    """优先带年份路径的较新链接（目录列表常按字母序把旧稿排前）。"""
    path = _path_of(url)
    years = [int(y) for y in re.findall(r"(?:^|/)(20\d{2})(?:/|-|_)", path)]
    year = max(years) if years else 0
    return (year, path)


def _parse_iso_ms(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


_PUBLISHED_KEYS = (
    "article:published_time",
    "datePublished",
    "date_published",
    "publishedAt",
    "publishDate",
    "pubdate",
    "_createdAt",
)

_HEADER_DATE_WITH_READ_TIME_RE = re.compile(
    r"""([A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2})"""
    r"""\s*[•·|]\s*\d+\s*(?:minute|min)\s+read\b""",
    re.I,
)


def extract_published_date_html(html: str) -> str:
    """从原始页面提取首发日期；明确不使用 dateModified/_updatedAt。"""
    body = unescape(str(html or ""))
    if not body:
        return ""
    keys = "|".join(re.escape(key) for key in _PUBLISHED_KEYS)
    meta_patterns = [
        rf"""<meta\b[^>]*(?:property|name)=["'](?:{keys})["'][^>]*content=["']([^"']+)["']""",
        rf"""<meta\b[^>]*content=["']([^"']+)["'][^>]*(?:property|name)=["'](?:{keys})["']""",
    ]
    for pattern in meta_patterns:
        match = re.search(pattern, body, re.I)
        if match:
            return match.group(1).strip()

    # Next.js/Sanity 等常把文章对象放在转义后的脚本字符串中。
    json_match = re.search(
        rf"""(?:{keys})\\?["']?\s*:\\?["']([^"'\\<]{{8,80}})""",
        body,
        re.I,
    )
    if json_match:
        return json_match.group(1).strip()

    time_match = re.search(
        r"""<time\b[^>]*datetime=["']([^"']+)["'][^>]*>""",
        body,
        re.I,
    )
    if time_match:
        return time_match.group(1).strip()

    # Meta AI Blog 的首发日期没有 Published 标签，而是紧跟文章 h1，以
    # “April 8, 2026 • 8 minute read” 展示。只在标题后的有限 header 区域
    # 接受这种强结构，避免把导航、推荐卡或正文提到的任意日期当成首发时间。
    title_match = re.search(r"(?is)<h1\b[^>]*>.*?</h1\s*>", body)
    if title_match:
        header_text = _html_to_text(body[title_match.start() : title_match.end() + 5000])
        header_date = _HEADER_DATE_WITH_READ_TIME_RE.search(header_text)
        if header_date:
            return header_date.group(1).strip()

    # 只接受带“发布”语义的可见日期，避免误取版权年份或正文中的其它日期。
    visible = re.search(
        r"""(?:published|posted|发布日期|发布时间)\s*(?:on|[:：])?\s*"""
        r"""([A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2}|"""
        r"""\d{1,2}\s+[A-Z][a-z]{2,8}\s+20\d{2}|"""
        r"""20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)""",
        _html_to_text(body[:20000]),
        re.I,
    )
    return visible.group(1).strip() if visible else ""


def _published_date_from_jina(markdown: str) -> str:
    match = re.search(r"^Published Time:\s*(.+)$", str(markdown or ""), re.M)
    return match.group(1).strip() if match else ""


def _fetch_direct_published_date(url: str) -> str:
    """Jina 未返回日期时单次直连页面补元数据；失败即保持未知。"""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _UA},
            timeout=min(config.JINA_TIMEOUT, 20),
        )
        response.raise_for_status()
    except requests.RequestException:
        return ""
    return extract_published_date_html(response.text)


def _age_days(published_ms: int | None, *, now_ms: int | None = None) -> float | None:
    if published_ms is None:
        return None
    now = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    return max(0.0, (now - published_ms) / 86400000.0)


def _trending_recent_policy(feed: dict[str, Any]) -> dict[str, float]:
    """trending ∩ 近 N 日；更旧仅当超高热度例外。"""
    extra = _feed_extra(feed)
    return {
        "recent_days": float(extra.get("recent_days") or _DEFAULT_RECENT_DAYS),
        "min_upvotes": float(extra.get("min_upvotes") or _DEFAULT_MIN_UPVOTES),
        "high_upvote_threshold": float(
            extra.get("high_upvote_threshold") or _DEFAULT_HIGH_UPVOTES
        ),
        "high_stars_gained_24h": float(
            extra.get("high_stars_gained_24h") or _DEFAULT_HIGH_STARS_24H
        ),
        "max_heat_age_days": float(
            extra.get("max_heat_age_days") or _DEFAULT_MAX_HEAT_AGE_DAYS
        ),
    }


def _arxiv_id_month_end_ms(pid: str) -> int | None:
    """arXiv ID 的 YYMM 即首个版本的投稿月份，取该月最后一刻作为首发时间上界。"""
    m = re.match(r"^(\d{2})(\d{2})\.", str(pid or ""))
    if not m:
        return None
    year, month = 2000 + int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    next_month = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    return int(next_month.timestamp() * 1000) - 1


def _paper_first_published_raw(pid: str, listed_raw: str) -> str:
    """榜单日期与 arXiv ID 月份取早者，作为论文真实首发时间。

    HF/PwC 给的 publishedAt 是「进入榜单的日期」，几个月前的论文被重新顶上榜时
    会显示成本周，直接采信就会把旧论文当成新发布推出去。ID 月份是首个版本的
    投稿月，用它兜住上界即可识别这种重新上榜。
    """
    listed_ms = _parse_iso_ms(listed_raw)
    # 无日期仍不放行，交给 _keep_trending_paper 丢弃：ID 月份只用来把时间往早修正
    if listed_ms is None:
        return listed_raw
    id_ms = _arxiv_id_month_end_ms(pid)
    if id_ms is None or id_ms >= listed_ms:
        return listed_raw
    return datetime.fromtimestamp(id_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _keep_trending_paper(
    *,
    published_raw: str,
    upvotes: float = 0,
    stars_gained_24h: float = 0,
    age_days: float | None = None,
    feed: dict[str, Any],
) -> tuple[bool, bool]:
    """返回 (keep, heat_keep)。heat_keep=超高热度例外（可越过清洗 lookback）。"""
    policy = _trending_recent_policy(feed)
    recent_days = policy["recent_days"]
    pub_ms = _parse_iso_ms(published_raw)
    age = age_days if age_days is not None else _age_days(pub_ms)
    if age is not None and age <= recent_days:
        if upvotes and upvotes < policy["min_upvotes"]:
            return False, False
        return True, False
    # 无日期时不放行（避免空 published 被 process 当成 now）
    if age is None:
        return False, False
    # 高热例外也要有年龄上限，否则常年高赞的老论文会一直占着「热榜」名额
    if age > policy["max_heat_age_days"]:
        return False, False
    if upvotes >= policy["high_upvote_threshold"] or stars_gained_24h >= policy["high_stars_gained_24h"]:
        return True, True
    return False, False


def _is_hf_pwc_paper_feed(feed: dict[str, Any]) -> bool:
    """HF Papers / PwC 列表源：专用只抽 paper URL。"""
    sid = str(feed.get("id") or "").strip().lower()
    if sid in _HF_PWC_SOURCE_IDS or sid.startswith("hf-papers") or sid.startswith("papers-with-code"):
        return True
    host = _host(feed.get("url") or "")
    path = _path_of(feed.get("url") or "")
    if host in {"huggingface.co", "paperswithcode.com", "paperswithcode.co"} and (
        path in {"", "/"} or path == "/papers" or path.startswith("/papers/")
    ):
        return True
    return False


def _feed_force_direct(feed: dict[str, Any]) -> bool:
    """需原始 HTML（内嵌 SSR props：upvotes/publishedAt）才能抽取的源，强制走 direct。

    HF/PwC 论文榜单页经 Jina 渲染成 markdown 会丢掉 DailyPapers props，导致
    抽不到发布时间 → _keep_trending_paper 因无日期全部丢弃。这类源必须取原始 HTML。

    另有部分站点（如品玩/澎湃/财新）Jina 渲染后文章链接不以标准 markdown 链接
    形式出现，导致抽不到列表；这类源可在 extra_config 里显式设 force_direct=true。
    """
    if _is_hf_pwc_paper_feed(feed):
        return True
    return bool(_feed_extra(feed).get("force_direct"))


def _extract_pwc_co_trending_links(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """paperswithcode.co 是 SPA，列表页无可用 HTML；走官方 trending API。"""
    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)
    try:
        resp = requests.get(
            _PWC_CO_TRENDING_API,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=min(config.JINA_TIMEOUT, 30),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("PwC.co trending API 失败: %s", exc)
        return []

    if not isinstance(data, list):
        return []

    cand: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in data:
        if not isinstance(row, dict):
            continue
        pid = _norm_arxiv_paper_id(str(row.get("arxiv_id") or ""))
        if not pid or pid in seen:
            continue
        seen.add(pid)
        title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip() or pid
        published_raw = _paper_first_published_raw(pid, str(row.get("date_published") or "").strip())
        age = row.get("paper_age_days")
        try:
            age_f = float(age) if age is not None else None
        except (TypeError, ValueError):
            age_f = None
        # 接口给的 paper_age_days 是按上榜日算的，首发时间被修正过就以修正后的为准
        corrected_age = _age_days(_parse_iso_ms(published_raw))
        if corrected_age is not None and (age_f is None or corrected_age > age_f):
            age_f = corrected_age
        trending = row.get("trending") if isinstance(row.get("trending"), dict) else {}
        try:
            stars_24h = float(trending.get("stars_gained_24h") or 0)
        except (TypeError, ValueError):
            stars_24h = 0.0
        keep, heat_keep = _keep_trending_paper(
            published_raw=published_raw,
            stars_gained_24h=stars_24h,
            age_days=age_f,
            feed=feed,
        )
        if not keep:
            continue
        cand.append(
            {
                "url": f"https://huggingface.co/papers/{pid}",
                "title": title[:200],
                "published_raw": published_raw,
                "heat_keep": heat_keep,
                "metrics": {"community_heat": stars_24h, "stars_gained_24h": stars_24h},
            }
        )
        if len(cand) >= max_n:
            break
    return cand


def _extract_hf_pwc_paper_links(page: str, feed: dict[str, Any]) -> list[dict[str, Any]]:
    """只认 paper URL；策略=trending ∩ 近 N 日（默认 7），超高热度可例外。

    - huggingface.co / 旧 paperswithcode.com（会 302 到 HF）：解析 DailyPapers props
    - paperswithcode.co：列表页是 SPA，改走 /api/v1/papers/trending
    """
    host = _host(feed.get("url") or "")
    if host == "paperswithcode.co":
        return _extract_pwc_co_trending_links(feed)

    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)
    meta: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _add(
        pid: str,
        title: str = "",
        *,
        published_raw: str = "",
        upvotes: float = 0,
    ) -> None:
        pid = _norm_arxiv_paper_id(pid)
        if not pid:
            return
        if pid not in meta:
            order.append(pid)
            meta[pid] = {"title": "", "published_raw": "", "upvotes": 0.0}
        title = re.sub(r"\s+", " ", (title or "")).strip()
        if title and not _NAV.match(title) and len(title) > 3:
            meta[pid]["title"] = title
        if published_raw and not meta[pid]["published_raw"]:
            meta[pid]["published_raw"] = published_raw
        if upvotes:
            meta[pid]["upvotes"] = max(float(meta[pid]["upvotes"] or 0), float(upvotes))

    for m in _DAILY_PAPERS_PROPS_RE.finditer(page or ""):
        try:
            props = json.loads(unescape(m.group(1)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for entry in props.get("dailyPapers") or []:
            if not isinstance(entry, dict):
                continue
            paper = entry.get("paper") if isinstance(entry.get("paper"), dict) else {}
            try:
                upvotes = float(paper.get("upvotes") or entry.get("upvotes") or 0)
            except (TypeError, ValueError):
                upvotes = 0.0
            _add(
                str(paper.get("id") or ""),
                str(entry.get("title") or paper.get("title") or ""),
                published_raw=str(paper.get("publishedAt") or entry.get("publishedAt") or ""),
                upvotes=upvotes,
            )

    for m in _LINK_RE.finditer(page or ""):
        pm = _PAPER_URL_RE.search(m.group(2) or "")
        if pm:
            _add(pm.group(1), m.group(1) or "")

    for m in _PAPER_URL_RE.finditer(page or ""):
        _add(m.group(1))

    cand: list[dict[str, Any]] = []
    for pid in order:
        row = meta[pid]
        up = float(row.get("upvotes") or 0)
        published_raw = _paper_first_published_raw(pid, str(row.get("published_raw") or ""))
        keep, heat_keep = _keep_trending_paper(
            published_raw=published_raw,
            upvotes=up,
            feed=feed,
        )
        if not keep:
            continue
        # 社区反响：点赞/采用度 + HF AI 摘要，正文与 metrics 一并带上
        block, cmetrics = _hf_paper_community(pid)
        metrics = {"community_heat": up, "community_upvotes": up}
        metrics.update({k: v for k, v in cmetrics.items() if v})
        cand.append(
            {
                "url": f"https://huggingface.co/papers/{pid}",
                "title": (str(row.get("title") or "") or pid)[:200],
                "published_raw": published_raw,
                "heat_keep": heat_keep,
                "metrics": metrics,
                "community_block": block,
            }
        )
        if len(cand) >= max_n:
            break
    return cand


def _is_modelscope_feed(feed: dict[str, Any]) -> bool:
    """魔搭首页/组织页是 SPA：走 OpenAPI，不解析 HTML。"""
    sid = str(feed.get("id") or "").strip().lower()
    if sid in _MODELSCOPE_SOURCE_IDS or sid.startswith("modelscope"):
        return True
    extra = _feed_extra(feed)
    if extra.get("modelscope_api") or extra.get("modelscope_mode"):
        return True
    host = _host(feed.get("url") or "")
    path = _path_of(feed.get("url") or "")
    return host in {"modelscope.cn"} and (path in {"", "/", "/home"} or path.startswith("/organization/"))


def _modelscope_model_page_url(model_id: str) -> str:
    mid = str(model_id or "").strip().lstrip("/")
    return f"https://www.modelscope.cn/models/{mid}" if mid else ""


def _fetch_modelscope_model_detail(model_id: str) -> dict[str, Any]:
    mid = str(model_id or "").strip()
    if not mid:
        return {}
    try:
        resp = requests.get(
            f"{_MODELSCOPE_OPENAPI_MODELS}/{mid}",
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=min(config.JINA_TIMEOUT, 30),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("ModelScope 详情 API 失败 %s: %s", mid, exc)
        return {}
    row = data.get("data") if isinstance(data, dict) else None
    return row if isinstance(row, dict) else {}


def _fetch_modelscope_items(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """对应 modelscope.cn/home 的 Models 新发布：OpenAPI 列表 → 详情 → 可点击模型页。

    extra_config:
      - modelscope_mode: home | owner（默认 home）
      - modelscope_owner: 仅 owner 模式，如 Qwen
      - recent_days: 按 created_at/last_modified 过滤（默认 14）
    """
    extra = _feed_extra(feed)
    mode = str(extra.get("modelscope_mode") or "home").strip().lower()
    owner = str(extra.get("modelscope_owner") or extra.get("owner") or "").strip()
    exclude_pattern = str(extra.get("model_name_exclude_regex") or "").strip()
    try:
        exclude_model = re.compile(exclude_pattern, re.I) if exclude_pattern else None
    except re.error:
        log.warning("ModelScope 模型排除正则无效，忽略: %s", exclude_pattern)
        exclude_model = None
    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)
    try:
        recent_days = float(extra.get("recent_days") or 14)
    except (TypeError, ValueError):
        recent_days = 14.0

    params: dict[str, Any] = {
        "PageSize": max(max_n * 3, 20),
        "PageNumber": 1,
        "SortBy": "last_modified",
        "Order": "desc",
    }
    if mode == "owner" and owner:
        params["owner"] = owner

    try:
        resp = requests.get(
            _MODELSCOPE_OPENAPI_MODELS,
            params=params,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=min(config.JINA_TIMEOUT, 30),
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("ModelScope 列表 API 失败: %s", exc)
        return []

    models = ((payload.get("data") or {}).get("models") if isinstance(payload, dict) else None) or []
    if not isinstance(models, list):
        return []

    items: list[dict[str, Any]] = []
    for row in models:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        candidate_name = (
            str(row.get("display_name") or "").strip() or mid.split("/")[-1]
        )
        if exclude_model and exclude_model.search(candidate_name):
            continue
        # last_modified 仅代表模型近期活跃；条目“发布时间”必须使用首次创建时间。
        published_raw = str(row.get("created_at") or "").strip()
        age = _age_days(_parse_iso_ms(published_raw))
        if age is None or age > recent_days:
            continue

        detail = _fetch_modelscope_model_detail(mid)
        title = (
            str(detail.get("display_name") or row.get("display_name") or "").strip()
            or mid.split("/")[-1]
        )
        desc = str(detail.get("description") or row.get("description") or "").strip()
        readme = str(detail.get("readme") or "").strip()
        body_parts = [p for p in (desc, readme) if p]
        body = "\n\n".join(body_parts)[:15000]
        if len(body) < 40:
            body = f"{title}\nModelScope model: {mid}\nPublished: {published_raw}"[:15000]

        url = _modelscope_model_page_url(mid)
        items.append(
            {
                "title": title[:200],
                "url": url,
                "body": body,
                "published_raw": published_raw,
                "heat_keep": False,
                "is_html": False,
                "feed": feed,
            }
        )
        if len(items) >= max_n:
            break
    return items


def _is_seed_feed(feed: dict[str, Any]) -> bool:
    """字节 Seed 官网是 SPA：走 get_article_list_v2，不解析 HTML。"""
    sid = str(feed.get("id") or "").strip().lower()
    if sid in _SEED_SOURCE_IDS or sid.startswith("bytedance-seed"):
        return True
    extra = _feed_extra(feed)
    if extra.get("seed_api") or extra.get("seed_article_type"):
        return True
    host = _host(feed.get("url") or "")
    return host == "seed.bytedance.com"


def _ms_to_iso(ms: Any) -> str:
    try:
        val = int(ms)
    except (TypeError, ValueError):
        return ""
    if val > 10_000_000_000:  # ms
        val = val / 1000.0
    try:
        return datetime.fromtimestamp(val, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _fetch_seed_article_detail(article_id: Any) -> dict[str, Any]:
    aid = str(article_id or "").strip()
    if not aid:
        return {}
    try:
        resp = requests.get(
            _SEED_ARTICLE_DETAIL_API,
            params={"article_id": aid},
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "Referer": "https://seed.bytedance.com/en/blog",
            },
            timeout=min(config.JINA_TIMEOUT, 30),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Seed 详情 API 失败 %s: %s", aid, exc)
        return {}
    art = data.get("article") if isinstance(data, dict) else None
    return art if isinstance(art, dict) else {}


def _fetch_seed_items(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Seed 博客/发布：列表 API → 详情 API → /en/blog/<TitleKey>。

    extra_config:
      - seed_article_type: 默认 2=Blog（与官网 Blog 一致）
      - seed_locale: en | zh（详情链接语言，默认 en）
      - recent_days: 默认 30
      - max_articles
    """
    extra = _feed_extra(feed)
    try:
        article_type = int(extra.get("seed_article_type") or _SEED_ARTICLE_TYPE_BLOG)
    except (TypeError, ValueError):
        article_type = _SEED_ARTICLE_TYPE_BLOG
    locale = str(extra.get("seed_locale") or "en").strip().lower() or "en"
    if locale not in {"en", "zh"}:
        locale = "en"
    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)
    try:
        recent_days = float(extra.get("recent_days") or 30)
    except (TypeError, ValueError):
        recent_days = 30.0

    try:
        resp = requests.get(
            _SEED_ARTICLE_LIST_API,
            params={
                "article_type": article_type,
                "count": max(max_n * 2, 10),
                "order_desc": 1,
            },
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "Referer": f"https://seed.bytedance.com/{locale}/blog",
            },
            timeout=min(config.JINA_TIMEOUT, 30),
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Seed 列表 API 失败: %s", exc)
        return []

    rows = payload.get("sub_article_list") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("article_list") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        meta = row.get("ArticleMeta") if isinstance(row.get("ArticleMeta"), dict) else row
        en = row.get("ArticleSubContentEn") if isinstance(row.get("ArticleSubContentEn"), dict) else {}
        zh = row.get("ArticleSubContentZh") if isinstance(row.get("ArticleSubContentZh"), dict) else {}
        prefer = zh if locale == "zh" else en
        other = en if locale == "zh" else zh
        title = str(prefer.get("Title") or other.get("Title") or meta.get("Title") or "").strip()
        abstract = str(prefer.get("Abstract") or other.get("Abstract") or meta.get("Abstract") or "").strip()
        title_key = str(prefer.get("TitleKey") or other.get("TitleKey") or meta.get("TitleKey") or "").strip()
        article_id = meta.get("ArticleID") or meta.get("ID") or row.get("ID")
        # UpdateTime 可能因编辑旧文刷新，不能冒充首次发布时间。
        published_raw = _ms_to_iso(meta.get("PublishDate"))
        age = _age_days(_parse_iso_ms(published_raw))
        if age is None or age > recent_days:
            continue
        if not title_key and not article_id:
            continue

        detail = _fetch_seed_article_detail(article_id) if article_id else {}
        dmeta = detail.get("ArticleMeta") if isinstance(detail.get("ArticleMeta"), dict) else {}
        html_body = ""
        if locale == "zh":
            html_body = str(detail.get("ContentZh") or detail.get("Content") or "")
        else:
            html_body = str(detail.get("Content") or detail.get("ContentZh") or "")
        body_bits: list[str] = []
        if title:
            body_bits.append(title)
        if abstract:
            body_bits.append(abstract)
        if html_body.strip():
            body_bits.append(_html_to_text(html_body))
        for key in ("Markdown", "Body"):
            val = detail.get(key) or dmeta.get(key)
            if isinstance(val, str) and val.strip():
                body_bits.append(val.strip())
        body = "\n\n".join(body_bits)[:15000]
        if len(body) < 40:
            body = f"{title}\n{abstract}\nSeed article: {title_key or article_id}"[:15000]

        url = (
            f"https://seed.bytedance.com/{locale}/blog/{title_key}"
            if title_key
            else f"https://seed.bytedance.com/{locale}/blog"
        )
        items.append(
            {
                "title": (title or title_key or str(article_id))[:200],
                "url": url,
                "body": body,
                "published_raw": published_raw,
                "heat_keep": False,
                "is_html": False,
                "feed": feed,
            }
        )
        if len(items) >= max_n:
            break
    return items


# ---------------- GitHub 热榜专用抽取 ----------------
# GitHub 无官方 trending API；用 Search API 圈出「已沉淀 + 近期仍活跃」的高价值仓库，
# 按 星标 / Fork 采用度 / 主题相关性 打分，排除 awesome/教程/wrapper 类。
# 圈定之后还要过一道发布事件判定（见 _fetch_github_items）：窗口内发过 release，
# 或仓库本身是窗口内新建；否则只是老项目在日常提交，不构成热榜信号。
_GITHUB_DEFAULT_TOPICS = [
    "llm", "large-language-models", "llmops", "agent", "ai-agent", "ai-agents",
    "rag", "retrieval-augmented-generation", "inference", "llm-inference",
    "mlops", "transformers", "diffusion", "diffusion-models", "fine-tuning",
    "vector-database", "embeddings", "multimodal", "reasoning", "generative-ai",
    "deep-learning", "machine-learning", "nlp", "computer-vision",
    "text-to-image", "text-to-video", "model-serving", "quantization",
]
_GITHUB_DEFAULT_KEYWORD = re.compile(
    r"(llm|gpt|claude|gemini|llama|qwen|mistral|deepseek|agent|rag|inference|"
    r"transformer|diffusion|multimodal|fine[- ]?tun|embedding|vector|reasoning|"
    r"mlops|llmops|serving|quantiz|prompt|copilot|assistant|neural|"
    r"deep[- ]?learning|machine[- ]?learning|text-to-|speech|vision|"
    r"foundation model|generative)",
    re.I,
)
_GITHUB_EXCLUDE_TOPICS = {
    "awesome", "awesome-list", "book", "books", "tutorial", "tutorials",
    "roadmap", "interview", "interviews", "cheatsheet", "cheat-sheet",
    "course", "courses", "learning-resources",
}
_GITHUB_EXCLUDE_NAME_RE = re.compile(
    r"(awesome|roadmap|interview|cheat-?sheet|tutorial|handbook|course|"
    r"free-?code-?camp|500-?lines|教程|面试|资料)",
    re.I,
)
# 新建仓库路线额外跑的检索词个数：搜索 API 未鉴权时每分钟只有 10 次，和沉淀路线共用额度
_GITHUB_NEW_REPO_QUERIES = 3
# 新项目排序补偿：星标基数差一到两个数量级，不补权重就会被老项目全部压掉
_GITHUB_NEW_REPO_BONUS = 1.2
# 新项目在单轮产出里的占比上限
_GITHUB_NEW_REPO_SHARE = 0.3
# 新项目的采用度下限：只有星没有 fork 的多半是炒作贴带来的围观
_GITHUB_NEW_REPO_MIN_FORKS = 20
_GITHUB_WRAPPER_RE = re.compile(
    r"\b(wrapper|ui for|client for|sdk for|unofficial|mirror of|clone of|"
    r"gui for|telegram bot|discord bot|chrome extension)\b",
    re.I,
)


def _is_github_feed(feed: dict[str, Any]) -> bool:
    sid = str(feed.get("id") or "").strip().lower()
    if sid in _GITHUB_SOURCE_IDS or sid.startswith("github"):
        return True
    host = _host(feed.get("url") or "")
    return host == "github.com" and _path_of(feed.get("url") or "") in {"/trending", ""}


def _github_headers(*, raw: bool = False) -> dict[str, str]:
    accept = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    headers = {"User-Agent": _UA, "Accept": accept}
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _github_search(query: str, per_page: int) -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            _GITHUB_SEARCH_API,
            params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
            headers=_github_headers(),
            timeout=min(config.JINA_TIMEOUT, 30),
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("items") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("GitHub 搜索失败 [%s]: %s", query[:60], exc)
        return []


_MD_FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$")
_MD_FENCE_OPEN_RE = re.compile(r"(?ms)^[ \t]*(?:`{3,}|~{3,})[^\n]*\n.*\Z")
_PRE_CODE_RE = re.compile(r"(?is)<(pre|code)\b[^>]*>.*?</\1>")
_MD_LINK_ONLY_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
# README 里与「这个项目做了什么」无关的样板小节：装环境、许可、引用、贡献指南……
# 留着只会挤掉真正的项目介绍，翻译时还要按字符收费。
_README_SKIP_SECTION_RE = re.compile(
    r"^(?:"
    r"table\s+of\s+contents|contents|installation|install(?:ing)?|setup|requirements?|"
    r"prerequisites?|dependencies|quick\s*start|getting\s+started|usage|examples?|"
    r"development|developing|building|build\s+from\s+source|docker|deployment|deploy|"
    r"testing|tests|contributing|contributions?|contributors|code\s+of\s+conduct|"
    r"licen[sc]e|licensing|citation|citing|cite|acknowledge?ments?|"
    r"(?:seeking|getting)\s+help.*|community.*|contact(?:\s+us)?|media\s+kit.*|"
    r"stay\s+(?:up\s*to\s*date|updated|tuned).*|who(?:['’]s)?\s+using.*|disclaimer.*|"
    r"star\s+history|sponsors?|support|faq|changelog|release\s+notes|roadmap|security|"
    r"目录|安装|安装说明|部署|环境要求|依赖|快速开始|快速上手|快速入门|使用方法|使用说明|"
    r"用法|示例|样例|贡献|贡献指南|许可|许可证|开源许可|引用|致谢|更新日志|路线图|常见问题"
    r")\s*[:：]?$",
    re.I,
)


_NAV_SEP_RE = re.compile(r"[|·•/\\\-—–>»\s]+")
_ANCHOR_PAIR_RE = re.compile(r"(?is)<a\b.*?</a>")


def _readme_nav_kind(line: str) -> str:
    """给一行打标：link=只剩链接/徽章，row=挤在一行的语言切换器，pipe=以竖线收尾的短行。

    README 顶部普遍有「English | 简体中文 | 日本語 | …」，去标签后变成一串以「|」
    结尾的短行；前端把连续带竖线的行当表格渲染，正文里就凭空多出一张右列全空的表。
    真表格的单元格里有实义文字，据此和导航条区分开。
    """
    text = line.strip()
    if not text or len(text) > 400:
        return ""
    # 删掉整段链接（而不是留下链接文字）后还有实义文字，就是夹了链接的正常句子
    residue = _TAG_RE.sub("", _ANCHOR_PAIR_RE.sub("", _MD_LINK_ONLY_RE.sub("", text)))
    if not _NAV_SEP_RE.sub("", residue):
        # 「| 文档 | 博客 | 论文 |」这类整行都是链接的导航条同样以竖线开头，
        # 所以这一条要排在表格行的保护之前
        return "link"
    if text.startswith("|"):  # markdown 表格行：靠单元格里的实义文字保住
        return ""
    plain = _TAG_RE.sub("", _MD_LINK_ONLY_RE.sub(r"\1", text)).strip()
    cells = [cell for cell in (c.strip() for c in plain.split("|")) if cell]
    if len(cells) >= 3 and all(len(cell) <= 16 for cell in cells):
        return "row"
    if plain.endswith("|") and len(plain) <= 40:
        return "pipe"
    return ""


def _drop_readme_nav_lines(text: str) -> str:
    """剔掉语言切换器与徽章行。

    「以竖线收尾的短行」单独出现时可能是别的东西，只有和相邻的导航行连成一片
    才算——`<b>English</b> |` 这种没有链接的行正是靠邻居才认得出来。
    """
    lines = text.split("\n")
    kinds = [_readme_nav_kind(line) for line in lines]
    filled = [index for index, line in enumerate(lines) if line.strip()]
    drop: set[int] = set()
    for position, index in enumerate(filled):
        kind = kinds[index]
        if kind in {"link", "row"}:
            drop.add(index)
        elif kind == "pipe":
            neighbors = [
                kinds[filled[other]]
                for other in (position - 1, position + 1)
                if 0 <= other < len(filled)
            ]
            if any(neighbor in {"link", "row", "pipe"} for neighbor in neighbors):
                drop.add(index)
    return "\n".join(line for index, line in enumerate(lines) if index not in drop)


_MD_TABLE_DIVIDER_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")


def _normalize_md_tables(text: str) -> str:
    """markdown 表格改成与 HTML 表格一致的「单元格 | 单元格」写法。

    前端靠这个约定还原行列；原样留着 `|---|---|` 分隔行会多渲染出一行破折号。
    空单元格要保留，否则各行列数不一致，前端会判定不是表格。
    """
    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if _MD_TABLE_DIVIDER_RE.match(stripped):
            continue
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            kept.append(" | ".join(cells))
            continue
        kept.append(line)
    return "\n".join(kept)


def _drop_readme_boilerplate_sections(text: str) -> str:
    """按小节标题整段剔掉样板内容，保留项目介绍与能力说明。"""
    kept: list[str] = []
    skipping = False
    for line in text.split("\n"):
        heading = _HEADING_RE.match(line)
        name = heading.group(1) if heading else ""
        if not name:
            bare = line.strip()
            # HTML 标题经 html_to_text 后只剩独立成行的短文本，同样当标题看
            if bare and len(bare) <= 40 and _README_SKIP_SECTION_RE.match(bare):
                name = bare
        if name:
            skipping = bool(_README_SKIP_SECTION_RE.match(_one_line(name)))
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def cut_on_boundary(text: str, limit: int) -> str:
    """按段落/句子边界截断，避免正文断在词或句子中间。"""
    text = str(text or "")
    if len(text) <= limit:
        return text
    head = text[:limit]
    floor = limit * 0.5
    for sep in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", "; "):
        idx = head.rfind(sep)
        if idx >= floor:
            return head[: idx + len(sep)].rstrip()
    idx = head.rfind(" ")
    return (head[:idx] if idx >= floor else head).rstrip()


def readme_to_text(raw: str) -> str:
    """README 是 markdown 与 HTML 混排，按标签处理后再清 markdown 记号。

    直接删 ">" 会把 HTML 标签打散成 `<p align="center"` 这样的残片留在正文里；
    把空白全压成单空格则会让整篇变成一堵字墙，所以段落边界要保住。
    代码块要整块剔掉：去掉围栏后剩下的 `python -m venv .my-env` 会被当句子翻译。
    """
    text = re.sub(r"(?is)<!--.*?-->", " ", str(raw or ""))
    text = _MD_FENCE_RE.sub("\n", text)
    text = _MD_FENCE_OPEN_RE.sub("\n", text)  # 未闭合的围栏：截到文末
    text = _PRE_CODE_RE.sub(" ", text)
    text = _IMG_RE.sub("", text)  # 徽章与插图：正文只留文字，配图另抽
    text = _drop_readme_nav_lines(text)
    text = _html_to_text(text)
    # 标题记号在这一步之后才清，样板小节的识别要靠它
    text = _drop_readme_boilerplate_sections(text)
    text = _normalize_md_tables(text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s*[#>]+\s*", "", text)  # 标题/引用前缀只出现在行首
    text = re.sub(r"[*`~]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _github_readme_raw(full_name: str) -> str:
    """取仓库 README 原文（markdown/HTML 混排），失败降级空串。"""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{full_name}/readme",
            headers=_github_headers(raw=True),
            timeout=min(config.JINA_TIMEOUT, 20),
        )
        return resp.text if resp.status_code == 200 else ""
    except Exception:  # noqa: BLE001
        return ""


def _github_readme_excerpt(readme_raw: str, limit: int = README_EXCERPT_CHARS) -> str:
    return cut_on_boundary(readme_to_text(readme_raw), limit)


_MD_IMAGE_PAIR_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_GITHUB_BADGE_RE = re.compile(
    r"(?i)(shields\.io|badgen\.net|badge\.fury\.io|travis-ci|circleci|codecov|coveralls|"
    r"codacy|snyk\.io|opencollective|contrib\.rocks|visitor-badge|hits\.dwyl|star-history|"
    r"forthebadge|repostatus\.org)"
)


def _github_readme_images(
    readme_raw: str, full_name: str, limit: int = 3
) -> list[dict[str, str]]:
    """README 里的架构图/效果图：markdown 与 HTML 两种写法都要认。

    仓库内的相对路径在 README 里是相对仓库根的，直接当页面链接解析会 404，
    统一拼到 raw.githubusercontent.com 上。
    """
    from . import rss  # 延迟导入，避免采集模块之间的加载顺序耦合

    raw = str(readme_raw or "")
    # markdown 图片改写成 <img>，和 README 里混排的 HTML 图片共用同一套过滤
    html = _MD_IMAGE_PAIR_RE.sub(lambda m: f'<img src="{m.group(2)}" alt="{m.group(1)}">', raw)
    # 「/docs/x.png」这类根路径写法同样按仓库根解析
    html = re.sub(r'(?i)(<img\b[^>]*\bsrc=")/+', r"\1", html)
    images = rss.extract_article_images(
        html, f"https://raw.githubusercontent.com/{full_name}/HEAD/", limit=limit + 5
    )
    return [img for img in images if not _GITHUB_BADGE_RE.search(img["url"])][:limit]


def _github_issue_feedback(full_name: str, n: int = 4) -> str:
    """社区反响：按 reactions 排序取热门 issue（用户反馈/痛点），失败降级空串。"""
    try:
        resp = requests.get(
            "https://api.github.com/search/issues",
            params={
                "q": f"repo:{full_name} is:issue",
                "sort": "reactions",
                "order": "desc",
                "per_page": n,
            },
            headers=_github_headers(),
            timeout=min(config.JINA_TIMEOUT, 20),
        )
        if resp.status_code != 200:
            return ""
        items = (resp.json() or {}).get("items") or []
    except Exception:  # noqa: BLE001
        return ""
    lines: list[str] = []
    for it in items[:n]:
        reactions = int((it.get("reactions") or {}).get("total_count") or 0)
        comments = int(it.get("comments") or 0)
        if reactions == 0 and comments == 0:
            continue
        state = str(it.get("state") or "")
        title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()[:100]
        lines.append(f"- [{state}] 👍{reactions} 💬{comments} {title}")
    if not lines:
        return ""
    return "【社区反响·热门 Issue】\n" + "\n".join(lines)


_GH_RELEASE_CACHE: dict[str, dict[str, Any] | None] = {}
# 整份 feed 可能有几百 KB，只需要最新几条，按字节截断即可
_GITHUB_ATOM_MAX_BYTES = 262144
# 预发布标记有两种写法：紧贴版本号（v0.26.1rc0、1.12.0.dev11）和独立词（v2.0-beta）。
# 只认独立词会漏掉前者，只认紧贴写法会漏掉后者，两条都要有。
_GITHUB_PRERELEASE_RE = re.compile(
    r"(?i)(?:\d|[\s\-._])(?:rc|alpha|beta|preview|nightly|canary|snapshot|dev|pre)[\s\-._]?\d"
    r"|(?i:(?:^|[\s\-._])(?:rc|alpha|beta|preview|nightly|canary|snapshot|unstable)(?:$|[\s\-._]))"
)
_ATOM_ENTRY_HEAD_CHARS = 4000
_ATOM_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)
_ATOM_UPDATED_RE = re.compile(r"<updated>([^<]+)</updated>")
_ATOM_LINK_RE = re.compile(r'<link[^>]*href="([^"]+)"')
_ATOM_CONTENT_RE = re.compile(r"<content[^>]*>(.*?)</content>", re.S)


def _github_releases_atom(full_name: str) -> str:
    """取 releases.atom 原文，失败返回空串。"""
    try:
        with requests.get(
            f"https://github.com/{full_name}/releases.atom",
            headers={"User-Agent": _UA, "Accept": "application/atom+xml"},
            timeout=min(config.JINA_TIMEOUT, 20),
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                return ""
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=16384):
                buf.extend(chunk)
                if len(buf) >= _GITHUB_ATOM_MAX_BYTES:
                    break
            return buf.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def _github_latest_release(full_name: str) -> dict[str, Any] | None:
    """最新正式版本，取 releases.atom 里第一条非预发布条目。

    None = 取不到（网络异常）；{} = 确实没有正式发布。两者必须区分：把「取不到」
    当成「没发过版」，会把所有老项目一并判成无近期发布。

    不走 api.github.com/releases/latest 是因为它每个仓库要花一次核心额度，未鉴权时
    每小时只有 60 次，一轮就会被限流，而限流响应和「没有 release」长得一模一样。
    atom 由 github.com 直出，不受这个额度限制。
    """
    if full_name in _GH_RELEASE_CACHE:
        return _GH_RELEASE_CACHE[full_name]
    feed = _github_releases_atom(full_name)
    release: dict[str, Any] | None = None if not feed else {}
    for chunk in feed.split("<entry>")[1:]:
        # 元数据都排在 <content> 之前，即使正文被截断也能取到
        head = chunk[:_ATOM_ENTRY_HEAD_CHARS]
        title = _ATOM_TITLE_RE.search(head)
        updated = _ATOM_UPDATED_RE.search(head)
        if not (title and updated):
            continue
        tag = _one_line(unescape(title.group(1)))
        # atom 不区分正式版与预发布，rc/beta 不算对外发布
        if _GITHUB_PRERELEASE_RE.search(tag):
            continue
        link = _ATOM_LINK_RE.search(head)
        content = _ATOM_CONTENT_RE.search(chunk)
        release = {
            "tag_name": tag,
            "published_at": updated.group(1).strip(),
            "html_url": link.group(1) if link else "",
            "body": unescape(content.group(1)) if content else "",
        }
        break
    _GH_RELEASE_CACHE[full_name] = release
    return release


def _github_release_notes(release: dict[str, Any], limit: int = 1200) -> str:
    """release notes 摘录：说明这次版本到底改了什么，是热榜条目的核心信息。"""
    raw = str(release.get("body") or "")
    # atom 给的是渲染后的 HTML，API 给的是 markdown，两种都要能处理
    text = html_to_text(raw) if "<" in raw else readme_to_text(raw)
    return cut_on_boundary(text, limit)


_HF_API_FAILS = 0  # 连续失败熔断：HF api 被限流时避免整轮 N×timeout 卡顿


def _hf_paper_community(pid: str) -> tuple[str, dict[str, Any]]:
    """HF 论文社区反响：点赞数、被采用模型数、关联仓库 stars、HF AI 摘要。

    返回 (正文追加块, metrics 增量)；失败降级 ("", {})。
    连续失败 >=3 次即熔断本轮后续调用（HF api 限流时不拖慢流水线）。
    """
    global _HF_API_FAILS
    if _HF_API_FAILS >= 3:
        return "", {}
    try:
        resp = requests.get(
            f"https://huggingface.co/api/papers/{pid}",
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=min(config.JINA_TIMEOUT, 8),
        )
        if resp.status_code != 200:
            _HF_API_FAILS += 1
            return "", {}
        d = resp.json() or {}
        _HF_API_FAILS = 0
    except Exception:  # noqa: BLE001
        _HF_API_FAILS += 1
        return "", {}
    up = int(d.get("upvotes") or 0)
    models = int(d.get("numTotalModels") or 0)
    gh_stars = int(d.get("githubStars") or 0)
    ai_sum = re.sub(r"\s+", " ", str(d.get("ai_summary") or "")).strip()
    parts = [f"👍 社区点赞 {up}"]
    if models:
        parts.append(f"🔧 被 {models} 个模型采用")
    if gh_stars:
        parts.append(f"⭐ 关联仓库 {gh_stars} stars")
    block = "【社区反响】" + " | ".join(parts)
    if ai_sum:
        block += f"\nHF 社区 AI 摘要：{ai_sum[:600]}"
    metrics: dict[str, Any] = {
        "community_heat": float(up),
        "community_upvotes": up,
        "adoption_models": models,
        "linked_github_stars": gh_stars,
    }
    return block, metrics


def _fetch_github_items(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """GitHub 热榜：Search API 选出高价值仓库，再按「发布事件」定时间与去留。

    时间只认真实的发布事件——最新 Release，或仓库本身就是近期新建。pushed_at 只说明
    有人提交过代码，老项目改个 typo 也会刷新成今天，拿它当发布时间就会把 2018 年的
    项目当成今日新闻推出去。

    参数优先取「GitHub筛选配置」表（feed["github_config"]），回落 extra_config。
    """
    # 配置表参数覆盖 extra_config
    params: dict[str, Any] = dict(_feed_extra(feed))
    params.update(feed.get("github_config") or {})
    max_n = int(params.get("max_items") or feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)

    topics = [t.lower() for t in _as_str_list(params.get("topic_whitelist"))] or _GITHUB_DEFAULT_TOPICS
    langs = [l.lower() for l in _as_str_list(params.get("languages"))]
    try:
        kw_re = re.compile(params["keyword_regex"], re.I) if params.get("keyword_regex") else _GITHUB_DEFAULT_KEYWORD
    except (re.error, TypeError):
        kw_re = _GITHUB_DEFAULT_KEYWORD
    excl_topics = {t.lower() for t in _as_str_list(params.get("exclude_topics"))} or _GITHUB_EXCLUDE_TOPICS
    try:
        excl_name_re = (
            re.compile(params["exclude_name_regex"], re.I)
            if params.get("exclude_name_regex")
            else _GITHUB_EXCLUDE_NAME_RE
        )
    except (re.error, TypeError):
        excl_name_re = _GITHUB_EXCLUDE_NAME_RE

    def _num(key: str, default: float) -> float:
        try:
            v = params.get(key)
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    # 沉淀门槛：绝对星标 +「近 active_days 天有 push」，用于圈定值得关注的仓库池
    min_stars = int(_num("min_stars", 2000))
    active_days = int(_num("active_pushed_days", 90))
    min_forks = int(_num("min_forks", 0))
    wrapper_penalty = _num("wrapper_penalty", -0.5)
    # 发布窗口：仓库必须在窗口内发过新版本，或本身就是窗口内新建，否则不算热榜信号
    release_days = int(_num("release_recent_days", 30))
    min_new_stars = int(_num("min_new_repo_stars", 500))

    # REST 搜索 API 只支持 AND，不支持 OR/括号：每个关键词单独查询再合并。
    query_terms = _as_str_list(params.get("query_terms")) or [
        "llm", "agent", "rag", "inference", "diffusion", "multimodal",
    ]
    now = datetime.now(timezone.utc)
    active_date = (now - timedelta(days=active_days)).strftime("%Y-%m-%d")
    created_date = (now - timedelta(days=release_days)).strftime("%Y-%m-%d")
    per_page = min(max(max_n * 3, 30), 50)

    rows: dict[str, dict[str, Any]] = {}
    for term in query_terms[:6]:
        phrase = f'"{term}"' if " " in term else term
        q = f"{phrase} in:name,description,topics pushed:>={active_date} stars:>={min_stars}"
        for r in _github_search(q, per_page=per_page):
            fn = str(r.get("full_name") or "")
            if fn and fn not in rows:
                rows[fn] = r
    # 新建仓库单独一路：套沉淀星标线的话，刚发布的项目永远够不到，热榜就只剩老面孔
    for term in query_terms[:_GITHUB_NEW_REPO_QUERIES]:
        phrase = f'"{term}"' if " " in term else term
        q = f"{phrase} in:name,description,topics created:>={created_date} stars:>={min_new_stars}"
        for r in _github_search(q, per_page=per_page):
            fn = str(r.get("full_name") or "")
            if fn and fn not in rows:
                rows[fn] = r

    scored: list[tuple[float, int, bool, dict[str, Any]]] = []
    for fn, r in rows.items():
        stars = int(r.get("stargazers_count") or 0)
        forks = int(r.get("forks_count") or 0)
        created_age = _age_days(_parse_iso_ms(r.get("created_at")))
        is_new_repo = created_age is not None and created_age <= release_days
        # 新仓库还没时间攒星和 fork，套沉淀门槛等于把「刚发布」这一路全部挡掉
        if stars < (min_new_stars if is_new_repo else min_stars):
            continue
        # 炒作型新仓能刷到星，但很少有人真去 fork 使用，用 fork 数挡掉这一类
        if forks < (_GITHUB_NEW_REPO_MIN_FORKS if is_new_repo else min_forks):
            continue
        name = str(r.get("name") or "")
        desc = str(r.get("description") or "")
        repo_topics = [str(t).lower() for t in (r.get("topics") or [])]
        lang = str(r.get("language") or "")
        if excl_name_re.search(fn) or excl_name_re.search(name):
            continue
        if any(t in excl_topics for t in repo_topics):
            continue
        haystack = f"{fn} {desc} {' '.join(repo_topics)}"
        topic_hit = any(t in topics for t in repo_topics)
        if not (topic_hit or kw_re.search(haystack)):
            continue
        if langs and lang and lang.lower() not in langs:
            continue

        # 打分：星标为主 + Fork 采用度 + 主题相关 + 近期活跃，无增速项
        score = math.log10(stars + 1) + 0.5 * math.log10(forks + 1)
        if topic_hit:
            score += 0.5
        if is_new_repo:
            # 与几万星的老项目同池排序，新项目不补权重就永远排不进来
            score += _GITHUB_NEW_REPO_BONUS
        push_age = _age_days(_parse_iso_ms(r.get("pushed_at")))
        if push_age is not None and push_age <= active_days:
            score += 0.3
        if _GITHUB_WRAPPER_RE.search(haystack):
            score += wrapper_penalty
        scored.append((score, stars, is_new_repo, r))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    items: list[dict[str, Any]] = []
    stale = 0
    unavailable = 0
    lookups = 0
    new_repo_count = 0
    # 每个仓库一次 atom 请求，候选池几百个时不能挨个查
    max_lookups = max(3 * max_n, 30)
    # 新仓上限：新项目普遍带投机噪音，放开配额会把整份热榜挤成「刚建的仓库」
    max_new_repos = max(1, round(max_n * _GITHUB_NEW_REPO_SHARE))
    for score, stars, is_new_repo, r in scored:
        if len(items) >= max_n:
            break
        if is_new_repo and new_repo_count >= max_new_repos:
            continue
        fn = str(r.get("full_name") or "")
        desc = str(r.get("description") or "").strip()
        lang = str(r.get("language") or "")
        forks = int(r.get("forks_count") or 0)
        repo_topics = [str(t) for t in (r.get("topics") or [])]
        pushed_raw = str(r.get("pushed_at") or r.get("updated_at") or "").strip()
        created_raw = str(r.get("created_at") or "").strip()
        repo_url = str(r.get("html_url") or f"https://github.com/{fn}")

        if is_new_repo:
            title, url = fn, repo_url
            published_raw = created_raw
            event_line = f"🌱 新项目｜创建于 {created_raw[:10]}"
            release_notes = ""
            new_repo_count += 1
        else:
            if lookups >= max_lookups:
                continue
            release = _github_latest_release(fn)
            lookups += 1
            if release is None:
                # 取不到就说不清有没有发版，宁可不收，也不要按「没发版」误判
                unavailable += 1
                continue
            published_raw = str(release.get("published_at") or "").strip()
            release_age = _age_days(_parse_iso_ms(published_raw))
            # 老项目只有日常提交、窗口内没发过版本：是活跃，不是新闻
            if release_age is None or release_age > release_days:
                stale += 1
                continue
            tag = str(release.get("tag_name") or "").strip()
            title = f"{fn} {tag}".strip()
            # 指向 release 页而非仓库首页：同一仓库的不同版本才会被当成不同条目
            url = str(release.get("html_url") or repo_url)
            event_line = f"🚀 新版本 {tag}｜发布于 {published_raw[:10]}"
            release_notes = _github_release_notes(release)

        readme_raw = _github_readme_raw(fn)
        readme = _github_readme_excerpt(readme_raw)
        readme_images = _github_readme_images(readme_raw, fn)
        feedback = _github_issue_feedback(fn)  # 社区反响：热门 issue 用户反馈
        body = cut_on_boundary(
            (
                f"{desc}\n\n"
                f"{event_line}\n"
                f"⭐ Stars: {stars} | 🍴 Forks: {forks} | 语言: {lang or 'N/A'} | "
                f"主题: {', '.join(repo_topics) or 'N/A'}\n"
                f"创建: {created_raw[:10]} | 最近提交: {pushed_raw[:10]} | 沉淀分: {score:.2f}\n\n"
                + (f"【本次发布】\n{release_notes}\n\n" if release_notes else "")
                + (f"{feedback}\n\n" if feedback else "")
                + f"{readme}"
            ).strip(),
            15000,
        )
        items.append(
            {
                "title": title[:200],
                "url": url,
                "body": body,
                # 卡片首图留空时由 daily 回落到仓库社交预览图（OG）
                "image_url": readme_images[0]["url"] if readme_images else "",
                "media_assets": {"images": readme_images, "videos": []},
                # 发布事件时间，不是 pushed_at
                "published_raw": published_raw,
                # 发布窗口已在本函数内判定，lookback 再卡一次会把窗口压回 7 天
                "heat_keep": True,
                "is_html": False,
                "feed": feed,
            }
        )
    log.info(
        "GitHub 命中 %d 仓库，选取 %d（其中新项目 %d）；发布窗口<=%dd，"
        "无近期发布跳过 %d，发布信息取不到跳过 %d",
        len(scored), len(items), new_repo_count, release_days, stale, unavailable,
    )
    return items


def _is_json_api_feed(feed: dict[str, Any]) -> bool:
    return _is_modelscope_feed(feed) or _is_seed_feed(feed) or _is_github_feed(feed)


def _fetch_json_api_items(feed: dict[str, Any]) -> list[dict[str, Any]]:
    if _is_seed_feed(feed):
        return _fetch_seed_items(feed)
    if _is_modelscope_feed(feed):
        return _fetch_modelscope_items(feed)
    if _is_github_feed(feed):
        return _fetch_github_items(feed)
    return []


def _extract_links_for_feed(page: str, feed: dict[str, Any], *, use_jina: bool) -> list[dict[str, Any]]:
    if _is_hf_pwc_paper_feed(feed):
        return _extract_hf_pwc_paper_links(page, feed)
    if str(_feed_extra(feed).get("list_parser") or "").strip() == "zhipu_news":
        return _extract_zhipu_news_links(page, feed)
    return _extract_links(page, feed) if use_jina else _extract_links_html(page, feed)


def _extract_zhipu_news_links(html: str, feed: dict[str, Any]) -> list[dict[str, str]]:
    """从智谱 SSR 新闻卡片保留真实标题和日期，避免按数字 URL 错排。"""
    from urllib.parse import urljoin

    src_url = str(feed.get("url") or "")
    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)
    anchors = list(
        re.finditer(
            r"""<a\b[^>]*\bhref=["']([^"']*/(?:zh|en)/news/\d+)["'][^>]*>""",
            html or "",
            re.I,
        )
    )
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, anchor in enumerate(anchors):
        url = urljoin(src_url, anchor.group(1)).split("#")[0]
        if url in seen:
            continue
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(html)
        card = html[anchor.end() : end]
        title_match = re.search(r"(?is)<h[1-4]\b[^>]*>(.*?)</h[1-4]>", card)
        if not title_match:
            title_match = re.search(r"""(?is)<img\b[^>]*\balt=["']([^"']+)["']""", card)
        date_match = re.search(
            r"(?is)<p\b[^>]*>\s*(20\d{2}[/-]\d{1,2}[/-]\d{1,2})\s*</p>",
            card,
        )
        title = _one_line(_html_to_text(title_match.group(1))) if title_match else ""
        published = _one_line(date_match.group(1)) if date_match else ""
        if not title:
            continue
        seen.add(url)
        links.append({"url": url, "title": title, "published_raw": published})
        if len(links) >= max_n:
            break
    return links


def _extract_links_html(html: str, feed: dict[str, Any]) -> list[dict[str, str]]:
    """从 HTML 抽出同域文章链接（direct 引擎）。"""
    from urllib.parse import urljoin

    src_url = feed["url"]
    src_host = _host(src_url)
    list_path = _path_of(src_url)
    strict = len(list_path) > 1
    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)
    seen: set[str] = set()
    cand: list[dict[str, str]] = []
    for m in _HREF_RE.finditer(html or ""):
        raw = m.group(1).strip()
        url = urljoin(src_url, raw)
        url = re.sub(r"[).,]+$", "", url.split("#")[0])
        if _SOCIAL.search(url):
            continue
        if _host(url) != src_host:
            continue
        path = _path_of(url)
        if not path or url.rstrip("/") == src_url.rstrip("/"):
            continue
        if not _link_depth_ok(path, feed, strict=strict, list_path=list_path):
            continue
        if not _path_allowed(path, feed):
            continue
        if url in seen:
            continue
        seen.add(url)
        # 用路径末段当临时标题
        title = segs[-1].replace("-", " ").replace("_", " ") if (segs := [s for s in path.split("/") if s]) else url
        cand.append({"url": url, "title": title[:120]})
    cand.sort(key=lambda x: _link_recency_key(x["url"]), reverse=True)
    return cand[:max_n]


def _build_item_direct(html: str, link: dict[str, Any], feed: dict[str, Any]) -> dict[str, Any] | None:
    if not html:
        return None
    title = link.get("title") or ""
    mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if mt:
        page_title = _one_line(_html_to_text(mt.group(1)))
        page_title = re.sub(r"^Paper page\s*[-–—]\s*", "", page_title, flags=re.I).strip()
        title = page_title or title
    if _feed_extra(feed).get("article_title_from_h1"):
        heading = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", html)
        if heading:
            title = _one_line(_html_to_text(heading.group(1))) or title
    block = str(link.get("community_block") or "")
    url = link.get("url") or ""
    # 与 RSS 回源同一套抽取：整页去标签会把导航栏、栏目名、页脚当成正文
    from . import rss  # 延迟导入，避免采集模块之间的加载顺序耦合

    parsed = rss.parse_article_html(
        html, url, title, limit=(15000 - len(block) - 2) if block else 15000
    )
    content = parsed["text"]
    if block:
        content = f"{content}\n\n{block}"
    if not title or not url or len(content) < 40:
        return None
    published = str(link.get("published_raw") or "").strip() or extract_published_date_html(html)
    item = {
        "title": title,
        "url": url,
        "body": content,
        "media_assets": {"images": parsed["images"], "videos": []},
        "published_raw": published,
        "heat_keep": bool(link.get("heat_keep")),
        "is_html": True,
        "feed": feed,
    }
    if link.get("metrics"):
        item["metrics"] = dict(link["metrics"])
    return item

def _host(u: str) -> str:
    m = re.match(r"^https?://([^/?#]+)", str(u or ""), re.I)
    return re.sub(r"^www\.", "", m.group(1), flags=re.I).lower() if m else ""


def _path_of(u: str) -> str:
    m = re.match(r"^https?://[^/?#]+([^?#]*)", str(u or ""), re.I)
    return re.sub(r"/+$", "", m.group(1) if m else "")


@retry(stop=stop_after_attempt(config.HTTP_MAX_TRIES), wait=wait_fixed(config.HTTP_WAIT_SECONDS))
def _jina_get(url: str, list_mode: bool) -> str:
    headers: dict[str, str] = {}
    if config.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"
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
        if not _link_depth_ok(path, feed, strict=strict, list_path=list_path):
            continue
        if not _path_allowed(path, feed):
            continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        cand.append({"url": key, "title": title})
    cand.sort(key=lambda x: _link_recency_key(x["url"]), reverse=True)
    return cand[:max_n]


def _build_item(article_md: str, link: dict[str, Any], feed: dict[str, Any]) -> dict[str, Any] | None:
    """对应 Build Scrape Items：把 Jina 正文组装成统一 RawItem。"""
    body = str(article_md or "")
    if not body:
        return None
    title = link.get("title") or ""
    published = str(link.get("published_raw") or "")

    mt = re.search(r"^Title:\s*(.+)$", body, re.M)
    if mt and mt.group(1).strip():
        page_title = mt.group(1).strip()
        page_title = re.sub(r"^Paper page\s*[-–—]\s*", "", page_title, flags=re.I).strip()
        title = page_title or title
    if not published:
        published = _published_date_from_jina(body)
    url = link.get("url") or ""
    block = str(link.get("community_block") or "")
    # 与 RSS 的 Jina 兜底走同一套还原：只做 _strip_md 会把正文插图一并删掉，
    # 于是 Jina 引擎抓来的条目一张图都没有
    from . import rss  # 延迟导入，避免采集模块之间的加载顺序耦合

    parsed = rss.parse_jina_markdown(
        body, url, title, (15000 - len(block) - 2) if block else 15000
    )
    content = parsed["text"]
    if block:
        content = f"{content}\n\n{block}"
    if not title or not url:
        return None
    item = {
        "title": title,
        "url": url,
        "body": content,
        "media_assets": {"images": parsed["images"], "videos": []},
        "published_raw": published,
        "heat_keep": bool(link.get("heat_keep")),
        "is_html": False,
        "feed": feed,
    }
    if link.get("metrics"):
        item["metrics"] = dict(link["metrics"])
    return item


def fetch_scrape_sources(feeds: list[dict[str, Any]], *, engine: str = "jina") -> list[dict[str, Any]]:
    """完整 Scrape 流水线：并发抓列表页 → 抽链接 → 并发抓正文 → 组装。"""
    items, _stats = fetch_scrape_sources_with_stats(feeds, engine=engine)
    return items


def fetch_scrape_sources_with_stats(
    feeds: list[dict[str, Any]],
    *,
    engine: str = "jina",
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """与 fetch_scrape_sources 相同，额外返回分源统计。

    engine: jina | direct | auto（auto 先探测 Jina，不可达则整批改 direct）
    """
    stats: dict[str, dict[str, Any]] = {}
    if not feeds:
        return [], stats

    resolved = (engine or "jina").strip().lower()
    if resolved == "auto":
        ok = probe_jina()
        resolved = "jina" if ok else "direct"
        log.info("Scrape engine=auto → %s（Jina %s）", resolved, "可达" if ok else "不可达")

    use_jina = resolved == "jina"
    for feed in feeds:
        sid = str(feed.get("id") or feed.get("url") or "")
        stats[sid] = {
            "source_id": sid,
            "engine": resolved,
            "list_ok": False,
            "list_chars": 0,
            "links": 0,
            "article_ok": 0,
            "article_fail": 0,
            "timing_ms": {"list": 0.0, "articles": 0.0, "total": 0.0},
            "error": None,
        }

    t0 = time.perf_counter()
    raw_items: list[dict[str, Any]] = []

    # SPA JSON API 源（魔搭 / Seed）：跳过 HTML 列表/正文
    html_feeds: list[dict[str, Any]] = []
    for feed in feeds:
        sid = str(feed.get("id") or feed.get("url") or "")
        if not _is_json_api_feed(feed):
            html_feeds.append(feed)
            continue
        lt0 = time.perf_counter()
        api_items = _fetch_json_api_items(feed)
        list_ms = (time.perf_counter() - lt0) * 1000
        st = stats[sid]
        st["timing_ms"]["list"] = round(list_ms, 1)
        st["timing_ms"]["articles"] = 0.0
        st["timing_ms"]["total"] = round(list_ms, 1)
        st["list_ok"] = True
        st["list_chars"] = len(api_items)
        st["links"] = len(api_items)
        st["article_ok"] = len(api_items)
        if not api_items:
            st["error"] = "no_links_extracted"
        else:
            st["error"] = None
            raw_items.extend(api_items)

    def _fetch_list(feed: dict[str, Any]) -> tuple[dict[str, Any], str, float]:
        lt0 = time.perf_counter()
        fj = use_jina and not _feed_force_direct(feed)
        body = _safe_jina_get(feed["url"], True) if fj else _safe_direct_get(feed["url"])
        return feed, body, (time.perf_counter() - lt0) * 1000

    list_results: list[tuple[dict[str, Any], str, float]] = []
    if html_feeds:
        with ThreadPoolExecutor(max_workers=config.JINA_CONCURRENCY) as pool:
            list_results = list(pool.map(_fetch_list, html_feeds))

    tasks: list[tuple[dict[str, str], dict[str, Any]]] = []
    for feed, page, list_ms in list_results:
        sid = str(feed.get("id") or feed.get("url") or "")
        st = stats[sid]
        st["timing_ms"]["list"] = round(list_ms, 1)
        if not page:
            st["error"] = "list_empty_or_failed"
            continue
        st["list_ok"] = True
        st["list_chars"] = len(page)
        links = _extract_links_for_feed(page, feed, use_jina=use_jina and not _feed_force_direct(feed))
        st["links"] = len(links)
        if not links:
            st["error"] = "no_links_extracted"
        for link in links:
            tasks.append((link, feed))

    log.info("Scrape 待抓正文 %d 篇（源 %d, engine=%s）", len(tasks), len(feeds), resolved)

    article_ms_by_source: dict[str, float] = {sid: 0.0 for sid in stats}

    def _fetch_article(task: tuple[dict[str, str], dict[str, Any]]) -> tuple[dict, dict, str, float]:
        link, feed = task
        at0 = time.perf_counter()
        fj = use_jina and not _feed_force_direct(feed)
        body = _safe_jina_get(link["url"], False) if fj else _safe_direct_get(link["url"])
        if fj and not link.get("published_raw") and not _published_date_from_jina(body):
            published = _fetch_direct_published_date(link["url"])
            if published:
                link = {**link, "published_raw": published}
        return link, feed, body, (time.perf_counter() - at0) * 1000

    articles: list[tuple[dict, dict, str, float]] = []
    if tasks:
        with ThreadPoolExecutor(max_workers=config.JINA_CONCURRENCY) as pool:
            articles = list(pool.map(_fetch_article, tasks))

    for link, feed, article_body, art_ms in articles:
        sid = str(feed.get("id") or feed.get("url") or "")
        article_ms_by_source[sid] = article_ms_by_source.get(sid, 0.0) + art_ms
        item = (
            _build_item(article_body, link, feed)
            if (use_jina and not _feed_force_direct(feed))
            else _build_item_direct(article_body, link, feed)
        )
        if item:
            stats[sid]["article_ok"] += 1
            raw_items.append(item)
        else:
            stats[sid]["article_fail"] += 1

    total_wall = (time.perf_counter() - t0) * 1000
    for sid, st in stats.items():
        feed_obj = next((f for f in feeds if str(f.get("id") or f.get("url") or "") == sid), None)
        if feed_obj and _is_json_api_feed(feed_obj):
            continue
        st["timing_ms"]["articles"] = round(article_ms_by_source.get(sid, 0.0), 1)
        st["timing_ms"]["total"] = round(
            float(st["timing_ms"]["list"]) + float(st["timing_ms"]["articles"]), 1
        )
        if st["list_ok"] and st["links"] and st["article_ok"] == 0 and st["article_fail"] > 0:
            st["error"] = st.get("error") or "articles_failed"
        elif st["list_ok"] and st["links"] and st["article_ok"] > 0:
            st["error"] = None

    log.info(
        "Scrape 完成：源 %d，正文成功 %d，墙钟 %.1fs，engine=%s",
        len(feeds),
        len(raw_items),
        total_wall / 1000,
        resolved,
    )
    return raw_items, stats
