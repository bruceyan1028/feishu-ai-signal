"""聚合近七天已分析信号，生成并持久化 AI 自动周报。"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from . import cluster, config, daily, feishu, report

log = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))


def week_window(end_day: date | None = None) -> tuple[date, date]:
    end_day = end_day or datetime.now(CN_TZ).date()
    return end_day - timedelta(days=max(1, config.WEEKLY_LOOKBACK_DAYS) - 1), end_day


def week_id(end_day: date) -> str:
    iso = end_day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _day_ms(value: date, *, next_day: bool = False) -> int:
    if next_day:
        value += timedelta(days=1)
    return int(datetime.combine(value, time.min, tzinfo=CN_TZ).timestamp() * 1000)


def _table_id(token: str, configured: str, ensure) -> str:
    return configured or ensure(token)


def read_pending(
    token: str, pending_table_id: str, target_week: str
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for record in feishu.read_all_records_with_ids(token, pending_table_id):
        fields = record.get("fields") or {}
        if str(daily.scalar(fields.get("状态")) or "") != "待纳入":
            continue
        target = str(daily.scalar(fields.get("目标周期")) or "").strip()
        if target and target != target_week:
            continue
        pending.append(record)
    return pending


def collect_candidates(
    records: list[dict[str, Any]],
    params: list[dict[str, Any]],
    start_day: date,
    end_day: date,
    pending_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    start_ms = _day_ms(start_day)
    end_ms = _day_ms(end_day, next_day=True)
    priorities = daily._priority_map(params)
    active = daily._active_source_ids(params)
    candidates: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    pending_ids = pending_ids or set()
    for record in records:
        record_id = str(record.get("record_id") or "")
        if not record_id or record_id in seen_record_ids:
            continue
        fields = record.get("fields") or {}
        source_id = str(daily.scalar(fields.get("source_id")) or "")
        stamp = int(float(daily.scalar(fields.get("发布时间")) or 0))
        in_window = start_ms <= stamp < end_ms
        if record_id not in pending_ids:
            if not in_window or source_id not in active:
                continue
            if daily._existing_analysis(fields) is None:
                continue
        candidates.append(
            {
                "record_id": record_id,
                "fields": fields,
                "source_id": source_id,
                "priority": priorities.get(source_id, "P2"),
                "stamp": stamp,
            }
        )
        seen_record_ids.add(record_id)
    collapsed = cluster.collapse_for_brief(
        candidates, threshold=0.85, limit=config.WEEKLY_SIGNAL_LIMIT
    )
    # 管理员明确加入的条目不能因为同事件折叠而消失；它们需要在 pendingFocus
    # 中可追溯，即使同簇已有更高层级的官方主条目。
    kept_ids = {str(item.get("record_id") or "") for item in collapsed}
    for item in candidates:
        record_id = str(item.get("record_id") or "")
        if record_id in pending_ids and record_id not in kept_ids:
            collapsed.append(item)
            kept_ids.add(record_id)
    return collapsed


def _analysis_fields(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "中文标题": analysis["title_cn"],
        "中文摘要": analysis["summary_cn"],
        "AI深度解读": analysis.get("deep_analysis_cn") or "",
        "为何重要": analysis["why"],
        "影响分": analysis["impact"],
        "新颖度": analysis["novelty"],
        "可行动性": analysis["actionability"],
        "紧迫度": daily.URGENCY_TO_TABLE[analysis["urgency"]],
        "主题": analysis["topics"],
        "状态": "已分析",
    }


def ensure_pending_analyses(
    token: str,
    records_by_id: dict[str, dict[str, Any]],
    pending_records: list[dict[str, Any]],
) -> None:
    updates: list[dict[str, Any]] = []
    for pending in pending_records:
        pending_fields = pending.get("fields") or {}
        record_id = str(daily.scalar(pending_fields.get("条目记录ID")) or "")
        entry = records_by_id.get(record_id)
        if not entry:
            continue
        fields = entry.get("fields") or {}
        if daily._existing_analysis(fields) is not None:
            continue
        analysis = daily.analyze_signal(fields)
        new_fields = _analysis_fields(analysis)
        fields.update(new_fields)
        updates.append({"record_id": record_id, "fields": new_fields})
    feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)


def signal_from_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    fields = item.get("fields") or {}
    analysis = daily._existing_analysis(fields)
    if analysis is None:
        return None
    signal = daily._signal_from_fields(
        str(item.get("record_id") or ""),
        fields,
        analysis,
        priority=str(item.get("priority") or "P2"),
        tier=str(daily.scalar(fields.get("层级")) or ""),
    )
    signal["qualityScore"] = float(daily.scalar(fields.get("质量分")) or 0)
    return signal


def deterministic_metrics(
    signals: list[dict[str, Any]], previous: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    impacts = [int(signal.get("impact") or 0) for signal in signals]
    categories = {str(signal.get("category") or "其他") for signal in signals}
    sources = {str(signal.get("source") or "") for signal in signals if signal.get("source")}
    values = {
        "信号总数": len(signals),
        "高影响(≥80)": sum(score >= 80 for score in impacts),
        "覆盖领域": len(categories),
        "覆盖来源": len(sources),
        "平均影响分": round(sum(impacts) / len(impacts)) if impacts else 0,
    }
    previous_values: dict[str, int] = {}
    for item in (previous or {}).get("metrics") or []:
        try:
            previous_values[str(item.get("label") or "")] = int(item.get("value") or 0)
        except (TypeError, ValueError):
            continue
    result: list[dict[str, str]] = []
    for label, value in values.items():
        if label in previous_values:
            delta = value - previous_values[label]
            sub = f"较上周 {delta:+d}" if delta else "与上周持平"
        else:
            sub = "本周"
        result.append({"label": label, "value": str(value), "sub": sub})
    return result


def _previous_weekly(
    token: str, table_id: str, current_week_id: str
) -> dict[str, Any] | None:
    rows: list[tuple[int, dict[str, Any]]] = []
    for record in feishu.read_all_records_with_ids(token, table_id):
        fields = record.get("fields") or {}
        if str(daily.scalar(fields.get("周报ID")) or "") == current_week_id:
            continue
        try:
            payload = json.loads(str(daily.scalar(fields.get("周报内容")) or "{}"))
            end_ms = int(float(daily.scalar(fields.get("周期结束")) or 0))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            rows.append((end_ms, payload))
    return max(rows, key=lambda item: item[0])[1] if rows else None


def synthesize(
    signals: list[dict[str, Any]],
    metrics: list[dict[str, str]],
    pending_ids: set[str],
) -> dict[str, Any]:
    numbered = "\n".join(
        (
            f"[{signal['recordId']}] {signal.get('titleCn') or signal.get('title')}｜"
            f"{signal.get('source')}｜{signal.get('category')}｜影响{signal.get('impact')}｜"
            f"{signal.get('summary')}"
        )
        for signal in signals
    )
    pending_note = "、".join(sorted(pending_ids)) or "无"
    prompt = f"""你是 AI 情报主编。只依据给定信号输出严格 JSON，不得虚构事实或数字。
