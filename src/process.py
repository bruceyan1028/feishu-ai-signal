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

# Anthropic / Meta AI Blog 等只给「Aug 27, 2026」这种纯日期，parse_date_ms 会落成当天
# 00:00 UTC，比真实发布时间最多偏早近 24h。日更流水线按一个采集周期补宽限，
# 抵消这个系统性高估；有时分秒的时间戳不扩窗，避免把真旧文放进来。
DATE_ONLY_LOOKBACK_GRACE_HOURS = 24
_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DATE_ONLY_RE = re.compile(
    rf"^(?:"
    rf"\d{{4}}\s*[-/年.]\s*\d{{1,2}}\s*[-/月.]\s*\d{{1,2}}\s*日?"
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"
    rf")\s*$",
    re.I,
)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def is_date_only_published(raw: Any) -> bool:
    """发布时间是否只有日期、没有时分秒。"""
    text = str(raw or "").strip()
    return bool(text) and bool(_DATE_ONLY_RE.match(text))


def effective_lookback_ms(lookback_hours: int, published_raw: Any = None) -> int:
    """实际生效的时间窗毫秒数；纯日期发布时间额外放宽一个采集周期。"""
    hours = max(1, int(lookback_hours))
    if is_date_only_published(published_raw):
        hours += DATE_ONLY_LOOKBACK_GRACE_HOURS
    return hours * 3600000


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
        return None
    try:
        dt = date_parser.parse(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed = int(dt.timestamp() * 1000)
        # 页面缓存、错误时区或脏元数据可能给出未来日期；不能因此绕过 lookback。
        if parsed > now_ms() + 2 * 86400000:
            return None
        return parsed
    except (ValueError, OverflowError, TypeError):
        return None


def strip_html(text: Any) -> str:
    s = _TAG_RE.sub("", str(text or ""))
    return _WS_RE.sub(" ", s).strip()


def strip_html_body(text: Any) -> str:
    """正文专用：保留段落边界，避免整篇被压成一行导致前端只能渲染字墙。"""
    from . import scrape

    return scrape.html_to_text(str(text or ""))


def build_dedup_key(url: str, title: str, feed: dict[str, Any]) -> str:
    strategy = feed.get("dedup_key") or "normalize(url)"
    if feed.get("fetch_method") == "Podcast" or "podcast_guid" in strategy:
        guid = str(feed.get("podcast_guid") or "").strip()
        if guid:
            return f"podcast:{guid}"[:240]
    if feed.get("fetch_method") == "Social" or "x_post_id" in strategy:
        if feed.get("x_post_id"):
            return f"x:{feed['x_post_id']}"
        match = re.search(r"(?:x|twitter)\.com/[^/]+/status/(\d+)", url, re.I)
        if match:
            return f"x:{match.group(1)}"
    if feed.get("fetch_method") == "Media" or "youtube_video_id" in strategy:
        match = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|embed/))([\w-]{11})", url, re.I)
        if match:
            return f"youtube:{match.group(1)}"
    if "arxiv_id" in strategy:
        arxiv_id = paper_enrich.extract_arxiv_id(url)
        if arxiv_id:
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


