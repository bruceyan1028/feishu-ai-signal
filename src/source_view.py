"""把飞书一级参数记录转成「数据源」页面用的展示模型。

只导出用户判断一个源是否可信所需的字段。keyword_regex、min_content_chars、
dedup_key、extra_config 一律不出现在前端载荷里：站点是公开的，这些规则既泄露
筛选策略，对读者也没有可读性。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import sources

CN_TZ = timezone(timedelta(hours=8))
SEED_FILE = Path(__file__).resolve().parent / "seed_default.json"

STATUS_ACTIVE = "active"
STATUS_EXPERIMENTAL = "experimental"
STATUS_PAUSED = "paused"
STATUS_ORDER = (STATUS_ACTIVE, STATUS_EXPERIMENTAL, STATUS_PAUSED)
STATUS_LABELS = {
    STATUS_ACTIVE: "已接入",
    STATUS_EXPERIMENTAL: "待测",
    STATUS_PAUSED: "已暂停",
}
LABEL_TO_STATUS = {label: code for code, label in STATUS_LABELS.items()}

PRIORITY_TO_CN = {"P0": "高", "P1": "中", "P2": "低"}
CN_TO_PRIORITY = {label: code for code, label in PRIORITY_TO_CN.items()}
PRIORITY_ORDER = ("高", "中", "低")

# 本地配置服务支持的写操作；静态站点导出时为空，前端据此决定控件是否可点。
WRITABLE_CAPABILITIES = ("status", "priority", "create", "delete")


def normalize_status(raw: Any) -> str:
    text = str(sources.cell(raw) or "").strip().lower()
    if text in STATUS_LABELS:
        return text
    if text in LABEL_TO_STATUS:
        return LABEL_TO_STATUS[text]
    # 信号源表用中文状态，两张表允许短暂漂移，这里统一归一
    if text in {"已接入", "启用", "on"}:
        return STATUS_ACTIVE
    if text in {"待测", "测试中"}:
        return STATUS_EXPERIMENTAL
    return STATUS_PAUSED


def normalize_priority(raw: Any) -> str:
    text = str(sources.cell(raw) or "").strip().upper()
    if text in PRIORITY_TO_CN:
        return PRIORITY_TO_CN[text]
    if text in CN_TO_PRIORITY:
        return text
    return "中"


def _format_stamp(raw: Any) -> str:
    try:
        stamp = int(float(sources.cell(raw) or 0))
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp / 1000, CN_TZ).strftime("%m-%d %H:%M")


def _int_cell(raw: Any) -> int:
    try:
        return int(float(sources.cell(raw) or 0))
    except (TypeError, ValueError):
        return 0


def brief_counts(briefs: list[dict[str, Any]] | None) -> dict[str, int]:
    """统计每个源在近几期简报里入选了多少条。

    这和参数表回写的「条目数」是两个口径：后者是采集入库量，这里是真正被选进
    简报的量。一个源天天入库却从不入选，是用户最该看到的信号。
    """
    counts: dict[str, int] = {}
    for brief in briefs or []:
        for signal in brief.get("signals") or []:
            source_id = str(signal.get("sourceId") or "").strip()
            if source_id:
                counts[source_id] = counts.get(source_id, 0) + 1
    return counts


def build_source(record: dict[str, Any], *, brief_count: int = 0) -> dict[str, Any]:
    fields = record.get("fields") or {}
    source_id = str(sources.cell(fields.get("source_id")) or "").strip()
    status = normalize_status(fields.get("status"))
    return {
        "id": source_id,
        "recordId": str(record.get("record_id") or ""),
        "name": str(sources.cell(fields.get("name")) or source_id),
        "url": str(sources.cell(fields.get("endpoint")) or ""),
        "status": status,
        "statusLabel": STATUS_LABELS[status],
        "on": status == STATUS_ACTIVE,
        "format": sources.normalize_signal_format(fields.get("来源类型"))
        or sources.SIGNAL_FORMAT_OTHER,
        "type": str(sources.cell(fields.get("dimension")) or "其他"),
        "tier": str(sources.cell(fields.get("tier")) or ""),
        "priority": normalize_priority(fields.get("priority")),
        "fetchMethod": str(sources.cell(fields.get("fetch_method")) or ""),
        "lookback": str(sources.cell(fields.get("lookback_window")) or ""),
        "last": _format_stamp(fields.get("最近采集时间")),
        "perDay": _int_cell(fields.get("条目数")),
        "briefCount": int(brief_count),
    }


def _ordered_values(items: list[str], preferred: tuple[str, ...] = ()) -> list[str]:
    seen: list[str] = [value for value in preferred if value in items]
    for value in items:
        if value and value not in seen:
            seen.append(value)
    return seen


def build_meta(source_list: list[dict[str, Any]], *, writable: bool = False) -> dict[str, Any]:
    return {
        "types": _ordered_values(sorted({s["type"] for s in source_list if s["type"]})),
        "formats": _ordered_values(sorted({s["format"] for s in source_list if s["format"]})),
        "priorities": list(PRIORITY_ORDER),
        "statuses": [
            {"code": code, "label": STATUS_LABELS[code]} for code in STATUS_ORDER
        ],
        "writable": list(WRITABLE_CAPABILITIES) if writable else [],
    }


def records_from_seed(seed_file: Path = SEED_FILE) -> list[dict[str, Any]]:
    """把仓库里的配置快照包成飞书记录形状。

    只用于本机没有飞书凭据时的预览：seed 由 export_seed 导出，运行时统计
    （最近采集时间、条目数）不在里面，所以那两列会是空的。
    """
    bundle = json.loads(seed_file.read_text(encoding="utf-8"))
    return [{"record_id": "", "fields": row} for row in bundle.get("一级参数") or []]


def build_payload(
    records: list[dict[str, Any]],
    *,
    briefs: list[dict[str, Any]] | None = None,
    writable: bool = False,
    origin: str = "feishu",
) -> dict[str, Any]:
    counts = brief_counts(briefs)
    source_list = [
        build_source(record, brief_count=counts.get(
            str(sources.cell((record.get("fields") or {}).get("source_id")) or "").strip(), 0
        ))
        for record in records
    ]
    source_list = [s for s in source_list if s["id"] or s["name"]]
    source_list.sort(
        key=lambda s: (
            STATUS_ORDER.index(s["status"]),
            PRIORITY_ORDER.index(s["priority"]),
            s["name"],
        )
    )
    return {
        "generatedAt": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        "origin": origin,
        "briefWindowDays": len(briefs or []),
        "sources": source_list,
        "meta": build_meta(source_list, writable=writable),
    }


def _local_briefs(data_dir: Path, limit: int = 7) -> list[dict[str, Any]]:
    briefs: list[dict[str, Any]] = []
    for path in sorted(data_dir.glob("brief-20*.json"), reverse=True)[:limit]:
        try:
            briefs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return briefs


def run() -> int:
    """本地生成 site/data/sources.json；没有飞书凭据时退回仓库里的配置快照。"""
    import argparse

    from . import feishu

    parser = argparse.ArgumentParser(description="导出数据源页面用的 sources.json")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "site" / "data" / "sources.json"))
    parser.add_argument("--seed", action="store_true", help="跳过飞书，直接用 seed_default.json")
    args = parser.parse_args()
    out = Path(args.out)
    origin = "feishu"
    if args.seed:
        records, origin = records_from_seed(), "seed"
    else:
        try:
            records = feishu.read_param_records(feishu.get_tenant_access_token())
        except Exception as exc:  # noqa: BLE001 - 本地预览允许降级，但要说清楚
            print(f"读取飞书失败（{exc}），改用仓库配置快照，采集统计列会是空的")
            records, origin = records_from_seed(), "seed"
    payload = build_payload(records, briefs=_local_briefs(out.parent), origin=origin)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out} · {len(payload['sources'])} 个源 · 来源 {origin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
