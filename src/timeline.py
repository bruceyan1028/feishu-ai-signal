"""追踪对象匹配、历史回填与时间线静态数据生成。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, daily, feishu, publish, report, sources

log = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
TIMELINE_CACHE = ROOT / "site" / "data" / "timeline-latest.json"
_TERM_SPLIT_RE = re.compile(r"[,，、;/\n]+")

_EVENT_RULES = [
    ("组织变动", re.compile(r"(?i)(任命|离职|加入|辞任|ceo|cto|chief|appoint|depart|hire)")),
    ("融资并购", re.compile(r"(?i)(融资|估值|收购|并购|ipo|funding|raises?|acqui)")),
    ("合作", re.compile(r"(?i)(合作|伙伴|联盟|协议|partner|collaborat)")),
    ("定价变化", re.compile(r"(?i)(定价|降价|价格|免费|price|pricing|cost)")),
    ("政策动作", re.compile(r"(?i)(政策|监管|法案|行政令|合规|regulat|policy|law)")),
    ("技术成果", re.compile(r"(?i)(论文|研究|基准|评测|突破|paper|research|benchmark)")),
    ("产品发布", re.compile(r"(?i)(发布|上线|推出|开放|launch|release|introduc|announce)")),
]


def _table_id(token: str, configured: str, ensure) -> str:
    return configured or ensure(token)


def slugify_entity_id(name: str, taken: set[str] | None = None) -> str:
    base = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", str(name).strip().lower()).strip("-")
    base = base or "entity"
    candidate = base
    suffix = 2
    taken = taken or set()
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def split_terms(value: Any) -> list[str]:
    raw = str(sources.cell(value) or "")
    return list(
        dict.fromkeys(term.strip() for term in _TERM_SPLIT_RE.split(raw) if term.strip())
    )


def entity_from_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    name = str(sources.cell(fields.get("名称")) or "").strip()
    return {
        "recordId": str(record.get("record_id") or ""),
        "id": str(sources.cell(fields.get("entity_id")) or "").strip(),
        "name": name,
        "type": str(sources.cell(fields.get("类型")) or "机构"),
        "aliases": split_terms(fields.get("别名")),
        "keywords": split_terms(fields.get("关键词")),
        "excludes": split_terms(fields.get("排除词")),
        "status": str(sources.cell(fields.get("状态")) or "active"),
        "lookbackDays": int(
            float(
                sources.cell(fields.get("回溯天数"))
                or config.TIMELINE_DEFAULT_LOOKBACK_DAYS
            )
        ),
        "minImpact": int(
            float(
                sources.cell(fields.get("最低影响分"))
                or config.TIMELINE_DEFAULT_MIN_IMPACT
            )
        ),
    }


def _term_matches(text: str, term: str) -> bool:
    term = term.strip().lower()
    if not term:
        return False
    if re.search(r"[\u3400-\u9fff]", term):
        return term in text
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))


def match_entity(entity: dict[str, Any], signal: dict[str, Any]) -> tuple[str, int] | None:
    text = " ".join(
        [
            str(signal.get("titleCn") or ""),
            str(signal.get("title") or ""),
            str(signal.get("summary") or ""),
            " ".join(str(tag) for tag in signal.get("tags") or []),
        ]
    ).lower()
    if any(_term_matches(text, term) for term in entity.get("excludes") or []):
        return None
    name = str(entity.get("name") or "")
    if _term_matches(text, name):
        return f"名称：{name}", 95
    for alias in entity.get("aliases") or []:
        if _term_matches(text, alias):
            return f"别名：{alias}", 90
    for keyword in entity.get("keywords") or []:
        if _term_matches(text, keyword):
            return f"关键词：{keyword}", 75
    return None


def infer_event_type(signal: dict[str, Any]) -> str:
    text = f"{signal.get('titleCn') or signal.get('title') or ''} {signal.get('summary') or ''}"
    for event_type, pattern in _EVENT_RULES:
        if pattern.search(text):
            return event_type
    return "其他动向"


def event_dedup_key(signal: dict[str, Any]) -> str:
    """同一新闻即使重复入库或来自不同 record_id，也只生成一个追踪事件。"""
    published = str(signal.get("publishedDate") or "")
    title = str(signal.get("titleCn") or signal.get("title") or "").lower()
    normalized_title = re.sub(r"[\W_]+", "", title, flags=re.UNICODE)
    if normalized_title:
        return f"{published}:{normalized_title}"
    url = re.sub(r"[?#].*$", "", str(signal.get("url") or "")).rstrip("/").lower()
    return f"{published}:{url}"


def event_fields(
    entity: dict[str, Any],
    signal: dict[str, Any],
    match_basis: str,
    confidence: int,
) -> dict[str, Any]:
    record_id = str(signal.get("recordId") or "")
    published = str(signal.get("publishedDate") or "")
    try:
        event_ms = daily.date_ms(published)
    except (TypeError, ValueError):
        event_ms = int(time.time() * 1000)
    digest = hashlib.sha1(event_dedup_key(signal).encode("utf-8")).hexdigest()[:16]
    return {
        "event_id": f"{entity['id']}:{digest}",
        "entity_id": entity["id"],
        "追踪对象": entity["name"],
        "信号记录ID": record_id,
        "事件日期": event_ms,
        "事件类型": infer_event_type(signal),
        "标题": str(signal.get("titleCn") or signal.get("title") or "")[:500],
        "摘要": str(signal.get("summary") or "")[:2000],
        "影响分": int(signal.get("impact") or 0),
        "来源": str(signal.get("source") or ""),
        "原文链接": str(signal.get("url") or ""),
        "匹配依据": match_basis,
        "置信度": confidence,
        "更新时间": int(time.time() * 1000),
    }


def match_events(
    entities: list[dict[str, Any]],
    entry_records: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(CN_TZ)
    signals = [publish._signal_from_record(record) for record in entry_records]
    events: list[dict[str, Any]] = []
    for entity in entities:
        if entity.get("status") != "active" or not entity.get("id"):
            continue
        cutoff = (now - timedelta(days=max(1, int(entity["lookbackDays"])))).date()
        matched_by_key: dict[str, tuple[dict[str, Any], str, int]] = {}
        for signal in signals:
            try:
                published = datetime.strptime(
                    str(signal.get("publishedDate") or ""), "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            if published < cutoff or int(signal.get("impact") or 0) < int(
                entity.get("minImpact") or 0
            ):
                continue
            match = match_entity(entity, signal)
            if not match:
                continue
            key = event_dedup_key(signal)
            previous = matched_by_key.get(key)
            if previous is None or int(signal.get("impact") or 0) > int(
                previous[0].get("impact") or 0
            ):
                matched_by_key[key] = (signal, *match)
        events.extend(
            event_fields(entity, signal, match_basis, confidence)
            for signal, match_basis, confidence in matched_by_key.values()
        )
    return events


def payload_from_records(
    entities: list[dict[str, Any]], event_records: list[dict[str, Any]]
) -> dict[str, Any]:
    events_by_entity: dict[str, list[dict[str, Any]]] = {}
    seen_event_ids: set[str] = set()
    for record in event_records:
        fields = record.get("fields") or {}
        entity_id = str(sources.cell(fields.get("entity_id")) or "")
        event_id = str(sources.cell(fields.get("event_id")) or "")
        if not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        event_ms = int(float(sources.cell(fields.get("事件日期")) or 0))
        event_date = (
            datetime.fromtimestamp(event_ms / 1000, CN_TZ).strftime("%Y-%m-%d")
            if event_ms
            else ""
        )
        events_by_entity.setdefault(entity_id, []).append(
            {
                "id": event_id,
                "recordId": str(sources.cell(fields.get("信号记录ID")) or ""),
                "date": event_date,
                "type": str(sources.cell(fields.get("事件类型")) or "其他动向"),
                "title": str(sources.cell(fields.get("标题")) or ""),
                "summary": str(sources.cell(fields.get("摘要")) or ""),
                "impact": int(float(sources.cell(fields.get("影响分")) or 0)),
                "source": str(sources.cell(fields.get("来源")) or ""),
                "url": str(sources.cell(fields.get("原文链接")) or ""),
                "matchBasis": str(sources.cell(fields.get("匹配依据")) or ""),
                "confidence": int(float(sources.cell(fields.get("置信度")) or 0)),
            }
        )
    output = []
    seen_entity_ids: set[str] = set()
    for entity in entities:
        if entity["id"] in seen_entity_ids:
            continue
        seen_entity_ids.add(entity["id"])
        events = events_by_entity.get(entity["id"], [])
        events.sort(key=lambda event: (event["date"], event["impact"]), reverse=True)
        output.append({**entity, "events": summarize_daily_events(events)})
    return {
        "generatedAt": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "entities": output,
    }


def summarize_daily_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把同一对象同一天的流水账压成一张摘要卡，并保留全部原文入口。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(str(event.get("date") or ""), []).append(event)
    daily_events = []
    for day, items in grouped.items():
        items.sort(
            key=lambda item: (int(item.get("impact") or 0), int(item.get("confidence") or 0)),
            reverse=True,
        )
        top = items[0]
        key_points = []
        for item in items[:3]:
            point = str(item.get("summary") or item.get("title") or "").strip()
            point = re.sub(r"\s+", " ", point)
            if len(point) > 180:
                point = point[:177].rstrip("，,；;。 ") + "…"
            if point and point not in key_points:
                key_points.append(point)
        summary = "；".join(point.rstrip("。") for point in key_points) + "。"
        originals = [
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "source": str(item.get("source") or ""),
                "recordId": str(item.get("recordId") or ""),
                "type": str(item.get("type") or "其他动向"),
                "impact": int(item.get("impact") or 0),
            }
            for item in items
        ]
        daily_events.append(
            {
                "id": f"daily:{day}",
                "recordId": str(top.get("recordId") or ""),
                "date": day,
                "type": "每日汇总",
                "title": str(top.get("title") or "关键进展"),
                "summary": summary,
                "impact": max(int(item.get("impact") or 0) for item in items),
                "source": "",
                "url": "",
                "matchBasis": "",
                "confidence": max(int(item.get("confidence") or 0) for item in items),
                "count": len(items),
                "originals": originals,
            }
        )
    daily_events.sort(key=lambda item: item["date"], reverse=True)
    return daily_events


def strategy_fingerprint(events: list[dict[str, Any]]) -> str:
    evidence = [
        {
            "id": event.get("id"),
            "title": event.get("title"),
            "summary": event.get("summary"),
            "originalIds": [
                item.get("recordId") for item in event.get("originals") or []
            ],
        }
        for event in events
    ]
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_strategy_cache(path: Path = TIMELINE_CACHE) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(entity.get("id") or ""): entity["strategy"]
        for entity in payload.get("entities") or []
        if isinstance(entity.get("strategy"), dict)
    }


def validate_strategy(
    raw: dict[str, Any], valid_event_ids: set[str], fingerprint: str
) -> dict[str, Any]:
    themes = []
    valid_theme_ids: set[str] = set()
    for index, theme in enumerate(raw.get("themes") or []):
        if not isinstance(theme, dict):
            continue
        theme_id = str(theme.get("id") or f"theme-{index + 1}")
        event_ids = [
            str(event_id)
            for event_id in theme.get("eventIds") or []
            if str(event_id) in valid_event_ids
        ]
        if not event_ids:
            continue
        valid_theme_ids.add(theme_id)
        themes.append(
            {
                "id": theme_id,
                "name": str(theme.get("name") or "战略主题")[:80],
                "summary": str(theme.get("summary") or "")[:500],
                "eventIds": list(dict.fromkeys(event_ids)),
            }
        )
    relations = []
    for relation in raw.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        from_id = str(relation.get("fromEventId") or "")
        to_id = str(relation.get("toEventId") or "")
        if from_id not in valid_event_ids or to_id not in valid_event_ids:
            continue
        relations.append(
            {
                "fromEventId": from_id,
                "toEventId": to_id,
                "type": str(relation.get("type") or "reinforces")[:40],
                "label": str(relation.get("label") or "相互印证")[:80],
                "explanation": str(relation.get("explanation") or "")[:500],
                "confidence": max(0, min(100, int(relation.get("confidence") or 0))),
            }
        )
    hypotheses = []
    for hypothesis in raw.get("hypotheses") or []:
        if not isinstance(hypothesis, dict):
            continue
        evidence_ids = [
            str(event_id)
            for event_id in hypothesis.get("evidenceEventIds") or []
            if str(event_id) in valid_event_ids
        ]
        theme_ids = [
            str(theme_id)
            for theme_id in hypothesis.get("themeIds") or []
            if str(theme_id) in valid_theme_ids
        ]
        if not evidence_ids:
            continue
        hypotheses.append(
            {
                "title": str(hypothesis.get("title") or "布局假设")[:120],
                "assessment": str(hypothesis.get("assessment") or "")[:1000],
                "confidence": max(
                    0, min(100, int(hypothesis.get("confidence") or 0))
                ),
                "themeIds": list(dict.fromkeys(theme_ids)),
                "evidenceEventIds": list(dict.fromkeys(evidence_ids)),
                "counterSignals": str(hypothesis.get("counterSignals") or "")[:500],
                "watchFor": [
                    str(item)[:200]
                    for item in hypothesis.get("watchFor") or []
                    if str(item).strip()
                ][:4],
            }
        )
    return {
        "fingerprint": fingerprint,
        "generatedAt": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "overview": str(raw.get("overview") or "")[:1200],
        "themes": themes[:5],
        "relations": relations[:10],
        "hypotheses": hypotheses[:4],
    }


def synthesize_strategy(
    entity: dict[str, Any],
    events: list[dict[str, Any]],
    cached: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fingerprint = strategy_fingerprint(events)
    if cached and cached.get("fingerprint") == fingerprint:
        return cached
    if not events or not config.LLM_API_KEY:
        return {
            "fingerprint": fingerprint,
            "generatedAt": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "overview": "",
            "themes": [],
            "relations": [],
            "hypotheses": [],
        }
    evidence = [
        {
            "eventId": event["id"],
            "date": event["date"],
            "title": event["title"],
            "summary": event["summary"],
            "originals": [
                {
                    "title": original["title"],
                    "source": original["source"],
                    "type": original["type"],
                }
                for original in event.get("originals") or []
            ],
        }
        for event in events
    ]
    prompt = f"""
