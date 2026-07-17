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
_DEFAULT_RECENT_DAYS = 7
_DEFAULT_HIGH_UPVOTES = 100
_DEFAULT_HIGH_STARS_24H = 100
_DEFAULT_MIN_UPVOTES = 0


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


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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
    }


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
        published_raw = str(row.get("date_published") or "").strip()
        age = row.get("paper_age_days")
        try:
            age_f = float(age) if age is not None else None
        except (TypeError, ValueError):
            age_f = None
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
        keep, heat_keep = _keep_trending_paper(
            published_raw=str(row.get("published_raw") or ""),
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
                "published_raw": str(row.get("published_raw") or ""),
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
        published_raw = str(row.get("last_modified") or row.get("created_at") or "").strip()
        age = _age_days(_parse_iso_ms(published_raw))
        if age is not None and age > recent_days:
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
            body = f"{title}\nModelScope model: {mid}\nUpdated: {published_raw}"[:15000]

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
        published_raw = _ms_to_iso(meta.get("PublishDate") or meta.get("UpdateTime"))
        age = _age_days(_parse_iso_ms(published_raw))
        if age is not None and age > recent_days:
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


# ---------------- GitHub 热榜专用抽取（纯沉淀型）----------------
# GitHub 无官方 trending API；用 Search API 只取「已沉淀 + 近期仍活跃」的高价值仓库：
# 绝对星标达标 + 近 N 天有 push，按 星标 / Fork 采用度 / 主题相关性 / 活跃度 打分，
# 不再引入增速(星/龄)与「爆发新仓」路线，排除 awesome/教程/wrapper 类。
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


def _github_readme_excerpt(full_name: str, limit: int = 1500) -> str:
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{full_name}/readme",
            headers=_github_headers(raw=True),
            timeout=min(config.JINA_TIMEOUT, 20),
        )
        if resp.status_code != 200:
            return ""
        text = _IMG_RE.sub("", resp.text)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"[#>*`_~|]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:limit]
    except Exception:  # noqa: BLE001
        return ""


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
    """GitHub 热榜（纯沉淀型）：Search API 取已沉淀且近期活跃的高星仓库 + 打分，直出 RawItem。

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

    # 纯沉淀型：只有「绝对星标门槛」+「近 active_days 天有 push」两道硬约束，无爆发/增速路线
    min_stars = int(_num("min_stars", 2000))
    active_days = int(_num("active_pushed_days", 90))
    min_forks = int(_num("min_forks", 0))
    wrapper_penalty = _num("wrapper_penalty", -0.5)

    # REST 搜索 API 只支持 AND，不支持 OR/括号：每个关键词单独查询再合并。
    query_terms = _as_str_list(params.get("query_terms")) or [
        "llm", "agent", "rag", "inference", "diffusion", "multimodal",
    ]
    now = datetime.now(timezone.utc)
    active_date = (now - timedelta(days=active_days)).strftime("%Y-%m-%d")
    per_page = min(max(max_n * 3, 30), 50)

    rows: dict[str, dict[str, Any]] = {}
    for term in query_terms[:6]:
        phrase = f'"{term}"' if " " in term else term
        q = f"{phrase} in:name,description,topics pushed:>={active_date} stars:>={min_stars}"
        for r in _github_search(q, per_page=per_page):
            fn = str(r.get("full_name") or "")
            if fn and fn not in rows:
                rows[fn] = r

    scored: list[tuple[float, int, dict[str, Any]]] = []
    for fn, r in rows.items():
        stars = int(r.get("stargazers_count") or 0)
        forks = int(r.get("forks_count") or 0)
        # 硬门槛：绝对星标 + Fork 采用度（沉淀信号）
        if stars < min_stars or forks < min_forks:
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
        push_age = _age_days(_parse_iso_ms(r.get("pushed_at")))
        if push_age is not None and push_age <= active_days:
            score += 0.3
        if _GITHUB_WRAPPER_RE.search(haystack):
            score += wrapper_penalty
        scored.append((score, stars, r))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    items: list[dict[str, Any]] = []
    for score, stars, r in scored[:max_n]:
        fn = str(r.get("full_name") or "")
        desc = str(r.get("description") or "").strip()
        lang = str(r.get("language") or "")
        forks = int(r.get("forks_count") or 0)
        repo_topics = [str(t) for t in (r.get("topics") or [])]
        pushed_raw = str(r.get("pushed_at") or r.get("updated_at") or "").strip()
        created_raw = str(r.get("created_at") or "").strip()
        readme = _github_readme_excerpt(fn)
        feedback = _github_issue_feedback(fn)  # 社区反响：热门 issue 用户反馈
        body = (
            f"{desc}\n\n"
            f"⭐ Stars: {stars} | 🍴 Forks: {forks} | 语言: {lang or 'N/A'} | "
            f"主题: {', '.join(repo_topics) or 'N/A'}\n"
            f"创建: {created_raw[:10]} | 最近提交: {pushed_raw[:10]} | 沉淀分: {score:.2f}\n\n"
            + (f"{feedback}\n\n" if feedback else "")
            + f"{readme}"
        ).strip()[:15000]
        items.append(
            {
                "title": fn[:200],
                "url": str(r.get("html_url") or f"https://github.com/{fn}"),
                "body": body,
                "published_raw": pushed_raw or created_raw,
                "heat_keep": True,  # 热榜=沉淀热度信号而非时效，跳过 lookback
                "is_html": False,
                "feed": feed,
            }
        )
    log.info("GitHub 命中 %d 仓库，选取 %d（min_stars=%d, active<=%dd）", len(scored), len(items), min_stars, active_days)
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
    return _extract_links(page, feed) if use_jina else _extract_links_html(page, feed)


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
        page_title = _html_to_text(mt.group(1)) or ""
        page_title = re.sub(r"^Paper page\s*[-–—]\s*", "", page_title, flags=re.I).strip()
        title = page_title or title
    block = str(link.get("community_block") or "")
    content = _html_to_text(html)[: (15000 - len(block) - 2) if block else 15000]
    if block:
        content = f"{content}\n\n{block}"
    url = link.get("url") or ""
    if not title or not url or len(content) < 40:
        return None
    item = {
        "title": title,
        "url": url,
        "body": content,
        "published_raw": str(link.get("published_raw") or ""),
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


def _strip_md(s: str) -> str:
    s = _IMG_RE.sub("", str(s or ""))
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^[#>*`\-\s]+", "", s, flags=re.M)
    s = re.sub(r"[`*_]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _build_item(article_md: str, link: dict[str, Any], feed: dict[str, Any]) -> dict[str, Any] | None:
    """对应 Build Scrape Items：把 Jina 正文组装成统一 RawItem。"""
    body = str(article_md or "")
    if not body:
        return None
    title = link.get("title") or ""
    published = str(link.get("published_raw") or "")
    content = body

    mt = re.search(r"^Title:\s*(.+)$", body, re.M)
    if mt and mt.group(1).strip():
        page_title = mt.group(1).strip()
        page_title = re.sub(r"^Paper page\s*[-–—]\s*", "", page_title, flags=re.I).strip()
        title = page_title or title
    mp = re.search(r"^Published Time:\s*(.+)$", body, re.M)
    if mp and not published:
        published = mp.group(1).strip()
    marker = body.find("Markdown Content:")
    if marker >= 0:
        content = body[marker + len("Markdown Content:") :]

    block = str(link.get("community_block") or "")
    content = _strip_md(content)[: (15000 - len(block) - 2) if block else 15000]
    if block:
        content = f"{content}\n\n{block}"
    url = link.get("url") or ""
    if not title or not url:
        return None
    item = {
        "title": title,
        "url": url,
        "body": content,
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