def extract_policy_metadata(
    title: str,
    url: str,
    body: str,
    feed: dict[str, Any],
    entry_tags: list[str] | None = None,
) -> dict[str, str]:
    """确定性标注官方政策载体，避免把建议性报告误判为已生效命令。"""
    text = f"{title}\n{body[:2500]}"
    lower = text.lower()
    path = url.lower()
    agency = "White House"
    if "office of science and technology policy" in lower or re.search(r"\bOSTP\b", text):
        agency = "White House OSTP"
    elif "office of management and budget" in lower or re.search(r"\bOMB\b", text):
        agency = "White House / OMB"

    tags = " ".join(entry_tags or []).lower()
    is_presidential_action = "/presidential-actions/" in path
    title_lower = title.lower()
    if is_presidential_action and "presidential determination" in title_lower:
        stage = "总统决定"
    elif is_presidential_action and (
        "presidential memoranda" in tags or "presidential memorandum" in title.lower()
    ):
        stage = "总统备忘录"
    elif is_presidential_action and ("proclamations" in tags or "proclamation" in title.lower()):
        stage = "总统公告"
    elif is_presidential_action and (
        "executive orders" in tags
        or "executive order" in title_lower
        or re.search(r"\bExecutive Order\s+(?:No\.?\s*)?\d+", text, re.I)
    ):
        stage = "行政命令"
    elif "fact sheet" in title_lower:
        stage = "事实清单"
    elif (feed.get("extra_config") or {}).get("document_pdf_enrich") and (
        "report" in title_lower
        or "recommendations" in title_lower
        or "报告" in title
    ):
        stage = "政策报告"
    elif is_presidential_action:
        stage = "总统行动"
    else:
        stage = "官方发布"
    return {"agency": agency, "stage": stage, "authority": "whitehouse.gov"}


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


def _podcast_keyword_ok(keyword_re: re.Pattern[str], title: str, body: str, min_hits: int) -> bool:
    """播客只按标题或节目简介开头判断主题，避免尾部时间戳偶提 AI 就放行整期。"""
    if keyword_re.search(title or ""):
        return True
    intro = (body or "")[:1600]
    return len(keyword_re.findall(intro)) >= max(3, min_hits)


