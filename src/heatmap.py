"""话题热度：枚举、JSONL、按 ISO 周聚合。

打标和聚合拆成两个入口（`tag_topics` / `aggregate`），热力图本身只吃
`heatmap.json`。所以 mock 数据和真实流水线写出同一份结构，前端不用分叉。
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
ITEMS_DIR = ROOT / "data" / "items"
TAGGED_DIR = ROOT / "data" / "tagged"
HEATMAP_PATH = ROOT / "data" / "heatmap.json"
SITE_HEATMAP_PATH = ROOT / "site" / "data" / "heatmap.json"

# 固定枚举。顺序即矩阵行序；前端按 trend 重排，不在这里排。
TOPICS = (
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
    "other",
)
TOPIC_SET = frozenset(TOPICS)
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
    "other": "其他",
}

WINDOW_WEEKS = 12
BATCH_SIZE = 20


def iso_week(value: str | date) -> str:
    day = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def parse_iso_week(label: str) -> date:
    year_s, week_s = label.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_topics_strict(raw: Any) -> list[str] | None:
    """校验失败返回 None，让调用方重打。sanitize_topics 会把脏标签吞成 other，那会污染热力图。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    if len(raw) == 0:
        return ["other"]
    values: list[str] = []
    for item in raw:
        key = str(item).strip().lower().replace("_", "-")
        if key not in TOPIC_SET:
            return None
        if key not in values:
            values.append(key)
        if len(values) > 2:
            return None
    if not values:
        return ["other"]
    if "other" in values and len(values) > 1:
        values = [item for item in values if item != "other"]
    return values[:2]


def sanitize_topics(raw: Any) -> list[str]:
    parsed = parse_topics_strict(raw)
    return parsed if parsed is not None else ["other"]


def item_id(row: dict[str, Any], fallback: str) -> str:
    return str(row.get("id") or row.get("recordId") or fallback)


def brief_to_items(brief: dict[str, Any]) -> list[dict[str, Any]]:
    """把现有简报 JSON 收成打标脚本认的条目形状，真实管道接上后不必另造一份库。"""
    day = str(brief.get("date") or "")
    items: list[dict[str, Any]] = []
    for index, signal in enumerate(brief.get("signals") or [], start=1):
        items.append(
            {
                "id": item_id(signal, f"{day}-{index:03d}"),
                "title": str(signal.get("titleCn") or signal.get("title") or ""),
                "summary": str(signal.get("summary") or ""),
                "url": str(signal.get("url") or ""),
                "source": str(signal.get("source") or ""),
                "date": str(signal.get("publishedDate") or day),
            }
        )
    return items


def _week_labels(tagged_rows: list[dict[str, Any]], window: int = WINDOW_WEEKS) -> list[str]:
    weeks = sorted({iso_week(row["date"]) for row in tagged_rows if row.get("date")})
    if not weeks:
        return []
    latest = parse_iso_week(weeks[-1])
    labels = [iso_week(latest - timedelta(weeks=offset)) for offset in range(window - 1, -1, -1)]
    # 真实数据不足 12 周时，只从有数据的最早周切到最晚周，避免 mock 4 周被 8 列空白稀释。
    # 中间缺的周仍留空列，否则横轴会对不齐。
    first = next((label for label in labels if label in weeks), weeks[0])
    return labels[labels.index(first) :]


def build_heatmap(tagged_rows: list[dict[str, Any]], window: int = WINDOW_WEEKS) -> dict[str, Any]:
    weeks = _week_labels(tagged_rows, window=window)
    week_index = {label: i for i, label in enumerate(weeks)}
    topic_index = {topic: i for i, topic in enumerate(TOPICS)}
    raw = [[0 for _ in weeks] for _ in TOPICS]
    buckets: dict[str, list[str]] = defaultdict(list)
    catalog: dict[str, dict[str, str]] = {}

    for row in tagged_rows:
        day = str(row.get("date") or "")
        if not day:
            continue
        week = iso_week(day)
        if week not in week_index:
            continue
        ident = item_id(row, f"{week}-{len(catalog)+1}")
        catalog[ident] = {
            "title": str(row.get("title") or ""),
            "source": str(row.get("source") or ""),
            "url": str(row.get("url") or ""),
        }
        col = week_index[week]
        for topic in sanitize_topics(row.get("topics")):
            raw[topic_index[topic]][col] += 1
            key = f"{topic}|{week}"
            if ident not in buckets[key]:
                buckets[key].append(ident)

    normalized: list[list[float]] = []
    trend: dict[str, float] = {}
    for row in raw:
        peak = max(row) if row else 0
        normalized.append([round(value / peak, 4) if peak else 0.0 for value in row])
        recent = row[-2:] if len(row) >= 2 else row
        prior = row[:-2][-10:] if len(row) > 2 else []
        recent_mean = sum(recent) / len(recent) if recent else 0.0
        prior_mean = sum(prior) / len(prior) if prior else 0.0
        if prior_mean == 0:
            ratio = 2.0 if recent_mean > 0 else 1.0
        else:
            ratio = recent_mean / prior_mean
        trend[TOPICS[len(normalized) - 1]] = round(ratio, 3)

    return {
        "weeks": weeks,
        "topics": list(TOPICS),
        "labels": dict(TOPIC_LABELS),
        "matrix": {"raw": raw, "normalized": normalized},
        "items": dict(buckets),
        "itemIndex": catalog,
        "trend": trend,
        "window_end": weeks[-1] if weeks else "",
    }


