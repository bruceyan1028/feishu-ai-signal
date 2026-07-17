"""解析飞书「源配置表」记录 → 内部 feed 对象。

对应 n8n 节点：Map Feed Sources / Map Scrape Sources
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from . import config

FEED_METHODS = {"RSS"}
_B_SET = {"chatbot-arena", "artificial-analysis", "papers-with-code-sota"}

# 信号载体类型（写入条目表「来源类型」）：按内容形态分类，不再用 Research/Company Blog 这类主题标签
SIGNAL_FORMAT_PAPER = "论文"
SIGNAL_FORMAT_WEB = "纯网页"
SIGNAL_FORMAT_VIDEO = "视频"
SIGNAL_FORMAT_SOCIAL = "社交媒体"
SIGNAL_FORMAT_WECHAT = "公众号"
SIGNAL_FORMAT_PODCAST = "播客"
SIGNAL_FORMAT_GITHUB = "Github热榜"
SIGNAL_FORMAT_OTHER = "其他"

_ENTITY_TO_FORMAT = {
    "paper": SIGNAL_FORMAT_PAPER,
    "wechat": SIGNAL_FORMAT_WECHAT,
    "video": SIGNAL_FORMAT_VIDEO,
    "social": SIGNAL_FORMAT_SOCIAL,
    "github": SIGNAL_FORMAT_GITHUB,
}

_FORMAT_ALIASES = {
    "论文": SIGNAL_FORMAT_PAPER,
    "paper": SIGNAL_FORMAT_PAPER,
    "research": SIGNAL_FORMAT_PAPER,
    "纯网页": SIGNAL_FORMAT_WEB,
    "web": SIGNAL_FORMAT_WEB,
    "company blog": SIGNAL_FORMAT_WEB,
    "tech news": SIGNAL_FORMAT_WEB,
    "media": SIGNAL_FORMAT_WEB,
    "report": SIGNAL_FORMAT_WEB,
    "product": SIGNAL_FORMAT_WEB,
    "policy": SIGNAL_FORMAT_WEB,
    "视频": SIGNAL_FORMAT_VIDEO,
    "video": SIGNAL_FORMAT_VIDEO,
    "社交媒体": SIGNAL_FORMAT_SOCIAL,
    "social": SIGNAL_FORMAT_SOCIAL,
    "公众号": SIGNAL_FORMAT_WECHAT,
    "wechat": SIGNAL_FORMAT_WECHAT,
    "播客": SIGNAL_FORMAT_PODCAST,
    "podcast": SIGNAL_FORMAT_PODCAST,
    "github热榜": SIGNAL_FORMAT_GITHUB,
    "github": SIGNAL_FORMAT_GITHUB,
    "github-trending": SIGNAL_FORMAT_GITHUB,
    "其他": SIGNAL_FORMAT_OTHER,
    "other": SIGNAL_FORMAT_OTHER,
}

_PAPER_IDS = {
    "openreview",
    "hf-papers-trending",
    "papers-with-code-trending",
    "papers-with-code-sota",
    "papers-with-code-api",
    "semantic-scholar",
    "jmlr",
    "nature-machine-intelligence",
    "nature-computational-science",
    "google-scholar",
    "conference-authors",
    "arxiv-comments",
}
_VIDEO_IDS = {"video-channels"}
_SOCIAL_IDS = {"social-media"}
_WECHAT_MARKERS = ("wechat", "weixin", "公众号")
_PODCAST_MARKERS = ("podcast", "播客")


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
    m = re.search(r"(\d+(?:\.\d+)?)\s*d", s)
    if m:
        return int(float(m.group(1)) * 24)
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
    return quote(url, safe=":/?&=%+#")


def _parse_extra(f: dict[str, Any]) -> dict[str, Any] | None:
    try:
        raw = cell(f.get("extra_config"))
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


def normalize_signal_format(raw: Any) -> str | None:
    """把飞书「来源类型」归一成标准枚举；空值返回 None。"""
    if raw is None:
        return None
    s = str(cell(raw) or "").strip()
    if not s:
        return None
    return _FORMAT_ALIASES.get(s) or _FORMAT_ALIASES.get(s.lower()) or s


def infer_signal_format(
    source_id: str,
    *,
    endpoint: str = "",
    extra: Any = None,
    fetch_method: str = "",
    entity_type: str | None = None,
    explicit_type: str | None = None,
) -> str:
    """推断信号载体类型：论文 / 纯网页 / 视频 / 社交媒体 / 公众号 / 播客 / 其他。

    优先级：显式「来源类型」字段 > typed config entity_type > id/URL 启发式。
    """
    explicit = normalize_signal_format(explicit_type)
    if explicit in {
        SIGNAL_FORMAT_PAPER,
        SIGNAL_FORMAT_WEB,
        SIGNAL_FORMAT_VIDEO,
        SIGNAL_FORMAT_SOCIAL,
        SIGNAL_FORMAT_WECHAT,
        SIGNAL_FORMAT_PODCAST,
        SIGNAL_FORMAT_GITHUB,
        SIGNAL_FORMAT_OTHER,
    }:
        return explicit

    sid = str(source_id or "").strip()
    sid_l = sid.lower()
    url = str(endpoint or "").lower()
    extra = extra or {}

    et = entity_type or extra.get("entity_type")
    if et in _ENTITY_TO_FORMAT:
        return _ENTITY_TO_FORMAT[et]

    if fetch_method == "Bridge" or any(m in sid_l for m in _WECHAT_MARKERS) or "mp.weixin.qq.com" in url:
        return SIGNAL_FORMAT_WECHAT
    if sid_l.startswith("arxiv-") or sid_l in _PAPER_IDS or any(
        h in url for h in ("arxiv.org", "openreview.net", "doi.org", "jmlr.org", "nature.com/", "aclanthology.org")
    ):
        return SIGNAL_FORMAT_PAPER
    if sid_l.startswith("github") or "github.com" in url:
        return SIGNAL_FORMAT_GITHUB
    if sid_l in _VIDEO_IDS or any(h in url for h in ("youtube.com", "youtu.be", "bilibili.com", "vimeo.com")):
        return SIGNAL_FORMAT_VIDEO
    if sid_l in _SOCIAL_IDS or any(
        h in url for h in ("x.com/", "twitter.com/", "weibo.com/", "linkedin.com/", "xiaohongshu.com")
    ):
        return SIGNAL_FORMAT_SOCIAL
    if any(m in sid_l for m in _PODCAST_MARKERS) or any(h in url for h in ("spotify.com", "xiaoyuzhoufm.com")):
        return SIGNAL_FORMAT_PODCAST
    if sid or url:
        return SIGNAL_FORMAT_WEB
    return SIGNAL_FORMAT_OTHER


def is_paper_source(
    *,
    source_id: str = "",
    source_type: str = "",
    entity_type: str | None = None,
    endpoint: str = "",
    extra: Any = None,
) -> bool:
    """判断是否为论文源：显式类型 / 论文配置表 / id·URL 启发式。"""
    if normalize_signal_format(source_type) == SIGNAL_FORMAT_PAPER:
        return True
    if entity_type == "paper" or (extra or {}).get("entity_type") == "paper":
        return True
    return (
        infer_signal_format(
            source_id,
            endpoint=endpoint,
            extra=extra,
            entity_type=entity_type,
            explicit_type=source_type,
        )
        == SIGNAL_FORMAT_PAPER
    )


def _infer_feed_source_type(
    source_id: str,
    dimension: str,
    extra: Any,
    fetch_method: str,
    *,
    explicit_type: str | None = None,
    endpoint: str = "",
) -> str:
    return infer_signal_format(
        source_id,
        endpoint=endpoint,
        extra=extra,
        fetch_method=fetch_method,
        explicit_type=explicit_type,
    )


def _infer_scrape_source_type(
    source_id: str,
    dimension: str,
    extra: Any,
    endpoint: str = "",
    *,
    explicit_type: str | None = None,
) -> str:
    return infer_signal_format(
        source_id,
        endpoint=endpoint,
        extra=extra,
        fetch_method="Scrape",
        explicit_type=explicit_type,
    )


def _is_active(f: dict[str, Any]) -> bool:
    status = cell(f.get("status"))
    if status and status != "active":
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
        # 正文关键词命中密度阈值：标题命中直接过，否则正文需命中 >= 该值。
        # 默认 1（沿用旧行为）；正文导航/推荐位噪音大的源可在 extra_config 设 2+ 降误报。
        "keyword_min_hits": max(1, int((extra or {}).get("keyword_min_hits") or 1)),
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
        explicit = cell(f.get("来源类型")) or cell(f.get("source_type"))
        feed["source_type"] = _infer_feed_source_type(
            feed["_source_id"],
            feed["_dimension"],
            extra,
            fetch_method,
            explicit_type=str(explicit) if explicit else None,
            endpoint=feed["url"],
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
    return _map_scrape_sources(records, include_b_class=False, allow_experimental=False)


def map_scrape_sources_for_diag(
    records: list[dict[str, Any]],
    *,
    include_b_class: bool = True,
    allow_experimental: bool = True,
) -> list[dict[str, Any]]:
    """诊断用：可包含 B 类与 experimental。"""
    return _map_scrape_sources(
        records,
        include_b_class=include_b_class,
        allow_experimental=allow_experimental,
    )


def _map_scrape_sources(
    records: list[dict[str, Any]],
    *,
    include_b_class: bool,
    allow_experimental: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        f = rec.get("fields") or {}
        if cell(f.get("fetch_method")) != "Scrape":
            continue
        b_class = _is_b_class(f)
        if b_class and not include_b_class:
            continue
        status = cell(f.get("status"))
        if status == "paused":
            continue
        if status and status != "active":
            if not (allow_experimental and status == "experimental"):
                continue
        extra = _parse_extra(f)
        feed = _base_feed(f, extra, "Scrape")
        if not feed["url"]:
            continue
        explicit = cell(f.get("来源类型")) or cell(f.get("source_type"))
        feed["source_type"] = _infer_scrape_source_type(
            feed["_source_id"],
            feed["_dimension"],
            extra,
            feed["url"],
            explicit_type=str(explicit) if explicit else None,
        )
        feed["min_content_chars"] = (
            int(cell(f.get("min_content_chars")) or 0)
            or int(feed["_min_from_extra"] or 0)
            or 100
        )
        feed["max_articles"] = int((extra or {}).get("max_articles") or 0) or config.DEFAULT_MAX_ARTICLES
        feed["b_class"] = b_class
        feed["status"] = status or "active"
        out.append(feed)
    return out


def scrape_cohort(source_id: str, category: str = "", url: str = "") -> str:
    """诊断分桶：公司博客 / 实验室 / 论文站 / 榜单 / 招聘 / 其它。"""
    sid = str(source_id or "").lower()
    cat = str(category or "")
    u = str(url or "").lower()
    if sid in _B_SET or any(
        x in sid for x in ("arena", "helm", "swe-bench", "livecodebench", "aider", "opencompass", "scale-seal")
    ):
        return "榜单"
    if "career" in sid or "/careers" in u:
        return "招聘"
    if sid in _PAPER_IDS or any(
        x in sid for x in ("hf-papers", "papers-with-code", "scholar", "openreview", "conference")
    ):
        return "论文站"
    if sid.startswith("lab-") or cat == "技术研究开源" and "lab" in sid:
        return "实验室"
    if cat == "前沿模型公司" or any(
        x in sid for x in ("-blog", "-news", "anthropic", "mistral", "cohere", "meta-ai", "xai-", "bytedance", "qwen")
    ):
        return "公司博客"
    if cat == "技术研究开源":
        return "实验室"
    return "其它"

def catalog_signal_format(name: str, *, endpoint: str = "", fetch_method: str = "") -> str:
    """信号源表（无 source_id）按名称/链接推断来源类型。"""
    n = str(name or "").strip().lower()
    paper_markers = (
        "arxiv",
        "openreview",
        "papers with code",
        "paperswithcode",
        "hugging face papers",
        "huggingface papers",
        "hf papers",
        "semantic scholar",
        "google scholar",
        "conference accepted",
        "jmlr",
        "nature machine",
        "nature computational",
        "alphaxiv",
        "评论区",
    )
    if any(m in n for m in paper_markers):
        return SIGNAL_FORMAT_PAPER
    if any(m in n for m in ("youtube", "b站", "视频")) or fetch_method == "Media" and "播客" not in n:
        if "播客" in n or "podcast" in n:
            return SIGNAL_FORMAT_PODCAST
        if "视频" in n or "youtube" in n or "b站" in n:
            return SIGNAL_FORMAT_VIDEO
    if any(m in n for m in ("公众号", "微信")):
        return SIGNAL_FORMAT_WECHAT
    if any(m in n for m in ("社交", "微博", "twitter", "x账号")):
        return SIGNAL_FORMAT_SOCIAL
    if "播客" in n or "podcast" in n:
        return SIGNAL_FORMAT_PODCAST
    return infer_signal_format(
        "",
        endpoint=endpoint,
        fetch_method=fetch_method,
    )