你是一名严谨的企业战略情报分析师。请基于下列已编号证据，研判主体
「{entity['name']}」的长期商业计划和潜在布局意图。

必须遵守：
1. 先梳理跨时间事件之间的承接、强化、能力前置、商业化、生态扩张或资本布局关系；
2. 区分“已发生事实”和“战略假设”，不得把推测写成事实；
3. 每个主题、关系和假设必须引用给定 eventId，禁止创造不存在的引用；
4. 假设必须给出 0-100 置信度、可能反证和未来可验证的观察信号；
5. 避免逐条复述新闻，重点解释这些动作组合起来意味着什么；
6. 使用简洁中文。

只返回 JSON：
{{
  "overview": "200-350字总体研判",
  "themes": [
    {{"id":"theme-1","name":"主题名","summary":"该主题如何形成","eventIds":["daily:YYYY-MM-DD"]}}
  ],
  "relations": [
    {{"fromEventId":"daily:YYYY-MM-DD","toEventId":"daily:YYYY-MM-DD","type":"capability|commercialization|expansion|partnership|capital|reinforces","label":"短关系名","explanation":"两项动作的关系","confidence":85}}
  ],
  "hypotheses": [
    {{"title":"长期布局假设","assessment":"基于证据的判断","confidence":80,
      "themeIds":["theme-1"],"evidenceEventIds":["daily:YYYY-MM-DD"],
      "counterSignals":"什么情况会削弱该判断","watchFor":["未来验证信号"]}}
  ]
}}