def process_and_clean(
    raw_items: list[dict[str, Any]],
    type_configs: dict[str, dict[str, Any]] | None = None,
    drop_stats: dict[str, int] | None = None,
    funnel_out: Any = None,
) -> list[dict[str, Any]]:
    """对应 Process and Clean：时间窗/关键词过滤 + 本轮去重。

    正文长度不在这里判，见 drop_too_short：它要等回源补全之后才有意义。

    type_configs：source_id -> {entity_type, params}，来自类型化筛选配置表；
    命中的源在通用过滤后再走对应类型的分支过滤。

    drop_stats（可选出参）：source_id -> 「抽到有效内容但因时间窗被过滤」的条数。
    只统计本可通过其余过滤（有标题/链接、正文够长、命中关键词）却因 lookback 被丢的条目。

    funnel_out（可选出参）：health.Funnel，把每一步的淘汰按 source_id 归属。总计
    仍照旧打进日志，所以不传它时行为与改造前一致。
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
    if funnel_out is not None:
        for item in raw_items:
            funnel_out.bump(str((item.get("feed") or {}).get("id") or ""), "raw")

    def drop(feed_id: str, stage: str) -> None:
        """记一次淘汰：总计照旧，同时归属到源。"""
        funnel[stage] += 1
        if funnel_out is not None:
            funnel_out.bump(feed_id, stage)

    for item in raw_items:
        feed = item.get("feed") or {}
        feed_id = feed.get("id") or ""
        feed_key = feed_id or feed.get("url") or "0"
        feed_hits = per_feed.get(feed_key, 0)
        if feed_hits >= config.MAX_ITEMS_PER_FEED:
            drop(feed_id, "per_feed_cap")
            continue

        # lookback_window 配了就按配置；没配才回落到默认（不再强制抬到 168h）
        lookback_hours = int(feed.get("lookback_hours") or config.MIN_LOOKBACK_HOURS)
        keyword_re = _safe_regex(feed.get("keyword_regex"))
        title_exclude_re = _safe_regex(feed.get("title_exclude_regex")) if feed.get(
            "title_exclude_regex"
        ) else None
        kw_min_hits = max(1, int(feed.get("keyword_min_hits") or 1))
        min_chars = feed.get("min_content_chars") or 100
        # Bridge/Social 已按账号白名单筛选；Podcast 仍需主题过滤，避免白名单节目中的非 AI 单集。
        fetch_method = feed.get("fetch_method")
        skip_keyword = fetch_method in {"Bridge", "Social"}

        url = normalize_url(item.get("url"))
        title = strip_html(item.get("title"))
        body_text = strip_html_body(item.get("body"))
        published_raw = item.get("published_raw")
        published_ms = parse_date_ms(published_raw)
        # 纯日期会落成当天 00:00，比真实发布时间偏早；effective_lookback_ms 补一天宽限。
        lookback_ms = effective_lookback_ms(lookback_hours, published_raw)
        duplicate_key = build_dedup_key(url, title, feed)
        combined = f"{title} {body_text}"

        if not title or not url:
            drop(feed_id, "missing_title_url")
            continue
        if title_exclude_re and title_exclude_re.search(title):
            drop(feed_id, "title_exclude_regex")
            continue
        # 精准度优先：缺发布时间不能用采集时间冒充“刚发布”，否则任意旧页面都会进近七日池。
        if published_ms is None:
            drop(feed_id, "missing_or_invalid_date")
            continue
        # heat_keep：超高热度旧文例外，跳过 lookback（仍写入真实发布时间）
        if not item.get("heat_keep") and (
            now - published_ms >= lookback_ms
        ):
            drop(feed_id, "lookback")
            if (
                drop_stats is not None
                and title
                and url
                and len(combined) >= min_chars
                and (
                    skip_keyword
                    or (
                        _podcast_keyword_ok(keyword_re, title, body_text, kw_min_hits)
                        if fetch_method == "Podcast"
                        else _keyword_ok(keyword_re, title, body_text, kw_min_hits)
                    )
                )
            ):
                drop_stats[feed_id] = drop_stats.get(feed_id, 0) + 1
            continue
        # 正文长度不在这里判：摘要型 RSS（OpenAI / DeepMind）feed 里只有一两百字，
        # 要等跨轮去重后回源补全，再由 drop_too_short 按最终正文统一判一次。
        if not skip_keyword:
            keyword_ok = (
                _podcast_keyword_ok(keyword_re, title, body_text, kw_min_hits)
                if fetch_method == "Podcast"
                else _keyword_ok(keyword_re, title, body_text, kw_min_hits)
            )
            if not keyword_ok:
                drop(feed_id, "keyword_regex")
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
            verdict = paper_enrich.evaluate_paper(
                title, body_text, url, (type_cfg or {}).get("params"), metrics
            )
            if not verdict.keep:
                drop(feed_id, verdict.reason)
                continue
            quality_fields = verdict.quality_fields

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
                drop(feed_id, reason or "typed_filter")
                log.debug("类型过滤丢弃 %s（%s: %s）", url, type_cfg["entity_type"], reason)
                continue

        is_social = (
            (type_cfg or {}).get("entity_type") == "social"
            or feed.get("source_type") == sources.SIGNAL_FORMAT_SOCIAL
            or feed.get("fetch_method") == "Social"
        )
        if is_social:
            quality_fields = {
                "quality_score": float(metrics.get("social_score") or 0),
                "social_metrics_json": metrics,
            }

        if duplicate_key in seen:
            drop(feed_id, "dup_round")
            continue
        seen.add(duplicate_key)
        per_feed[feed_key] = feed_hits + 1

        media_assets = item.get("media_assets")
        if not isinstance(media_assets, dict):
            media_assets = {"images": [], "videos": []}
        if (feed.get("extra_config") or {}).get("policy_stage_extract"):
            media_assets["policy"] = extract_policy_metadata(
                title,
                url,
                body_text,
                feed,
                [str(tag) for tag in item.get("entry_tags") or []],
            )

        row = {
            "title": title,
            "url": url,
            "source": feed.get("name") or "Unknown",
            "source_id": feed_id,
            "source_type": feed.get("source_type") or sources.SIGNAL_FORMAT_OTHER,
            "fetch_method": feed.get("fetch_method") or "",
            "category": sources.normalize_category(feed.get("category") or ""),
            "tier": feed.get("tier") or "",
            "published_ms": published_ms,
            "collected_ms": collected_ms,
            "raw_content": body_text[:15000],
            "image_url": str(item.get("image_url") or "").strip(),
            "media_assets": media_assets,
            "topics": infer_topics(title, body_text),
            "duplicate_key": duplicate_key,
            "quality_score": float(quality_fields.get("quality_score") or 0),
            "min_content_chars": min_chars,
            # 采集后处理所需的内部字段；format_for_feishu 不会直接写入。
            "feed": feed,
            "podcast": item.get("podcast") or {},
            "metrics": metrics,
        }
        row.update(quality_fields)
        result.append(row)
        drop(feed_id, "kept")

    log.info(
        "清洗漏斗 raw=%d kept=%d drops=%s",
        funnel["raw"],
        funnel["kept"],
        {k: v for k, v in funnel.most_common() if k not in {"raw", "kept"}},
    )
    return result


def drop_too_short(
    items: list[dict[str, Any]], funnel_out: Any = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """回源补全之后统一判正文长度，返回 (保留, 丢弃)。

    Social 不判：推文在抓取层已按自己的规则筛过，带图或带硬证据的短推文是有意保留的。
    传 funnel_out 时把淘汰记到 min_content_chars，并把清洗阶段已计的 kept 回退。
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in items:
        if item.get("fetch_method") == "Social":
            kept.append(item)
            continue
        combined = f"{item.get('title') or ''} {item.get('raw_content') or ''}"
        if len(combined) < int(item.get("min_content_chars") or 0):
            dropped.append(item)
            if funnel_out is not None:
                sid = str(item.get("source_id") or "")
                funnel_out.bump(sid, "min_content_chars")
                funnel_out.bump(sid, "kept", -1)
            continue
        kept.append(item)
    if dropped:
        log.info("补全后仍过短丢弃 %d 条", len(dropped))
    return kept, dropped


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
    podcast_analysis = item.get("podcast_analysis") or {}
    if podcast_analysis:
        urgency = str(podcast_analysis.get("urgency") or "中")
        fields.update(
            {
                "中文标题": str(podcast_analysis.get("title_cn") or item["title"]),
                "中文摘要": str(podcast_analysis.get("summary_cn") or ""),
                "AI深度解读": str(podcast_analysis.get("deep_analysis_cn") or ""),
                "为何重要": str(podcast_analysis.get("why") or ""),
                "主题": list(podcast_analysis.get("topics") or topics or ["其他"]),
                "影响分": float(podcast_analysis.get("impact") or 0),
                "新颖度": float(podcast_analysis.get("novelty") or 0),
                "可行动性": float(podcast_analysis.get("actionability") or 0),
                "紧迫度": urgency if urgency in {"高", "中", "低"} else "中",
                "状态": "已分析",
            }
        )
    media_assets = item.get("media_assets") or {}
    if (
        media_assets.get("images")
        or media_assets.get("videos")
        or media_assets.get("audio")
        or media_assets.get("documents")
        # 只带外链文章卡的帖子（正文一句话 + 一张官方封面）在这里漏判过：整份
        # media_assets 不写库，封面和跳转链接就都没了
        or media_assets.get("articles")
        or media_assets.get("policy")
    ):
        fields["媒体资源"] = json.dumps(media_assets, ensure_ascii=False)
    image = _to_link(item.get("image_url") or "", "原文配图")
    if image:
        fields["图片链接"] = image
    if item.get("paper_metrics_json") or item.get("social_metrics_json") or (
        item.get("quality_score") is not None
        and (
            item.get("source_type") == sources.SIGNAL_FORMAT_PAPER
            or item.get("source_type") == sources.SIGNAL_FORMAT_SOCIAL
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
        social_metrics = item.get("social_metrics_json")
        if social_metrics:
            fields["社媒指标"] = json.dumps(social_metrics, ensure_ascii=False)
    podcast_metrics = item.get("podcast_metrics_json")
    if podcast_metrics:
        fields["播客指标"] = json.dumps(podcast_metrics, ensure_ascii=False)
        fields["质量分"] = float(item.get("quality_score") or 0)
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
