"""为近期 HF/PwC 论文回填 PDF 全文证据与图表。"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, daily, feishu, paper_fulltext

log = logging.getLogger("paper-fulltext-backfill")
CN_TZ = timezone(timedelta(hours=8))
DEFAULT_SOURCES = {"hf-papers-trending", "papers-with-code-trending"}


def _json_cell(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(daily.scalar(value) or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _brief_record_ids(token: str, brief_date: str) -> set[str]:
    if not brief_date:
        return set()
    table_id = config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token)
    for record in feishu.read_all_records_with_ids(token, table_id):
        fields = record.get("fields") or {}
        if str(daily.scalar(fields.get("简报ID")) or "") != brief_date:
            continue
        try:
            values = json.loads(str(daily.scalar(fields.get("信号记录ID")) or "[]"))
        except (TypeError, ValueError):
            return set()
        return {str(value) for value in values} if isinstance(values, list) else set()
    return set()


def run(*, days: int = 7, force: bool = False, brief_date: str = "") -> int:
    token = feishu.get_tenant_access_token()
    records = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    cutoff = datetime.now(CN_TZ) - timedelta(days=max(1, days))
    brief_ids = _brief_record_ids(token, brief_date)
    updates: list[dict[str, Any]] = []
    attempted = 0
    succeeded = 0

    for record in records:
        fields = record.get("fields") or {}
        source_id = str(daily.scalar(fields.get("source_id")) or "")
        if brief_date:
            if str(record.get("record_id") or "") not in brief_ids:
                continue
            if daily.content_type(fields) != "论文":
                continue
        elif source_id not in DEFAULT_SOURCES:
            continue
        try:
            published = datetime.fromtimestamp(
                float(daily.scalar(fields.get("发布时间")) or 0) / 1000,
                CN_TZ,
            )
        except (TypeError, ValueError):
            continue
        if published < cutoff:
            continue
        metrics = _json_cell(fields.get("论文指标"))
        existing = metrics.get("full_text") or {}
        if (
            not force
            and isinstance(existing, dict)
            and existing.get("source") == "pdf"
            and int(existing.get("version") or 0) >= paper_fulltext.EVIDENCE_VERSION
        ):
            continue

        item: dict[str, Any] = {
            "url": daily.link(fields.get("链接")),
            "raw_content": str(daily.scalar(fields.get("原文")) or ""),
            "paper_metrics_json": metrics,
            "media_assets": daily.media_assets(fields.get("媒体资源")),
            "image_url": daily.link(fields.get("图片链接")),
        }
        attempted += 1
        ok = paper_fulltext.enrich_item(item)
        succeeded += int(ok)
        update_fields: dict[str, Any] = {
            "论文指标": json.dumps(item["paper_metrics_json"], ensure_ascii=False),
        }
        media = item.get("media_assets") or {}
        if media.get("images"):
            update_fields["媒体资源"] = json.dumps(media, ensure_ascii=False)
        if item.get("image_url"):
            update_fields["图片链接"] = {
                "link": str(item["image_url"]),
                "text": "论文图表",
            }
        updates.append({"record_id": record["record_id"], "fields": update_fields})
        log.info(
            "%s | %s | %s",
            "PDF" if ok else "摘要",
            source_id,
            daily.scalar(fields.get("标题")),
        )
        if len(updates) >= 10:
            feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)
            updates.clear()

    feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)
    log.info("回填完成：尝试 %d，PDF 成功 %d", attempted, succeeded)
    return succeeded


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="回填近期 HF/PwC 论文 PDF 全文证据")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--brief-date", default="")
    args = parser.parse_args()
    run(days=args.days, force=args.force, brief_date=args.brief_date)