证据：
{json.dumps(evidence, ensure_ascii=False)}
""".strip()
    raw = report._llm_json(prompt)
    return validate_strategy(raw, {event["id"] for event in events}, fingerprint)


def sync(entity_id: str | None = None) -> dict[str, Any]:
    token = feishu.get_tenant_access_token()
    entity_table_id = _table_id(
        token,
        config.FEISHU_TRACKED_ENTITY_TABLE_ID,
        feishu.ensure_tracked_entity_table,
    )
    event_table_id = _table_id(
        token,
        config.FEISHU_TRACKED_EVENT_TABLE_ID,
        feishu.ensure_tracked_event_table,
    )
    entity_records = feishu.read_all_records_with_ids(token, entity_table_id)
    entities = []
    seen_entity_ids: set[str] = set()
    duplicate_entity_record_ids: list[str] = []
    for record in entity_records:
        entity = entity_from_record(record)
        if entity["id"] in seen_entity_ids:
            duplicate_entity_record_ids.append(str(record.get("record_id") or ""))
            continue
        seen_entity_ids.add(entity["id"])
        entities.append(entity)
    feishu.batch_delete_records(
        token, entity_table_id, duplicate_entity_record_ids
    )
    if entity_id:
        entities = [entity for entity in entities if entity["id"] == entity_id]
    entries = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    existing_records = feishu.read_all_records_with_ids(token, event_table_id)
    existing_ids = {
        str(sources.cell((record.get("fields") or {}).get("event_id")) or "")
        for record in existing_records
    }
    expected_by_id = {
        str(fields["event_id"]): fields for fields in match_events(entities, entries)
    }
    expected_ids = set(expected_by_id)
    synced_entity_ids = {str(entity["id"]) for entity in entities}
    stale_record_ids = []
    seen_existing_ids: set[str] = set()
    for record in existing_records:
        fields = record.get("fields") or {}
        existing_entity_id = str(sources.cell(fields.get("entity_id")) or "")
        existing_event_id = str(sources.cell(fields.get("event_id")) or "")
        if existing_entity_id not in synced_entity_ids:
            continue
        if existing_event_id not in expected_ids or existing_event_id in seen_existing_ids:
            stale_record_ids.append(str(record.get("record_id") or ""))
            continue
        seen_existing_ids.add(existing_event_id)
    feishu.batch_delete_records(token, event_table_id, stale_record_ids)
    if stale_record_ids:
        existing_records = [
            record
            for record in existing_records
            if str(record.get("record_id") or "") not in set(stale_record_ids)
        ]
        existing_ids = {
            str(sources.cell((record.get("fields") or {}).get("event_id")) or "")
            for record in existing_records
        }
    new_fields = [
        fields
        for event_id, fields in expected_by_id.items()
        if event_id not in existing_ids
    ]
    feishu.batch_create_table_records(token, event_table_id, new_fields)
    if new_fields:
        existing_records.extend({"fields": fields} for fields in new_fields)
    # 单实体回填仍导出全部对象，避免覆盖静态 JSON 时丢掉其它时间线。
    if entity_id:
        entities = []
        seen_entity_ids = set()
        for record in feishu.read_all_records_with_ids(token, entity_table_id):
            entity = entity_from_record(record)
            if entity["id"] in seen_entity_ids:
                continue
            seen_entity_ids.add(entity["id"])
            entities.append(entity)
        existing_records = feishu.read_all_records_with_ids(token, event_table_id)
    payload = payload_from_records(entities, existing_records)
    cache = read_strategy_cache()
    for entity in payload["entities"]:
        try:
            entity["strategy"] = synthesize_strategy(
                entity, entity["events"], cache.get(entity["id"])
            )
        except Exception as exc:  # noqa: BLE001 - 时间线仍应可用
            log.warning("战略研判失败 %s：%s", entity["name"], exc)
            entity["strategy"] = cache.get(entity["id"]) or {
                "fingerprint": strategy_fingerprint(entity["events"]),
                "overview": "",
                "themes": [],
                "relations": [],
                "hypotheses": [],
            }
    payload["createdEvents"] = len(new_fields)
    return payload


def write_payload(payload: dict[str, Any], output: Path | str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity-id", help="只回填指定追踪对象")
    parser.add_argument("--output", default="site/data/timeline-latest.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    payload = sync(args.entity_id)
    output = write_payload(payload, args.output)
    log.info(
        "时间线已更新：%d 个对象，新增 %d 个事件 → %s",
        len(payload["entities"]),
        payload["createdEvents"],
        output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