def load_all_tagged(tagged_dir: Path = TAGGED_DIR) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not tagged_dir.is_dir():
        return rows
    for path in sorted(tagged_dir.glob("*.jsonl")):
        rows.extend(read_jsonl(path))
    return rows


# --- 4 周模拟数据 ----------------------------------------------------------
# 刻意做成「智能体升温、融资退烧、基建横盘」，trend 标注才有东西可看。
_MOCK_WEEKS = (
    ("2026-08-05", {"funding": 8, "product": 4, "infra": 5, "agent": 1, "safety-policy": 2}),
    ("2026-08-12", {"funding": 6, "product": 3, "infra": 5, "agent": 3, "reasoning": 2, "multimodal": 2}),
    ("2026-08-19", {"funding": 3, "product": 4, "infra": 6, "agent": 6, "reasoning": 3, "open-source-model": 3, "rag-search": 2}),
    ("2026-08-26", {"funding": 1, "product": 5, "infra": 5, "agent": 9, "reasoning": 4, "embodied": 3, "multimodal": 3, "open-source-model": 2}),
)

_MOCK_TITLES = {
    "agent": "多智能体工作流把 MCP 工具调用接到生产编排",
    "reasoning": "测试时计算让小模型在数学基准追上更大参数量",
    "multimodal": "开源视觉语言模型补上长视频理解缺口",
    "open-source-model": "新的开源权重发布并放出推理代码",
    "rag-search": "企业知识库把检索和重排收进同一条流水线",
    "infra": "推理框架把定制芯片的吞吐再抬一档",
    "embodied": "具身模型在仓库拣货任务上完成闭环演示",
    "safety-policy": "监管草案要求生成内容必须可追溯来源",
    "product": "办公套件把助手默开放进付费档",
    "funding": "某模型公司完成新一轮融资并调整管理层",
    "other": "行业会议公布与模型无关的场馆安排",
}

_MOCK_SOURCES = ("TechCrunch AI", "arXiv cs.AI", "The Information", "华尔街见闻", "GitHub Trending")


def seed_mock(*, rng_seed: int = 27) -> dict[str, Any]:
    rng = random.Random(rng_seed)
    tagged_by_day: dict[str, list[dict[str, Any]]] = {}
    serial = 0
    for day, counts in _MOCK_WEEKS:
        rows: list[dict[str, Any]] = []
        for topic, n in counts.items():
            for _ in range(n):
                serial += 1
                ident = f"mock-{day}-{serial:03d}"
                rows.append(
                    {
                        "id": ident,
                        "title": f"{_MOCK_TITLES[topic]}（{serial}）",
                        "summary": f"模拟摘要：本条归入 {TOPIC_LABELS[topic]}，用于预览热力图。",
                        "url": f"https://example.com/ai/{ident}",
                        "source": _MOCK_SOURCES[rng.randrange(len(_MOCK_SOURCES))],
                        "date": day,
                        "topics": [topic],
                    }
                )
        rng.shuffle(rows)
        tagged_by_day[day] = rows

    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    TAGGED_DIR.mkdir(parents=True, exist_ok=True)
    all_tagged: list[dict[str, Any]] = []
    for day, rows in tagged_by_day.items():
        write_jsonl(ITEMS_DIR / f"{day}.jsonl", [{k: v for k, v in row.items() if k != "topics"} for row in rows])
        write_jsonl(TAGGED_DIR / f"{day}.jsonl", rows)
        all_tagged.extend(rows)

    payload = build_heatmap(all_tagged, window=WINDOW_WEEKS)
    write_json(HEATMAP_PATH, payload)
    write_json(SITE_HEATMAP_PATH, payload)
    return payload
