"""首页话题热力图：Google Trends 兴趣指数 + X 近 7 日讨论量。

和 `heatmap.py` 那份「简报条目 × ISO 周」不是同一种量。这边是右栏 Google | X
切换吃的 7 日快照，落成 `site/data/heatmap-trends.json`。两源各自捕获异常，
一块挂了另一块照常写；失败时尽量沿用上一份快照里重叠的日期列。

    python -m src.trends --output site/data/heatmap-trends.json

Google 走 unofficial pytrends：单次最多 5 词，分数相对当次请求。用锚点词
`artificial intelligence` 把各批缩到同一标尺，格子颜色才能跨话题比。
GitHub Actions 出口经常被 Trends 拒，失败不让 CI 红。

X 走 `tweets/counts/recent`，和白名单时间线不是同一条产品、也不共用权限。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

log = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "site" / "data" / "heatmap-trends.json"
RAW_DIR = ROOT / "data" / "trends"

WINDOW_DAYS = 7
ANCHOR = "artificial intelligence"
BATCH_SIZE = 4  # 4 话题 + 锚点 = Trends 上限 5
GOOGLE_TZ = -480  # 北京 UTC+8，pytrends 用「西经分钟」
RETRY_SLEEP = 2.0
BATCH_SLEEP = 2.0

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
    "agent": {"g": "AI agent MCP", "x": '("AI agent" OR MCP OR #AIAgents) -is:retweet'},
    "reasoning": {"g": "test time compute", "x": '("test-time compute" OR #Reasoning) -is:retweet'},
    "multimodal": {
        "g": "image video generation AI",
        "x": '("image generation" OR "video generation" OR #Multimodal) -is:retweet',
    },
    "open-source-model": {"g": "open source LLM", "x": '("open source LLM" OR #OpenSourceAI) -is:retweet'},
    "rag-search": {"g": "RAG retrieval augmented", "x": '(RAG OR "retrieval augmented" OR #RAG) -is:retweet'},
    "infra": {"g": "AI chips inference GPU", "x": '("AI chips" OR inference OR #AIInfra) -is:retweet'},
    "embodied": {"g": "humanoid robot AI", "x": '("humanoid robot" OR #EmbodiedAI) -is:retweet'},
    "safety-policy": {"g": "AI regulation safety", "x": '("AI regulation" OR #AISafety) -is:retweet'},
    "product": {"g": "ChatGPT Copilot product", "x": '(ChatGPT OR Copilot OR #AIProduct) -is:retweet'},
    "funding": {"g": "AI startup funding", "x": '("AI startup" OR "AI funding" OR #AIFunding) -is:retweet'},
}

BatchFn = Callable[[list[str]], tuple[list[str], dict[str, list[float]], dict[str, list[str]]]]
CountsFn = Callable[[str, str], dict[str, Any]]


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


def trend_ratios(raw: list[list[float]]) -> dict[str, float]:
    trend: dict[str, float] = {}
    for topic, row in zip(TOPICS, raw):
        prior = row[:-1]
        prior_mean = sum(prior) / len(prior) if prior else 0.0
        last = row[-1] if row else 0.0
        trend[topic] = round(last / prior_mean, 3) if prior_mean else 1.0
    return trend


def empty_source(days: list[str], *, error: str = "") -> dict[str, Any]:
    cols = len(days) or WINDOW_DAYS
    raw = [[0.0 for _ in range(cols)] for _ in TOPICS]
    return {
        "error": error,
        "matrix": {"raw": raw, "normalized": normalize_rows(raw)},
        "trend": {topic: 1.0 for topic in TOPICS},
        "items": {},
        "itemIndex": {},
    }


def _align_series(dates: list[str], values: list[float], days: list[str]) -> list[float]:
    lookup = {day: float(value) for day, value in zip(dates, values)}
    return [lookup.get(day, 0.0) for day in days]


def _scale_to_anchor(
    reference: list[float], batch_anchor: list[float], series: list[float]
) -> list[float]:
    out: list[float] = []
    for ref, anchor, value in zip(reference, batch_anchor, series):
        if anchor > 0:
            out.append(round(value * (ref / anchor), 2))
        else:
            out.append(round(value, 2))
    return out


def _google_explore_url(query: str) -> str:
    return f"https://trends.google.com/trends/explore?date=now%207-d&q={quote(query)}"


def _x_search_url(query: str) -> str:
    return f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"


def _source_links(
    days: list[str],
    *,
    kind: str,
    related: dict[str, list[str]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    items: dict[str, list[str]] = {}
    index: dict[str, dict[str, str]] = {}
    related = related or {}
    for topic in TOPICS:
        query = QUERIES[topic]
        is_google = kind == "g"
        ident = f"{kind}-{topic}"
        index[ident] = {
            "title": f"{'Google Trends' if is_google else 'X 热搜'} · {query['g' if is_google else 'x']}",
            "source": "Google Trends" if is_google else "X",
            "url": _google_explore_url(query["g"]) if is_google else _x_search_url(query["x"]),
        }
        extras: list[str] = []
        if is_google:
            for rank, phrase in enumerate(related.get(topic, [])[:5], start=1):
                extra_id = f"{ident}-q{rank}"
                extras.append(extra_id)
                index[extra_id] = {
                    "title": phrase,
                    "source": "Google Trends",
                    "url": _google_explore_url(phrase),
                }
        last = days[-1] if days else ""
        for day in days:
            key = f"{topic}|{day}"
            ids = [ident]
            if day == last:
                ids.extend(extras)
            items[key] = ids
    return items, index


def _finish_source(
    days: list[str],
    raw: list[list[float]],
    *,
    kind: str,
    error: str = "",
    related: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    items, index = _source_links(days, kind=kind, related=related)
    return {
        "error": error,
        "matrix": {"raw": raw, "normalized": normalize_rows(raw)},
        "trend": trend_ratios(raw),
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


def _pytrends_batch(keywords: list[str]) -> tuple[list[str], dict[str, list[float]], dict[str, list[str]]]:
    from pytrends.request import TrendReq

    req = TrendReq(hl="en-US", tz=GOOGLE_TZ)
    req.build_payload(list(keywords), timeframe="today 7-d", geo="")
    frame = req.interest_over_time()
    if frame is None or getattr(frame, "empty", True):
        raise RuntimeError("Google Trends 未返回 interest_over_time")
    if "isPartial" in frame.columns:
        frame = frame.drop(columns=["isPartial"])
    dates = []
    for idx in frame.index:
        if hasattr(idx, "date"):
            dates.append(idx.date().isoformat())
        else:
            dates.append(str(idx)[:10])
    series: dict[str, list[float]] = {}
    for keyword in keywords:
        if keyword in frame.columns:
            series[keyword] = [float(value) for value in frame[keyword].fillna(0).tolist()]
        else:
            series[keyword] = [0.0] * len(dates)
    related: dict[str, list[str]] = {}
    try:
        payload = req.related_queries() or {}
    except Exception as exc:  # noqa: BLE001 - 配图链接失败不影响指数
        log.warning("Google Trends related_queries 失败：%s", exc)
        payload = {}
    for keyword, blob in payload.items():
        if not isinstance(blob, dict):
            continue
        rising = blob.get("rising")
        phrases: list[str] = []
        if rising is not None and getattr(rising, "empty", True) is False:
            column = "query" if "query" in rising.columns else rising.columns[0]
            phrases = [str(value) for value in rising[column].tolist() if str(value).strip()][:5]
        if phrases:
            related[keyword] = phrases
    return dates, series, related


def _call_batch(batch_fn: BatchFn, keywords: list[str], *, retries: int, sleep_fn: Callable[[float], Any]) -> tuple[list[str], dict[str, list[float]], dict[str, list[str]]]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return batch_fn(keywords)
        except Exception as exc:  # noqa: BLE001 - 重试后再写成 error
            last_exc = exc
            log.warning("Google Trends 第 %d/%d 次失败：%s", attempt + 1, retries, exc)
            if attempt + 1 < retries:
                sleep_fn(RETRY_SLEEP * (attempt + 1))
    raise RuntimeError(str(last_exc) if last_exc else "Google Trends 失败") from last_exc


def fetch_google(
    days: list[str],
    *,
    batch_fn: BatchFn | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
    retries: int = 3,
) -> dict[str, Any]:
    worker = batch_fn or _pytrends_batch
    raw_dump: list[dict[str, Any]] = []
    by_topic: dict[str, list[float]] = {}
    related_by_topic: dict[str, list[str]] = {}
    reference_anchor: list[float] | None = None
    topics = list(TOPICS)
    try:
        for start in range(0, len(topics), BATCH_SIZE):
            chunk = topics[start : start + BATCH_SIZE]
            keywords = [ANCHOR, *[QUERIES[topic]["g"] for topic in chunk]]
            dates, series, related = _call_batch(worker, keywords, retries=retries, sleep_fn=sleep_fn)
            raw_dump.append({"keywords": keywords, "dates": dates, "series": series, "related": related})
            anchor = _align_series(dates, series.get(ANCHOR) or [], days)
            if reference_anchor is None:
                reference_anchor = anchor
            for topic in chunk:
                query = QUERIES[topic]["g"]
                aligned = _align_series(dates, series.get(query) or [], days)
                by_topic[topic] = _scale_to_anchor(reference_anchor, anchor, aligned)
                phrases = related.get(query) or related.get(topic) or []
                if phrases:
                    related_by_topic[topic] = phrases
            if start + BATCH_SIZE < len(topics):
                sleep_fn(BATCH_SLEEP)
    except Exception as exc:  # noqa: BLE001 - 与看板一样，失败写成 error
        log.warning("Google Trends 获取失败：%s", exc)
        return empty_source(days, error=str(exc))

    write_raw("google", raw_dump)
    raw = [by_topic.get(topic, [0.0] * len(days)) for topic in TOPICS]
    log.info("Google Trends %d 话题 × %d 日", len(TOPICS), len(days))
    return _finish_source(days, raw, kind="g", related=related_by_topic)


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
    bearer: str | None = None,
    api_get: CountsFn | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    token = (os.environ.get("X_BEARER_TOKEN") or "").strip() if bearer is None else bearer.strip()
    if not token:
        return empty_source(days, error="未配置 X_BEARER_TOKEN")

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
    for index, topic in enumerate(TOPICS):
        query = QUERIES[topic]["x"]
        try:
            payload = getter(query, start_iso)
            dump.append({"topic": topic, "query": query, "payload": payload})
            raw_rows.append(_bucket_counts(payload, days))
        except Exception as exc:  # noqa: BLE001 - 单话题失败记下来，不全盘放弃
            log.warning("X counts %s 失败：%s", topic, exc)
            errors.append(f"{topic}: {exc}")
            raw_rows.append([0.0] * len(days))
        if index + 1 < len(TOPICS):
            sleep_fn(0.3)

    write_raw("x", dump)
    if len(errors) == len(TOPICS):
        return empty_source(days, error=errors[0] if errors else "X counts 全部失败")
    if errors:
        log.warning("X counts 部分失败：%s", "；".join(errors))
    log.info("X counts %d 话题 × %d 日", len(TOPICS), len(days))
    return _finish_source(days, raw_rows, kind="x")


def _has_values(block: dict[str, Any] | None) -> bool:
    if not block:
        return False
    raw = ((block.get("matrix") or {}).get("raw")) or []
    return any(float(value) for row in raw for value in row)


def reindex_source(
    block: dict[str, Any], old_days: list[str], new_days: list[str], *, kind: str
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
    while len(raw) < len(TOPICS):
        raw.append([0.0] * len(new_days))
    raw = raw[: len(TOPICS)]
    items, catalog = _source_links(new_days, kind=kind)
    return {
        "error": "",
        "matrix": {"raw": raw, "normalized": normalize_rows(raw)},
        "trend": trend_ratios(raw),
        "items": items,
        "itemIndex": catalog,
    }


def coalesce(
    fresh: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    old_days: list[str],
    new_days: list[str],
    kind: str,
) -> dict[str, Any]:
    if not fresh.get("error"):
        return fresh
    if not previous or not _has_values(previous):
        return fresh
    merged = reindex_source(previous, old_days, new_days, kind=kind)
    merged["error"] = str(fresh.get("error") or "")
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


def build_payload(
    *,
    today: date | None = None,
    previous: dict[str, Any] | None = None,
    google_fn: Callable[[list[str]], dict[str, Any]] | None = None,
    x_fn: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    days = day_labels(today)
    old_days = list((previous or {}).get("days") or [])
    google = (google_fn or fetch_google)(days)
    x_block = (x_fn or fetch_x)(days)
    google = coalesce(
        google, (previous or {}).get("google-trends"), old_days=old_days, new_days=days, kind="g"
    )
    x_block = coalesce(x_block, (previous or {}).get("x"), old_days=old_days, new_days=days, kind="x")
    return {
        "generatedAt": _now_iso(),
        "days": days,
        "topics": list(TOPICS),
        "labels": dict(TOPIC_LABELS),
        "queries": {key: dict(value) for key, value in QUERIES.items()},
        "google-trends": google,
        "x": x_block,
    }


def write_payload(payload: dict[str, Any], output: Path | str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Google / X 话题热力图 JSON")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    previous = load_previous(args.output)
    payload = build_payload(previous=previous)
    path = write_payload(payload, args.output)
    log.info("热力图已写入 %s", path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(run())