输出字段：
headline：本周唯一主线，一句话；
thesis：300-500字综述；
areas：3-6项，每项含 cat、text、refs；refs 只能使用方括号中的 recordId；
topSignals：最重要的3-5个 recordId；
risks、opportunities、actions、nextWeek：各3-5条中文字符串。
指标由程序计算，不要在正文改写或新增统计数字。
管理员额外关注 recordId：{pending_note}
确定性指标：{json.dumps(metrics, ensure_ascii=False)}
信号：
{numbered}"""
    return report._llm_json(prompt)


def validate_synthesis(
    raw: dict[str, Any], valid_ids: set[str], fallback_ids: list[str]
) -> dict[str, Any]:
    def strings(name: str, limit: int = 5) -> list[str]:
        return [
            str(item).strip()
            for item in (raw.get(name) or [])
            if str(item).strip()
        ][:limit]

    areas: list[dict[str, Any]] = []
    for item in raw.get("areas") or []:
        if not isinstance(item, dict):
            continue
        refs = [str(ref) for ref in item.get("refs") or [] if str(ref) in valid_ids]
        text = str(item.get("text") or "").strip()
        category = str(item.get("cat") or "").strip()
        if text and category:
            areas.append({"cat": category, "text": text, "refs": refs[:8]})
    top = [str(ref) for ref in raw.get("topSignals") or [] if str(ref) in valid_ids]
    top = list(dict.fromkeys(top))[:5] or fallback_ids[:5]
    headline = str(raw.get("headline") or "").strip()
    thesis = str(raw.get("thesis") or "").strip()
    if not headline or not thesis or not areas:
        raise RuntimeError("周报 LLM 输出缺少 headline、thesis 或 areas")
    return {
        "headline": headline,
        "thesis": thesis,
        "areas": areas[:6],
        "topSignals": top,
        "risks": strings("risks"),
        "opportunities": strings("opportunities"),
        "actions": strings("actions"),
        "nextWeek": strings("nextWeek"),
    }


def _upsert_weekly(
    token: str, table_id: str, payload: dict[str, Any]
) -> str:
    record = None
    for item in feishu.read_all_records_with_ids(token, table_id):
        if str(daily.scalar((item.get("fields") or {}).get("周报ID")) or "") == payload["weekId"]:
            record = item
            break
    # 完整 signals 含正文、媒体和聚合信息，写进一个飞书文本单元格会超过
    # 100KB 限制。表内保存周报结构与 recordId，网页所需完整快照留在 JSON。
    stored_payload = {key: value for key, value in payload.items() if key != "signals"}
    fields = {
        "周报ID": payload["weekId"],
        "周期开始": _day_ms(date.fromisoformat(payload["periodStart"])),
        "周期结束": _day_ms(date.fromisoformat(payload["periodEnd"])),
        "周报标题": payload["title"],
        "核心判断": payload["headline"],
        "综述": payload["thesis"],
        "周报内容": json.dumps(stored_payload, ensure_ascii=False),
        "信号记录ID": json.dumps(
            [signal["recordId"] for signal in payload["signals"]], ensure_ascii=False
        ),
        "额外关注记录ID": json.dumps(
            [item["recordId"] for item in payload["pendingFocus"]], ensure_ascii=False
        ),
        "状态": "已发布",
        "网页路径": f"/?page=tasks&tab=report&week={payload['weekId']}",
    }
    if record:
        feishu.update_record(token, table_id, str(record["record_id"]), fields)
        return str(record["record_id"])
    fields["发送状态"] = "待发送"
    return str(feishu.create_record(token, table_id, fields).get("record_id") or "")


def generate(end_day: date | None = None) -> dict[str, Any]:
    if not config.LLM_API_KEY:
        raise config.ConfigError("生成真实周报需要 LLM_API_KEY")
    start_day, end_day = week_window(end_day)
    current_week = week_id(end_day)
    token = feishu.get_tenant_access_token()
    weekly_table_id = _table_id(
        token, config.FEISHU_WEEKLY_TABLE_ID, feishu.ensure_weekly_report_table
    )
    pending_table_id = _table_id(
        token,
        config.FEISHU_WEEKLY_PENDING_TABLE_ID,
        feishu.ensure_weekly_pending_table,
    )
    params = feishu.read_param_records(token)
    records = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    records_by_id = {str(record.get("record_id") or ""): record for record in records}
    pending_records = read_pending(token, pending_table_id, current_week)
    pending_ids = {
        str(daily.scalar((item.get("fields") or {}).get("条目记录ID")) or "")
        for item in pending_records
    }
    pending_ids.discard("")
    ensure_pending_analyses(token, records_by_id, pending_records)
    candidates = collect_candidates(records, params, start_day, end_day, pending_ids)
    signals = [signal_from_candidate(item) for item in candidates]
    signals = [signal for signal in signals if signal]
    signals.sort(
        key=lambda signal: (
            float(signal.get("qualityScore") or 0),
            int(signal.get("impact") or 0),
            int(signal.get("novelty") or 0),
            int(signal.get("actionability") or 0),
        ),
        reverse=True,
    )
    if not signals:
        raise RuntimeError("近七天没有可用于周报的已分析信号")
    previous = _previous_weekly(token, weekly_table_id, current_week)
    metrics = deterministic_metrics(signals, previous)
    raw = synthesize(signals, metrics, pending_ids)
    synthesized = validate_synthesis(
        raw,
        {str(signal["recordId"]) for signal in signals},
        [str(signal["recordId"]) for signal in signals],
    )
    pending_focus = [
        {
            "recordId": record_id,
            "titleCn": next(
                (
                    str(signal.get("titleCn") or signal.get("title") or "")
                    for signal in signals
                    if signal.get("recordId") == record_id
                ),
                "",
            ),
        }
        for record_id in sorted(pending_ids)
        if record_id in {str(signal["recordId"]) for signal in signals}
    ]
    payload: dict[str, Any] = {
        "weekId": current_week,
        "periodStart": start_day.isoformat(),
        "periodEnd": end_day.isoformat(),
        "period": f"{start_day.isoformat()} → {end_day.isoformat()}",
        "title": f"AI Signal 自动周报 · {current_week}",
        "metrics": metrics,
        "signals": signals,
        "pendingFocus": pending_focus,
        "generatedAt": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        **synthesized,
    }
    weekly_record_id = _upsert_weekly(token, weekly_table_id, payload)
    payload["weeklyRecordId"] = weekly_record_id
    payload["weeklyTableId"] = weekly_table_id
    if pending_records:
        feishu.batch_update_records(
            token,
            pending_table_id,
            [
                {"record_id": str(item["record_id"]), "fields": {"状态": "已纳入"}}
                for item in pending_records
            ],
        )
    return payload


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="周期结束日期 YYYY-MM-DD，默认北京时间今天")
    parser.add_argument("--output", default="output/weekly-report.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    end_day = date.fromisoformat(args.date) if args.date else None
    payload = generate(end_day)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已生成 %s，共 %d 条信号", payload["weekId"], len(payload["signals"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
