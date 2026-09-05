"""首页话题热力图：Google 与 X 各自从本平台热搜筛 AI 词，再补近 7 日热度。

Google 行来自多地区 `trending_now`，X 行来自官方 Trends by WOEID；两边话题集
完全独立。Google 每词拉 0–100 兴趣曲线，X 每词用 recent counts 数近 7 日帖子。
任一平台失败时，只在该平台话题不变时沿用上一份快照的重叠日期。

    python -m src.trends --output site/data/heatmap-trends.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

log = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "site" / "data" / "heatmap-trends.json"
RAW_DIR = ROOT / "data" / "trends"

WINDOW_DAYS = 7
GOOGLE_TZ = -480
GOOGLE_RANGE_DAYS = 8
RELATED_MIN_PEAK = 1.0
RETRY_SLEEP = 2.0
TOPIC_SLEEP = 0.8
GOOGLE_REQUEST_DELAY = 1.5
MAX_TOPICS = 10
MIN_ROWS = 4
RATIO_MIN = 1.5
RATIO_RELAXED = 1.3
RATIO_LAYERS: tuple[dict[str, Any], ...] = (
    {"skip_yesterday": False, "threshold": RATIO_MIN},
    {"skip_yesterday": True, "threshold": RATIO_MIN},
    {"skip_yesterday": False, "threshold": RATIO_RELAXED},
    {"skip_yesterday": True, "threshold": RATIO_RELAXED},
)
TREND_HOURS = 48
TREND_GEOS = ("US", "GB", "IN", "DE", "JP", "AU", "CA")

# 只认明确的 AI 产品/概念，不把整个 Technology 类（iPhone、PS）算进来。
AI_RE = re.compile(
    r"(?i)(?<![a-z])("
    r"ai|a\.i\.|artificial intelligence|generative ai|physical ai|"
    r"chatgpt|gpt-?\d+|claude code|claude ai|claude opus|claude sonnet|"
    r"gemini|grok|"
    r"openai|anthropic|deepseek|copilot|perplexity|midjourney|sora|"
    r"llm|large language|llama|qwen|mistral|"
    r"ai agent|agent ai|agentic|claude code|"
    r"cursor|dlss|nvidia|"
    r"人工智能|大模型|智能体|フィジカルai"
    r")(?![a-z])"
)
DENY_RE = re.compile(
    r"(?i)iphone|ipad|android|playstation|xbox|nintendo|pokemon|"
    r"honor robot phone|premier league|cricket|football|soccer"
)

# 单测 / 演示回退用的旧赛道，正式采集不再走这里。
TOPICS: tuple[str, ...] = (
    "agent",
    "reasoning",
    "multimodal",
    "open-source-model",
    "rag-search",
    "infra",
    "embodied",
    "safety-policy",
    "product",
    "funding",
)
TOPIC_LABELS = {
    "agent": "智能体",
    "reasoning": "推理",
    "multimodal": "多模态",
    "open-source-model": "开源模型",
    "rag-search": "RAG / 检索",
    "infra": "算力基建",
    "embodied": "具身智能",
    "safety-policy": "安全监管",
    "product": "应用产品",
    "funding": "融资并购",
}
QUERIES: dict[str, dict[str, str]] = {
    "agent": {"g": "AI agent", "x": '("AI agent" OR MCP OR #AIAgents) -is:retweet'},
    "reasoning": {"g": "reasoning model", "x": '("test-time compute" OR #Reasoning) -is:retweet'},
    "multimodal": {
        "g": "image generation",
        "x": '("image generation" OR "video generation" OR #Multimodal) -is:retweet',
    },
    "open-source-model": {"g": "open source LLM", "x": '("open source LLM" OR #OpenSourceAI) -is:retweet'},
    "rag-search": {"g": "RAG", "x": '(RAG OR "retrieval augmented" OR #RAG) -is:retweet'},
    "infra": {"g": "AI chips", "x": '("AI chips" OR inference OR #AIInfra) -is:retweet'},
    "embodied": {"g": "humanoid robot", "x": '("humanoid robot" OR #EmbodiedAI) -is:retweet'},
    "safety-policy": {"g": "AI regulation", "x": '("AI regulation" OR #AISafety) -is:retweet'},
    "product": {"g": "ChatGPT", "x": '(ChatGPT OR Copilot OR #AIProduct) -is:retweet'},
    "funding": {"g": "AI startup", "x": '("AI startup" OR "AI funding" OR #AIFunding) -is:retweet'},
}

BatchFn = Callable[[list[str]], tuple[list[str], dict[str, list[float]], dict[str, list[str]]]]
CountsFn = Callable[[str, str], dict[str, Any]]
TrendingFn = Callable[[str], Iterable[Any]]
XTrendingFn = Callable[[int], dict[str, Any]]

X_TREND_PLACES: tuple[tuple[str, int], ...] = (
    ("", 1),  # Worldwide
    ("US", 23424977),
    ("GB", 23424975),
    ("IN", 23424848),
    ("DE", 23424829),
    ("JP", 23424856),
    ("AU", 23424748),
    ("CA", 23424775),
)


@dataclass
class TopicSpec:
    id: str
    label: str
    query_g: str
    query_x: str
    volume: int = 0
    geos: tuple[str, ...] = ()
    scope: str = "global"
    geo: str = ""
    mark: str = "🌐"
    breakout: bool = False

    def as_query(self) -> dict[str, str]:
        return {"g": self.query_g, "x": self.query_x}


def editorial_specs() -> list[TopicSpec]:
    return [
        TopicSpec(id=key, label=TOPIC_LABELS[key], query_g=spec["g"], query_x=spec["x"])
        for key, spec in QUERIES.items()
    ]


def day_labels(today: date | None = None) -> list[str]:
    today = today or datetime.now(CN_TZ).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(WINDOW_DAYS - 1, -1, -1)]


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def normalize_rows(raw: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for row in raw:
        peak = max(row) if row else 0.0
        out.append([round(value / peak, 4) if peak else 0.0 for value in row])
    return out


def row_ratio(row: list[float], *, skip_yesterday: bool = False) -> float:
    last = row[-1] if row else 0.0
    prior = row[:-2] if skip_yesterday and len(row) >= 3 else row[:-1]
    prior_mean = sum(prior) / len(prior) if prior else 0.0
    if prior_mean:
        return round(last / prior_mean, 3)
    return 2.0 if last > 0 else 1.0


def passes_ratio_layer(row: list[float], *, skip_yesterday: bool, threshold: float) -> bool:
    return max(row or [0.0]) > 0 and row_ratio(row, skip_yesterday=skip_yesterday) >= threshold


def passes_any_layer(row: list[float], layers: tuple[dict[str, Any], ...] = RATIO_LAYERS) -> bool:
    return any(
        passes_ratio_layer(row, skip_yesterday=bool(layer["skip_yesterday"]), threshold=float(layer["threshold"]))
        for layer in layers
    )


def trend_ratios(raw: list[list[float]], topics: list[str]) -> dict[str, float]:
    return {topic: row_ratio(row) for topic, row in zip(topics, raw)}


def flag_emoji(cc: str) -> str:
    letters = (cc or "").strip().upper()
    if len(letters) != 2 or not letters.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in letters)


def scoped_spec(spec: TopicSpec, *, scope: str, geo: str = "", breakout: bool = False) -> TopicSpec:
    mark = "🌐" if scope == "global" else flag_emoji(geo)
    return TopicSpec(
        id=spec.id,
        label=spec.label,
        query_g=spec.query_g,
        query_x=spec.query_x,
        volume=spec.volume,
        geos=spec.geos,
        scope=scope,
        geo=geo if scope == "country" else "",
        mark=mark,
        breakout=breakout,
    )


def _layer_hits(
    candidates: list[TopicSpec],
    series_for: Callable[[TopicSpec], tuple[list[float], str, str] | None],
    *,
    used: set[str],
    skip_yesterday: bool,
    threshold: float,
) -> list[tuple[float, TopicSpec, list[float]]]:
    hits: list[tuple[float, TopicSpec, list[float]]] = []
    for spec in candidates:
        if spec.id in used:
            continue
        found = series_for(spec)
        if not found:
            continue
        row, scope, geo = found
        row = list(row or [])
        if not passes_ratio_layer(row, skip_yesterday=skip_yesterday, threshold=threshold):
            continue
        hits.append(
            (
                row_ratio(row, skip_yesterday=skip_yesterday),
                scoped_spec(spec, scope=scope, geo=geo, breakout=True),
                row,
            )
        )
    hits.sort(key=lambda item: (-item[0], -item[1].volume))
    return hits


def pick_scoped_rows(
    candidates: list[TopicSpec],
    global_series: dict[str, list[float]],
    country_series: dict[str, tuple[str, list[float]]] | None = None,
    *,
    layers: tuple[dict[str, Any], ...] = RATIO_LAYERS,
    min_rows: int = MIN_ROWS,
    max_rows: int = MAX_TOPICS,
    fill_hot: bool = True,
) -> list[tuple[TopicSpec, list[float]]]:
    picked: list[tuple[TopicSpec, list[float]]] = []
    used: set[str] = set()

    def take(hits: list[tuple[float, TopicSpec, list[float]]]) -> None:
        for _ratio, spec, row in hits:
            if spec.id in used or len(picked) >= max_rows:
                return
            picked.append((spec, row))
            used.add(spec.id)

    def global_of(spec: TopicSpec) -> tuple[list[float], str, str] | None:
        row = global_series.get(spec.id)
        if row is None:
            return None
        return list(row), "global", ""

    def country_of(spec: TopicSpec) -> tuple[list[float], str, str] | None:
        if not country_series or spec.id not in country_series:
            return None
        geo, row = country_series[spec.id]
        return list(row), "country", geo

    for layer in layers:
        take(
            _layer_hits(
                candidates,
                global_of,
                used=used,
                skip_yesterday=bool(layer["skip_yesterday"]),
                threshold=float(layer["threshold"]),
            )
        )
        if len(picked) >= min_rows:
            return picked[:max_rows]
    if country_series:
        for layer in layers:
            take(
                _layer_hits(
                    candidates,
                    country_of,
                    used=used,
                    skip_yesterday=bool(layer["skip_yesterday"]),
                    threshold=float(layer["threshold"]),
                )
            )
            if len(picked) >= min_rows:
                return picked[:max_rows]
    if len(picked) >= min_rows or not fill_hot:
        return picked[:max_rows]
    leftovers = [spec for spec in candidates if spec.id not in used]
    leftovers.sort(key=lambda spec: (-spec.volume, spec.label.lower()))
    for spec in leftovers:
        if len(picked) >= max_rows:
            break
        row = global_series.get(spec.id)
        if row is None:
            continue
        picked.append((scoped_spec(spec, scope="global", breakout=False), list(row)))
        used.add(spec.id)
    if country_series and len(picked) < min_rows:
        for spec in leftovers:
            if spec.id in used or len(picked) >= max_rows:
                continue
            found = country_series.get(spec.id)
            if not found:
                continue
            geo, row = found
            picked.append((scoped_spec(spec, scope="country", geo=geo, breakout=False), list(row)))
            used.add(spec.id)
    return picked[:max_rows]


def empty_source(days: list[str], *, topics: list[str] | None = None, error: str = "") -> dict[str, Any]:
    ids = list(topics if topics is not None else TOPICS)
    cols = len(days) or WINDOW_DAYS
    raw = [[0.0 for _ in range(cols)] for _ in ids]
    return {
        "error": error,
        "topics": ids,
        "matrix": {"raw": raw, "normalized": normalize_rows(raw)},
        "trend": {topic: 1.0 for topic in ids},
        "items": {},
        "itemIndex": {},
    }


def _align_series(dates: list[str], values: list[float], days: list[str]) -> list[float]:
    lookup = {day: float(value) for day, value in zip(dates, values)}
    return [lookup.get(day, 0.0) for day in days]


def _google_explore_url(query: str, geo: str = "") -> str:
    extra = f"&geo={quote(geo)}" if geo else ""
    return f"https://trends.google.com/trends/explore?date=now%207-d&q={quote(query)}{extra}"


def _x_search_url(query: str) -> str:
    return f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"


def _x_query(keyword: str) -> str:
    return f'"{keyword}" -is:retweet'


def _source_links(
    days: list[str],
    specs: list[TopicSpec],
    *,
    kind: str,
    related: dict[str, list[str]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    items: dict[str, list[str]] = {}
    index: dict[str, dict[str, str]] = {}
    related = related or {}
    for spec in specs:
        is_google = kind == "g"
        ident = f"{kind}-{spec.id}"
        query = spec.query_g if is_google else spec.query_x
        index[ident] = {
            "title": f"{'Google Trends' if is_google else 'X 热搜'} · {query}",
            "source": "Google Trends" if is_google else "X",
            "url": _google_explore_url(spec.query_g, spec.geo) if is_google else _x_search_url(spec.query_x),
        }
        extras: list[str] = []
        if is_google:
            for rank, phrase in enumerate(related.get(spec.id, [])[:5], start=1):
                extra_id = f"{ident}-q{rank}"
                extras.append(extra_id)
                index[extra_id] = {
                    "title": phrase,
                    "source": "Google Trends",
                    "url": _google_explore_url(phrase, spec.geo),
                }
        last = days[-1] if days else ""
        for day in days:
            key = f"{spec.id}|{day}"
            ids = [ident]
            if day == last:
                ids.extend(extras)
            items[key] = ids
    return items, index


def _finish_source(
    days: list[str],
    raw: list[list[float]],
    specs: list[TopicSpec],
    *,
    kind: str,
    error: str = "",
    related: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    topics = [spec.id for spec in specs]
    items, index = _source_links(days, specs, kind=kind, related=related)
    return {
        "error": error,
        "topics": topics,
        "labels": {spec.id: spec.label for spec in specs},
        "queries": {spec.id: spec.as_query() for spec in specs},
        "scopes": {spec.id: spec.scope for spec in specs},
        "marks": {spec.id: spec.mark for spec in specs},
        "breakouts": [spec.id for spec in specs if spec.breakout],
        "selection": {
            "volumes": {spec.id: spec.volume for spec in specs},
            "geos": {spec.id: list(spec.geos) for spec in specs},
            "plot_geo": {spec.id: spec.geo for spec in specs},
        },
        "matrix": {"raw": raw, "normalized": normalize_rows(raw)},
        "trend": trend_ratios(raw, topics),
        "items": items,
        "itemIndex": index,
    }


def write_raw(kind: str, payload: Any) -> None:
    try:
        path = RAW_DIR / kind / f"{datetime.now(CN_TZ).date().isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("写 %s 原始响应失败：%s", kind, exc)


def google_timeframe(today: date | None = None) -> str:
    today = today or datetime.now(CN_TZ).date()
    start = today - timedelta(days=GOOGLE_RANGE_DAYS)
    return f"{start.isoformat()} {today.isoformat()}"


def _dates_from_index(index: Any) -> list[str]:
    dates: list[str] = []
    for idx in index:
        if hasattr(idx, "date"):
            dates.append(idx.date().isoformat())
        else:
            dates.append(str(idx)[:10])
    return dates


def _rising_phrases(blob: Any) -> list[str]:
    if not isinstance(blob, dict):
        return []
    rising = blob.get("rising")
    if rising is None or getattr(rising, "empty", True):
        return []
    column = "query" if "query" in rising.columns else rising.columns[0]
    return [str(value) for value in rising[column].tolist() if str(value).strip()][:5]


def _series_from_frame(frame: Any, keywords: list[str]) -> tuple[list[str], dict[str, list[float]]]:
    if frame is None or getattr(frame, "empty", True):
        raise RuntimeError("Google Trends 未返回 interest_over_time")
    if "isPartial" in getattr(frame, "columns", []):
        frame = frame.drop(columns=["isPartial"])
    dates = _dates_from_index(frame.index)
    series: dict[str, list[float]] = {}
    for keyword in keywords:
        if keyword in frame.columns:
            series[keyword] = [float(value) for value in frame[keyword].fillna(0).tolist()]
        else:
            series[keyword] = [0.0] * len(dates)
    return dates, series


def _canonical(keyword: str) -> str:
    return re.sub(r"[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]+", "", keyword.lower())


def is_latin_keyword(keyword: str) -> bool:
    text = (keyword or "").strip()
    return bool(text and re.fullmatch(r"[A-Za-z][A-Za-z0-9 +./\-']*", text))


def is_ai_trend(keyword: str, related: Iterable[str] | None = None) -> bool:
    if not is_latin_keyword(keyword):
        return False
    blob = " ".join([keyword, *list(related or [])]).strip()
    if not blob or DENY_RE.search(blob):
        return False
    if re.fullmatch(r"(?i)claude", keyword.strip()):
        return True
    return bool(AI_RE.search(blob))


def _too_similar(left: str, right: str) -> bool:
    a, b = _canonical(left), _canonical(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _norm_keyword(keyword: str) -> str:
    return re.sub(r"\s+", " ", keyword).strip().lower()


def _topic_id(keyword: str, used: set[str]) -> str:
    base = re.sub(r"[^\w]+", "-", keyword.lower(), flags=re.UNICODE)
    base = re.sub(r"^-+|-+$", "", base)[:40] or "topic"
    ident = base
    suffix = 2
    while ident in used:
        ident = f"{base}-{suffix}"
        suffix += 1
    used.add(ident)
    return ident


def _item_field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def merge_trending_hits(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        keyword = str(row.get("keyword") or "").strip()
        if not keyword:
            continue
        key = _norm_keyword(keyword)
        current = merged.get(key)
        geos = list(row.get("geos") or [])
        related = [str(item) for item in (row.get("related") or []) if str(item).strip()]
        volume = int(row.get("volume") or 0)
        if current is None:
            merged[key] = {
                "keyword": keyword,
                "volume": volume,
                "geos": geos,
                "related": related,
            }
            continue
        if volume > int(current["volume"] or 0):
            current["keyword"] = keyword
            current["volume"] = volume
        current["geos"] = list(dict.fromkeys([*current["geos"], *geos]))
        current["related"] = list(dict.fromkeys([*current["related"], *related]))
    return sorted(merged.values(), key=lambda item: (-int(item["volume"] or 0), item["keyword"]))


def select_ai_topics(hits: Iterable[dict[str, Any]], *, limit: int = MAX_TOPICS) -> list[TopicSpec]:
    picked: list[TopicSpec] = []
    used: set[str] = set()
    for hit in merge_trending_hits(hits):
        keyword = str(hit.get("keyword") or "").strip()
        related = [str(item) for item in (hit.get("related") or []) if str(item).strip()]
        if not is_ai_trend(keyword, related):
            continue
        if any(_too_similar(keyword, spec.label) for spec in picked):
            continue
        ident = _topic_id(keyword, used)
        picked.append(
            TopicSpec(
                id=ident,
                label=keyword,
                query_g=keyword,
                query_x=_x_query(keyword),
                volume=int(hit.get("volume") or 0),
                geos=tuple(hit.get("geos") or ()),
            )
        )
        if len(picked) >= limit:
            break
    return picked


def _default_trending(geo: str, hours: int) -> list[Any]:
    from trendspy import Trends

    client = Trends(language="en", tzs=GOOGLE_TZ, request_delay=GOOGLE_REQUEST_DELAY, max_retries=2)
    return list(client.trending_now(geo=geo, hours=hours) or [])


def fetch_trending_hits(
    *,
    geos: tuple[str, ...] = TREND_GEOS,
    hours: int = TREND_HOURS,
    trending_fn: TrendingFn | None = None,
) -> list[dict[str, Any]]:
    worker = trending_fn or (lambda geo: _default_trending(geo, hours))
    rows: list[dict[str, Any]] = []
    for geo in geos:
        try:
            items = list(worker(geo) or [])
        except Exception as exc:  # noqa: BLE001 - 单地区失败继续
            log.warning("Google 热搜 %s 失败：%s", geo, exc)
            continue
        for item in items:
            keyword = str(_item_field(item, "keyword") or "").strip()
            if not keyword:
                continue
            related = _item_field(item, "trend_keywords") or []
            rows.append(
                {
                    "keyword": keyword,
                    "volume": int(_item_field(item, "volume") or 0),
                    "geos": [geo],
                    "related": [str(item) for item in related if str(item).strip()],
                }
            )
    merged = merge_trending_hits(rows)
    write_raw("trending", {"geos": list(geos), "hits": merged})
    return merged


def select_trending_ai(
    *,
    geos: tuple[str, ...] = TREND_GEOS,
    hours: int = TREND_HOURS,
    trending_fn: TrendingFn | None = None,
    limit: int = MAX_TOPICS,
) -> list[TopicSpec]:
    hits = fetch_trending_hits(geos=geos, hours=hours, trending_fn=trending_fn)
    picked = select_ai_topics(hits, limit=limit)
    log.info("热搜筛出 %d 个 AI 话题：%s", len(picked), ", ".join(spec.label for spec in picked) or "无")
    return picked


def _x_trends_get(woeid: int, *, bearer: str) -> dict[str, Any]:
    from . import social

    return social._api_get(
        f"trends/by/woeid/{woeid}",
        bearer=bearer,
        params={"max_trends": 50, "trend.fields": "trend_name,tweet_count"},
    )


def fetch_x_trending_hits(
    *,
    bearer: str | None = None,
    places: tuple[tuple[str, int], ...] = X_TREND_PLACES,
    api_get: XTrendingFn | None = None,
) -> list[dict[str, Any]]:
    token = (os.environ.get("X_BEARER_TOKEN") or "").strip() if bearer is None else bearer.strip()
    if not token:
        raise RuntimeError("未配置 X_BEARER_TOKEN")
    getter = api_get or (lambda woeid: _x_trends_get(woeid, bearer=token))
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    succeeded = 0
    for geo, woeid in places:
        try:
            payload = getter(woeid)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 - 单地区失败继续
            errors.append(f"{geo or 'WORLD'}: {exc}")
            log.warning("X 热搜 %s 失败：%s", geo or "WORLD", exc)
            continue
        trends = list(payload.get("data") or [])
        for rank, item in enumerate(trends):
            keyword = str(item.get("trend_name") or "").strip()
            if not keyword:
                continue
            count = item.get("tweet_count")
            # 官方响应经常不给 tweet_count；用榜内倒序分保留趋势排名，后续热度矩阵
            # 仍由 tweets/counts/recent 的真实帖子数生成。
            rank_score = max(1, len(trends) - rank)
            rows.append(
                {
                    "keyword": keyword,
                    "volume": int(count) if count is not None else rank_score,
                    "geos": [geo or "WORLD"],
                    "related": [],
                }
            )
    if not succeeded:
        raise RuntimeError(errors[0] if errors else "X Trends 未返回数据")
    merged = merge_trending_hits(rows)
    write_raw("x-trending", {"places": dict(places), "hits": merged, "errors": errors})
    return merged


def select_x_trending_ai(
    *,
    bearer: str | None = None,
    places: tuple[tuple[str, int], ...] = X_TREND_PLACES,
    api_get: XTrendingFn | None = None,
    limit: int = MAX_TOPICS,
) -> list[TopicSpec]:
    hits = fetch_x_trending_hits(bearer=bearer, places=places, api_get=api_get)
    picked = []
    for spec in select_ai_topics(hits, limit=limit):
        if "WORLD" in spec.geos or len(spec.geos) != 1:
            picked.append(scoped_spec(spec, scope="global"))
        else:
            picked.append(scoped_spec(spec, scope="country", geo=spec.geos[0]))
    log.info("X 热搜筛出 %d 个 AI 话题：%s", len(picked), ", ".join(spec.label for spec in picked) or "无")
    return picked


def specs_from_payload(payload: dict[str, Any] | None) -> list[TopicSpec]:
    if not payload:
        return []
    topics = list(payload.get("topics") or [])
    labels = payload.get("labels") or {}
    queries = payload.get("queries") or {}
    scopes = payload.get("scopes") or {}
    marks = payload.get("marks") or {}
    volumes = ((payload.get("selection") or {}).get("volumes") or {})
    plot_geo = ((payload.get("selection") or {}).get("plot_geo") or {})
    listing = ((payload.get("selection") or {}).get("geos") or {})
    breakouts = set((payload.get("selection") or {}).get("breakouts") or payload.get("breakouts") or [])
    out: list[TopicSpec] = []
    for topic in topics:
        query = queries.get(topic) or {}
        label = str(labels.get(topic) or topic)
        scope = str(scopes.get(topic) or "global")
        geo = str(plot_geo.get(topic) or "")
        geos = tuple(listing.get(topic) or ((geo,) if geo else ()))
        out.append(
            TopicSpec(
                id=str(topic),
                label=label,
                query_g=str(query.get("g") or label),
                query_x=str(query.get("x") or _x_query(label)),
                volume=int(volumes.get(topic) or 0),
                geos=geos,
                scope=scope,
                geo=geo,
                mark=str(marks.get(topic) or ("🌐" if scope == "global" else flag_emoji(geo))),
                breakout=str(topic) in breakouts,
            )
        )
    return out


def specs_from_source(block: dict[str, Any] | None) -> list[TopicSpec]:
    """恢复单个平台自己的话题；不回退顶层，避免把旧 Google 行误当成 X 热搜。"""
    if not block or not block.get("topics"):
        return []
    payload = {
        "topics": block.get("topics"),
        "labels": block.get("labels"),
        "queries": block.get("queries"),
        "scopes": block.get("scopes"),
        "marks": block.get("marks"),
        "breakouts": block.get("breakouts"),
        "selection": block.get("selection"),
    }
    return specs_from_payload(payload)


def _make_google_batch() -> BatchFn:
    from trendspy import Trends

    client = Trends(language="en", tzs=GOOGLE_TZ, request_delay=GOOGLE_REQUEST_DELAY, max_retries=3)
    timeframe = google_timeframe()
    related_ok = True

    def batch(
        keywords: list[str], geo: str = ""
    ) -> tuple[list[str], dict[str, list[float]], dict[str, list[str]]]:
        nonlocal related_ok
        frame = client.interest_over_time(list(keywords), timeframe=timeframe, geo=geo or "")
        dates, series = _series_from_frame(frame, keywords)
        related: dict[str, list[str]] = {}
        if not related_ok:
            return dates, series, related
        for keyword in keywords:
            peak = max(series.get(keyword) or [0.0])
            if peak <= RELATED_MIN_PEAK:
                continue
            try:
                blob = client.related_queries(keyword, timeframe=timeframe, geo=geo or "")
            except Exception as exc:  # noqa: BLE001 - 配图链接失败不影响指数
                log.warning("Google Trends related_queries 失败：%s", exc)
                related_ok = False
                break
            phrases = _rising_phrases(blob)
            if phrases:
                related[keyword] = phrases
        return dates, series, related

    return batch


def _invoke_batch(
    batch_fn: BatchFn, keywords: list[str], geo: str = ""
) -> tuple[list[str], dict[str, list[float]], dict[str, list[str]]]:
    try:
        return batch_fn(keywords, geo)  # type: ignore[misc]
    except TypeError:
        return batch_fn(keywords)


def _call_batch(
    batch_fn: BatchFn,
    keywords: list[str],
    *,
    geo: str = "",
    retries: int,
    sleep_fn: Callable[[float], Any],
) -> tuple[list[str], dict[str, list[float]], dict[str, list[str]]]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return _invoke_batch(batch_fn, keywords, geo)
        except Exception as exc:  # noqa: BLE001 - 重试后再写成 error
            last_exc = exc
            log.warning("Google Trends 第 %d/%d 次失败：%s", attempt + 1, retries, exc)
            if attempt + 1 < retries:
                sleep_fn(RETRY_SLEEP * (attempt + 1))
    raise RuntimeError(str(last_exc) if last_exc else "Google Trends 失败") from last_exc


def _one_series(
    worker: BatchFn,
    spec: TopicSpec,
    days: list[str],
    *,
    geo: str,
    retries: int,
    sleep_fn: Callable[[float], Any],
) -> tuple[list[float], dict[str, list[str]], dict[str, Any]]:
    dates, series, related = _call_batch(
        worker, [spec.query_g], geo=geo, retries=retries, sleep_fn=sleep_fn
    )
    aligned = [round(value, 2) for value in _align_series(dates, series.get(spec.query_g) or [], days)]
    related_map: dict[str, list[str]] = {}
    if max(aligned or [0.0]) > RELATED_MIN_PEAK:
        phrases = related.get(spec.query_g) or related.get(spec.id) or []
        if phrases:
            related_map[spec.id] = phrases
    dump = {"keywords": [spec.query_g], "geo": geo, "dates": dates, "series": series, "related": related}
    return aligned, related_map, dump


def fetch_google(
    days: list[str],
    *,
    topics: list[TopicSpec] | None = None,
    batch_fn: BatchFn | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
    retries: int = 3,
    breakout: bool = False,
) -> dict[str, Any]:
    specs = list(topics) if topics is not None else editorial_specs()
    if not specs:
        return empty_source(days, topics=[], error="热搜里今天没有筛出 AI 话题")
    worker = batch_fn if batch_fn is not None else _make_google_batch()
    raw_dump: list[dict[str, Any]] = []
    related_by_topic: dict[str, list[str]] = {}
    chosen: list[TopicSpec] = list(specs)
    rows: list[list[float]] = []
    try:
        global_series: dict[str, list[float]] = {}
        for index, spec in enumerate(specs):
            aligned, related, dump = _one_series(
                worker, spec, days, geo="", retries=retries, sleep_fn=sleep_fn
            )
            raw_dump.append(dump)
            global_series[spec.id] = aligned
            related_by_topic.update(related)
            if index + 1 < len(specs):
                sleep_fn(TOPIC_SLEEP)
        if breakout:
            picked = pick_scoped_rows(specs, global_series, fill_hot=False)
            leftovers = [spec for spec in specs if spec.id not in {item[0].id for item in picked}]
            country_series: dict[str, tuple[str, list[float]]] = {}
            if len(picked) < MIN_ROWS:
                for spec in leftovers:
                    for geo in spec.geos:
                        aligned, related, dump = _one_series(
                            worker, spec, days, geo=geo, retries=retries, sleep_fn=sleep_fn
                        )
                        raw_dump.append(dump)
                        related_by_topic.update(related)
                        if passes_any_layer(aligned):
                            country_series[spec.id] = (geo, aligned)
                            break
                        sleep_fn(TOPIC_SLEEP)
            picked = pick_scoped_rows(specs, global_series, country_series)
            if not picked:
                empty = empty_source(days, topics=[], error="近 7 日没有足够的破线话题")
                empty["_chosen"] = []
                return empty
            chosen = [spec for spec, _row in picked]
            rows = [row for _spec, row in picked]
        else:
            rows = [global_series.get(spec.id, [0.0] * len(days)) for spec in chosen]
    except Exception as exc:  # noqa: BLE001 - 与看板一样，失败写成 error
        log.warning("Google Trends 获取失败：%s", exc)
        return empty_source(days, topics=[spec.id for spec in specs], error=str(exc))

    write_raw("google", raw_dump)
    log.info(
        "Google Trends %d 话题 × %d 日（%s）",
        len(chosen),
        len(days),
        "破线" if breakout else "全量",
    )
    block = _finish_source(days, rows, chosen, kind="g", related=related_by_topic)
    block["_chosen"] = chosen
    return block


def _x_counts_get(query: str, start_time: str, *, bearer: str) -> dict[str, Any]:
    from . import social

    try:
        return social._api_get(
            "tweets/counts/recent",
            bearer=bearer,
            params={"query": query, "granularity": "day", "start_time": start_time},
        )
    except RuntimeError as exc:
        message = str(exc)
        if "403" in message:
            raise RuntimeError(
                "X API 返回 403：当前 App 无权访问 tweets/counts/recent"
                "（和白名单时间线权限不是一回事）"
            ) from exc
        raise


def _bucket_counts(payload: dict[str, Any], days: list[str]) -> list[float]:
    lookup = {day: 0.0 for day in days}
    for row in payload.get("data") or []:
        start = str(row.get("start") or "")
        if not start:
            continue
        try:
            instant = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        day = instant.astimezone(CN_TZ).date().isoformat()
        if day in lookup:
            lookup[day] += float(row.get("tweet_count") or 0)
    return [lookup[day] for day in days]


def fetch_x(
    days: list[str],
    *,
    topics: list[TopicSpec] | None = None,
    bearer: str | None = None,
    api_get: CountsFn | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    specs = list(topics) if topics is not None else editorial_specs()
    token = (os.environ.get("X_BEARER_TOKEN") or "").strip() if bearer is None else bearer.strip()
    if not token:
        return empty_source(days, topics=[spec.id for spec in specs], error="未配置 X_BEARER_TOKEN")
    if not specs:
        return empty_source(days, topics=[], error="热搜里今天没有筛出 AI 话题")

    first = date.fromisoformat(days[0])
    start_time = datetime(first.year, first.month, first.day, tzinfo=CN_TZ).astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    earliest = now_utc - timedelta(days=6, hours=23)
    if start_time < earliest:
        start_time = earliest
    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    getter: CountsFn
    if api_get is not None:
        getter = api_get
    else:
        getter = lambda query, start: _x_counts_get(query, start, bearer=token)

    raw_rows: list[list[float]] = []
    errors: list[str] = []
    dump: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        try:
            payload = getter(spec.query_x, start_iso)
            dump.append({"topic": spec.id, "query": spec.query_x, "payload": payload})
            raw_rows.append(_bucket_counts(payload, days))
        except Exception as exc:  # noqa: BLE001 - 单话题失败记下来，不全盘放弃
            log.warning("X counts %s 失败：%s", spec.id, exc)
            errors.append(f"{spec.id}: {exc}")
            raw_rows.append([0.0] * len(days))
        if index + 1 < len(specs):
            sleep_fn(0.3)

    write_raw("x", dump)
    if len(errors) == len(specs):
        return empty_source(
            days, topics=[spec.id for spec in specs], error=errors[0] if errors else "X counts 全部失败"
        )
    if errors:
        log.warning("X counts 部分失败：%s", "；".join(errors))
    log.info("X counts %d 话题 × %d 日", len(specs), len(days))
    return _finish_source(days, raw_rows, specs, kind="x")


def _has_values(block: dict[str, Any] | None) -> bool:
    if not block:
        return False
    raw = ((block.get("matrix") or {}).get("raw")) or []
    return any(float(value) for row in raw for value in row)


def reindex_source(
    block: dict[str, Any],
    old_days: list[str],
    new_days: list[str],
    specs: list[TopicSpec],
    *,
    kind: str,
) -> dict[str, Any]:
    old_index = {day: i for i, day in enumerate(old_days)}
    raw_in = ((block.get("matrix") or {}).get("raw")) or []
    raw: list[list[float]] = []
    for row in raw_in:
        raw.append(
            [
                float(row[old_index[day]]) if day in old_index and old_index[day] < len(row) else 0.0
                for day in new_days
            ]
        )
    while len(raw) < len(specs):
        raw.append([0.0] * len(new_days))
    raw = raw[: len(specs)]
    return _finish_source(days=new_days, raw=raw, specs=specs, kind=kind, error="")


def coalesce(
    fresh: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    old_days: list[str],
    new_days: list[str],
    specs: list[TopicSpec],
    kind: str,
) -> dict[str, Any]:
    if not fresh.get("error"):
        return fresh
    if not previous or not _has_values(previous):
        return fresh
    merged = reindex_source(previous, old_days, new_days, specs, kind=kind)
    merged["error"] = str(fresh.get("error") or "")
    # 拉数失败时窗口右端缺日会是 0，热力条看起来像断更；用旧窗口最后一天顶上
    old_set = set(old_days)
    raw = ((merged.get("matrix") or {}).get("raw")) or []
    for row in raw:
        last = 0.0
        for index, day in enumerate(new_days):
            if index >= len(row):
                break
            if day in old_set and float(row[index]):
                last = float(row[index])
            elif day not in old_set and last:
                row[index] = last
    return merged


def load_previous(path: Path | str) -> dict[str, Any] | None:
    target = Path(path)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("读不到上一份热力快照 %s：%s", target, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _payload_topics(specs: list[TopicSpec]) -> tuple[list[str], dict[str, str], dict[str, dict[str, str]]]:
    return (
        [spec.id for spec in specs],
        {spec.id: spec.label for spec in specs},
        {spec.id: spec.as_query() for spec in specs},
    )


def build_payload(
    *,
    today: date | None = None,
    previous: dict[str, Any] | None = None,
    topics: list[TopicSpec] | None = None,
    x_topics: list[TopicSpec] | None = None,
    select_fn: Callable[[], list[TopicSpec]] | None = None,
    x_select_fn: Callable[[], list[TopicSpec]] | None = None,
    google_fn: Callable[[list[str]], dict[str, Any]] | None = None,
    x_fn: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    days = day_labels(today)
    old_days = list((previous or {}).get("days") or [])
    if topics is not None:
        specs = list(topics)
    else:
        picker = select_fn or select_trending_ai
        try:
            specs = list(picker() or [])
        except Exception as exc:  # noqa: BLE001 - 榜单失败再看昨天
            log.warning("热搜筛选失败：%s", exc)
            specs = []
        if not specs:
            specs = specs_from_payload(previous)
    if google_fn is None:
        google = fetch_google(days, topics=specs, breakout=True)
        if "_chosen" in google:
            specs = list(google.pop("_chosen") or [])
    else:
        google = google_fn(days)
        google.pop("_chosen", None)
    topic_ids, labels, queries = _payload_topics(specs)
    breakouts = [spec.id for spec in specs if spec.breakout]
    if x_topics is not None:
        x_specs = list(x_topics)
    else:
        x_picker = x_select_fn or select_x_trending_ai
        try:
            x_specs = list(x_picker() or [])
        except Exception as exc:  # noqa: BLE001 - X 榜单失败只回退 X 自己昨天的话题
            log.warning("X 热搜筛选失败：%s", exc)
            x_specs = []
        if not x_specs:
            x_specs = specs_from_source((previous or {}).get("x"))
    if x_specs:
        x_block = (x_fn or (lambda days_arg: fetch_x(days_arg, topics=x_specs)))(days)
    else:
        x_block = empty_source(days, topics=[], error="X 热搜里今天没有筛出 AI 话题")
    x_block["selection"] = {
        **(x_block.get("selection") or {}),
        "method": "x-trends-by-woeid",
    }
    same_ids = topic_ids == list((previous or {}).get("topics") or [])
    x_topic_ids = [spec.id for spec in x_specs]
    same_x_ids = x_topic_ids == list(((previous or {}).get("x") or {}).get("topics") or [])
    if same_ids:
        google = coalesce(
            google,
            (previous or {}).get("google-trends"),
            old_days=old_days,
            new_days=days,
            specs=specs,
            kind="g",
        )
    if same_x_ids:
        x_block = coalesce(
            x_block,
            (previous or {}).get("x"),
            old_days=old_days,
            new_days=days,
            specs=x_specs,
            kind="x",
        )
        x_block["selection"] = {
            **(x_block.get("selection") or {}),
            "method": "x-trends-by-woeid",
        }
    google["selection"] = {
        **(google.get("selection") or {}),
        "method": "google-trending-ai",
    }
    return {
        "generatedAt": _now_iso(),
        "days": days,
        "topics": topic_ids,
        "labels": labels,
        "queries": queries,
        "scopes": {spec.id: spec.scope for spec in specs},
        "marks": {spec.id: spec.mark for spec in specs},
        "breakouts": breakouts,
        "selection": {
            "method": "google-trending-ai",
            "volumes": {spec.id: spec.volume for spec in specs},
            "geos": {spec.id: list(spec.geos) for spec in specs},
            "plot_geo": {spec.id: spec.geo for spec in specs},
            "breakouts": breakouts,
        },
        "google-trends": google,
        "x": x_block,
    }


def write_payload(payload: dict[str, Any], output: Path | str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _load_dotenv() -> None:
    """本机 `python -m src.trends` 不经过 bootstrap，需要自己把 .env 灌进环境。

    已在环境里的变量不覆盖：CI 用 GitHub Secrets，本机已 export 的优先。
    """
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 Google 热搜筛 AI 话题并生成热力图 JSON")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    _load_dotenv()
    previous = load_previous(args.output)
    payload = build_payload(previous=previous)
    path = write_payload(payload, args.output)
    log.info("热力图已写入 %s", path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(run())
