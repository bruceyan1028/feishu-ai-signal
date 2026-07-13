"""将飞书中的真实简报生成为 GitHub Pages 静态站。"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, daily, feishu

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "index.html"


def _json_cell(value: Any, fallback: Any) -> Any:
    raw = daily.scalar(value)
    if not raw:
        return fallback
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return fallback


def _date_from_ms(value: Any) -> str:
    try:
        stamp = int(float(daily.scalar(value)))
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(stamp / 1000, CN_TZ).strftime("%Y-%m-%d")


def _signal_from_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    urgency = daily.URGENCY_TO_CN.get(str(daily.scalar(fields.get("紧迫度"))), "中")
    published = _date_from_ms(fields.get("发布时间"))
    return {
        "recordId": str(record.get("record_id") or ""),
        "title": str(daily.scalar(fields.get("标题")) or ""),
        "titleCn": str(daily.scalar(fields.get("中文标题")) or daily.scalar(fields.get("标题")) or ""),
        "source": str(daily.scalar(fields.get("来源")) or ""),
        "url": daily.link(fields.get("链接")),
        "category": str(daily.scalar(fields.get("分类")) or "其他"),
        "contentType": daily.content_type(fields),
        "publishedDate": published,
        "summary": str(daily.scalar(fields.get("中文摘要")) or ""),
        "why": str(daily.scalar(fields.get("为何重要")) or ""),
        "impact": int(float(daily.scalar(fields.get("影响分")) or 0)),
        "novelty": int(float(daily.scalar(fields.get("新颖度")) or 0)),
        "actionability": int(float(daily.scalar(fields.get("可行动性")) or 0)),
        "urgency": urgency,
        "tags": [str(daily.scalar(item)) for item in fields.get("主题") or []],
        "imageUrl": daily.link(fields.get("图片链接")),
        "mediaAssets": daily.media_assets(fields.get("媒体资源")),
    }


def load_recent_briefs(token: str, days: int = 7) -> list[dict[str, Any]]:
    table_id = config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token)
    brief_records = feishu.read_all_records_with_ids(token, table_id)
    entry_records = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    entries = {str(record.get("record_id")): _signal_from_record(record) for record in entry_records}
    briefs: list[dict[str, Any]] = []
    for record in brief_records:
        fields = record.get("fields") or {}
        if str(daily.scalar(fields.get("状态"))) != "已发布":
            continue
        date = str(daily.scalar(fields.get("简报ID")) or _date_from_ms(fields.get("简报日期")))
        signal_ids = [str(item) for item in _json_cell(fields.get("信号记录ID"), [])]
        signals = [entries[record_id] for record_id in signal_ids if record_id in entries]
        if not date or not signals:
            continue
        briefs.append(
            {
                "date": date,
                "title": str(daily.scalar(fields.get("简报标题")) or f"AI Signal 每日情报 · {date}"),
                "intro": str(daily.scalar(fields.get("导语")) or ""),
                "bullets": _json_cell(fields.get("关键要点"), []),
                "signals": signals,
                "briefRecordId": str(record.get("record_id") or ""),
                "briefTableId": table_id,
            }
        )
    briefs.sort(key=lambda item: item["date"], reverse=True)
    return briefs[:days]


def build_site(briefs: list[dict[str, Any]], site_dir: Path | str = ROOT / "site") -> Path:
    if not briefs:
        raise RuntimeError("没有可发布的已发布简报")
    site = Path(site_dir)
    if site.exists():
        shutil.rmtree(site)
    data_dir = site / "data"
    data_dir.mkdir(parents=True)
    shutil.copy2(TEMPLATE, site / "index.html")
    for brief in briefs:
        content = json.dumps(brief, ensure_ascii=False, indent=2)
        (data_dir / f'brief-{brief["date"]}.json').write_text(content, encoding="utf-8")
    (data_dir / "brief-latest.json").write_text(
        json.dumps(briefs[0], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (site / ".nojekyll").write_text("", encoding="utf-8")
    return site


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="优先加入本次生成的简报 JSON")
    parser.add_argument("--site-dir", default=str(ROOT / "site"))
    args = parser.parse_args()
    token = feishu.get_tenant_access_token()
    briefs = load_recent_briefs(token)
    if args.input:
        current = json.loads(Path(args.input).read_text(encoding="utf-8"))
        briefs = [current, *[item for item in briefs if item["date"] != current["date"]]][:7]
    site = build_site(briefs, args.site_dir)
    print(site)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
