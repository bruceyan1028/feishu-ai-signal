"""纠正历史 Scrape 条目中被采集时间冒充的发布时间。"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config, feishu, process, scrape


def _scalar(value: Any) -> Any:
    return feishu._read_cell_key(value)


def _link(value: Any) -> str:
    if isinstance(value, list):
        return _link(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "")
    return str(value or "")


def _candidate(record: dict[str, Any]) -> bool:
    fields = record.get("fields") or {}
    if str(_scalar(fields.get("路由来源")) or "") != "Scrape":
        return False
    try:
        published = int(float(_scalar(fields.get("发布时间")) or 0))
        collected = int(float(_scalar(fields.get("采集时间")) or 0))
    except (TypeError, ValueError):
        return False
    # 旧逻辑在逐条清洗时调用 now()，通常与整轮采集时间相差数秒到数小时。
    return bool(published and collected and abs(published - collected) <= 3 * 3600000)


def _inspect(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    url = _link(fields.get("链接"))
    raw = scrape._fetch_direct_published_date(url) if url else ""
    corrected = process.parse_date_ms(raw)
    current = int(float(_scalar(fields.get("发布时间")) or 0))
    changed = corrected is not None and abs(corrected - current) >= 6 * 3600000
    return {
        "record_id": str(record.get("record_id") or ""),
        "title": str(_scalar(fields.get("标题")) or ""),
        "source": str(_scalar(fields.get("来源")) or ""),
        "url": url,
        "raw": raw,
        "current_ms": current,
        "corrected_ms": corrected,
        "changed": changed,
    }


def run() -> int:
    parser = argparse.ArgumentParser(description="回填 Scrape 条目的真实首发时间")
    parser.add_argument("--write", action="store_true", help="确认后写回飞书；默认仅审计")
    parser.add_argument("--limit", type=int, help="最多检查多少条")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    token = feishu.get_tenant_access_token()
    records = feishu.read_all_records_with_ids(
        token,
        config.FEISHU_ENTRY_TABLE_ID,
        ["标题", "链接", "来源", "路由来源", "发布时间", "采集时间"],
    )
    candidates = [record for record in records if _candidate(record)]
    if args.limit:
        candidates = candidates[: max(0, args.limit)]
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as pool:
        inspected = list(pool.map(_inspect, candidates))
    changed = [item for item in inspected if item["changed"]]

    if args.write and changed:
        feishu.batch_update_records(
            token,
            config.FEISHU_ENTRY_TABLE_ID,
            [
                {
                    "record_id": item["record_id"],
                    "fields": {"发布时间": item["corrected_ms"]},
                }
                for item in changed
            ],
        )

    print(
        json.dumps(
            {
                "candidates": len(candidates),
                "date_found": sum(item["corrected_ms"] is not None for item in inspected),
                "changed": len(changed),
                "written": len(changed) if args.write else 0,
                "changes": [
                    {
                        "record_id": item["record_id"],
                        "source": item["source"],
                        "title": item["title"],
                        "published": item["raw"],
                    }
                    for item in changed
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
