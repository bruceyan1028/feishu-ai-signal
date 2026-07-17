"""清洗、过滤、去重键、飞书字段组装。

对应 n8n 节点：Process and Clean / Format for Feishu
"""
from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as date_parser

from . import config
from . import paper_enrich
from . import sources
from . import typed_config as tcfg

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


def _keyword_ok(
    keyword_re: re.Pattern[str], title: str, body: str, min_hits: int
) -> bool:
    """标题命中直接通过；否则正文关键词命中次数需达到 min_hits。
    用于压制正文导航/推荐位里蹭到单个关键词导致的假阳性（果壳中暑文等）。"""
    if keyword_re.search(title or ""):
        return True
    if min_hits <= 1:
        return bool(keyword_re.search(body or ""))
    return len(keyword_re.findall(body or "")) >= min_hits


def process_and_clean(
    raw_items: list[dict[str, Any]],
    type_configs: dict[str, dict[str, Any]] | None = None,
    drop_stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """对应 Process and Clean：时间窗/关键词/最小长度过滤 + 本轮去重。

    type_configs：source_id -> {entity_type, params}，来自类型化筛选配置表；
    命中的源在通用过滤后再走对应类型的分支过滤。

    drop_stats（可选出参）：source_id -> 「抽到有效内容但因时间窗被过滤」的条数。
    只统计本可通过其余过滤（有标题/链接、正文够长、命中关键词）却因 lookback 被丢的条目。
    """
    from collections import Counter

    type_configs = type_configs or {}
    now = now_ms()
    collected_ms = now
    seen: set[str] = set()
    per_feed: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    funnel: Counter[str] = Counter()
    funnel["raw"] = len(raw_items)

    for item in raw_items:
        feed = item.get("feed") or {}
        feed_id = feed.get("id") or ""
        feed_key = feed_id or feed.get("url") or "0"
        feed_hits = per_feed.get(feed_key, 0)
        if feed_hits >= config.MAX_ITEMS_PER_FEED:
            funnel["per_feed_cap"] += 1
            continue

        # lookback_window 配了就按配置；没配才回落到默认（不再强制抬到 168h）
        lookback_hours = int(feed.get("lookback_hours") or config.MIN_LOOKBACK_HOURS)
        lookback_ms = lookback_hours * 3600000
        keyword_re = _safe_regex(feed.get("keyword_regex"))
        kw_min_hits = max(1, int(feed.get("keyword_min_hits") or 1))
        min_chars = feed.get("min_content_chars") or 100
        # Bridge 本身已按账号筛选，跳过关键词；论文源一律走 keyword_regex
        skip_keyword = feed.get("fetch_method") == "Bridge"

        url = normalize_url(item.get("url"))
        title = strip_html(item.get("title"))
        body_text = strip_html(item.get("body"))
        published_ms = parse_date_ms(item.get("published_raw"))
        duplicate_key = build_dedup_key(url, title, feed)
        combined = f"{title} {body_text}"

        if not title or not url:
            funnel["missing_title_url"] += 1
            continue
        # heat_keep：超高热度旧文例外，跳过 lookback（仍写入真实发布时间）
        if not item.get("heat_keep") and (
            published_ms is None or now - published_ms >= lookback_ms
        ):
            funnel["lookback"] += 1
            if (
                drop_stats is not None
                and title
                and url
                and len(combined) >= min_chars
                and (skip_keyword or _keyword_ok(keyword_re, title, body_text, kw_min_hits))
            ):
                drop_stats[feed_id] = drop_stats.get(feed_id, 0) + 1
            continue
        if len(combined) < min_chars:
            funnel["min_content_chars"] += 1
            continue
        if not skip_keyword and not _keyword_ok(keyword_re, title, body_text, kw_min_hits):
            funnel["keyword_regex"] += 1
            continue

        metrics = dict(item.get("metrics") or {})
        type_cfg = type_configs.get(feed_id)
        is_paper = sources.is_paper_source(
            source_id=feed_id,
            source_type=str(feed.get("source_type") or ""),
            entity_type=(type_cfg or {}).get("entity_type"),
            endpoint=url,
            extra=feed.get("extra_config"),
        )
        quality_fields: dict[str, Any] = {}
        if is_paper:
            metrics.update(tcfg.infer_paper_metrics(title, body_text, url))
            params = (type_cfg or {}).get("params") or {}
            # 本地信号分先硬过滤，减少外网富集
            min_sig = params.get("min_signal_score")
            if min_sig is not None and metrics.get("signal_score") is not None:
                if float(metrics["signal_score"]) < float(min_sig):
                    funnel["min_signal_score"] += 1
                    continue
            enriched = paper_enrich.enrich_paper(
                url,
                signal_score=float(metrics.get("signal_score") or 50),
                venue_whitelist=params.get("venue_whitelist"),
                venue_blacklist=params.get("venue_blacklist"),
            )
            # 录用后不再视为纯预印本
            if enriched.get("accepted_venue"):
                metrics["is_preprint"] = False
            elif enriched.get("arxiv_id"):
                metrics["is_preprint"] = True
            metrics.update(
                {
                    k: v
                    for k, v in enriched.items()
                    if k
                    in {
                        "accepted_venue",
                        "community_heat",
                        "venue_score",
                        "venue_reason",
                        "quality_score",
                    }
                    and v is not None
                }
            )
            quality_fields = {
                "quality_score": enriched.get("quality_score"),
                "accepted_venue": enriched.get("accepted_venue") or "",
                "community_heat": enriched.get("community_heat"),
                "paper_metrics_json": {
                    "arxiv_id": enriched.get("arxiv_id"),
                    "comment": enriched.get("arxiv_comment"),
                    "journal_ref": enriched.get("journal_ref"),
                    "venue_score": enriched.get("venue_score"),
                    "venue_reason": enriched.get("venue_reason"),
                    "community": {
                        "upvotes": enriched.get("community_upvotes"),
                        "comments": enriched.get("community_comments"),
                        "heat": enriched.get("community_heat"),
                    },
                    "signal_score": metrics.get("signal_score"),
                    "quality_score": enriched.get("quality_score"),
                },
            }

        if type_cfg:
            keep, reason = tcfg.apply_typed_filter(
                type_cfg["entity_type"],
                type_cfg["params"],
                {
                    "text": combined.lower(),
                    "body_len": len(body_text),
                    "metrics": metrics,
                },
            )
            if not keep:
                funnel[reason or "typed_filter"] += 1
                log.debug("类型过滤丢弃 %s（%s: %s）", url, type_cfg["entity_type"], reason)
                continue

        if duplicate_key in seen:
            funnel["dup_round"] += 1
            continue
        seen.add(duplicate_key)
        per_feed[feed_key] = feed_hits + 1

        row = {
            "title": title,
            "url": url,
            "source": feed.get("name") or "Unknown",
            "source_id": feed_id,
            "source_type": feed.get("source_type") or sources.SIGNAL_FORMAT_OTHER,
            "fetch_method": feed.get("fetch_method") or "",
            "category": feed.get("category") or "",
            "tier": feed.get("tier") or "",
            "published_ms": published_ms,
            "collected_ms": collected_ms,
            "raw_content": body_text[:15000],
            "image_url": str(item.get("image_url") or "").strip(),
            "media_assets": item.get("media_assets") or {"images": [], "videos": []},
            "topics": infer_topics(title, body_text),
            "duplicate_key": duplicate_key,
            "quality_score": float(quality_fields.get("quality_score") or 0),
        }
        row.update(quality_fields)
        result.append(row)
        funnel["kept"] += 1

    log.info(
        "清洗漏斗 raw=%d kept=%d drops=%s",
        funnel["raw"],
        funnel["kept"],
        {k: v for k, v in funnel.most_common() if k not in {"raw", "kept"}},
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
    fields = {
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
    media_assets = item.get("media_assets") or {}
    if media_assets.get("images") or media_assets.get("videos"):
        fields["媒体资源"] = json.dumps(media_assets, ensure_ascii=False)
    image = _to_link(item.get("image_url") or "", "原文配图")
    if image:
        fields["图片链接"] = image
    if item.get("paper_metrics_json") or (
        item.get("quality_score") is not None
        and (
            item.get("source_type") == sources.SIGNAL_FORMAT_PAPER
            or str(item.get("source_id") or "").startswith("arxiv-")
        )
    ):
        fields["质量分"] = float(item.get("quality_score") or 0)
        if item.get("accepted_venue"):
            fields["录用会议"] = str(item.get("accepted_venue") or "")
        if item.get("community_heat") is not None:
            fields["社区热度"] = float(item.get("community_heat") or 0)
        metrics_json = item.get("paper_metrics_json")
        if metrics_json:
            fields["论文指标"] = json.dumps(metrics_json, ensure_ascii=False)
    return fields


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
