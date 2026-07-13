"""发送飞书每日情报消息卡片，并回写发送状态。"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import config, daily, feishu, publish


def detail_url(base_url: str, day: str) -> str:
    return f"{base_url.rstrip('/')}/?{urlencode({'date': day})}"


def build_card(brief: dict[str, Any], url: str) -> dict[str, Any]:
    bullets = "\n".join(f"• {item.get('text', '')}" for item in brief.get("bullets") or [])
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{brief.get('intro', '')}\n\n{bullets}".strip(),
            },
        },
        {"tag": "hr"},
    ]
    for index, signal in enumerate((brief.get("signals") or [])[:3], 1):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{index}. {signal.get('titleCn') or signal.get('title', '')}**\n"
                        f"{signal.get('source', '')} · 影响分 {signal.get('impact', 0)} · "
                        f"紧迫度 {signal.get('urgency', '中')}\n{signal.get('summary', '')}"
                    ),
                },
            }
        )
    elements.extend(
        [
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "查看完整网页简报"},
                        "url": url,
                    }
                ],
            },
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "内容来自真实 RSS，并已写入飞书多维表格"}],
            },
        ]
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": str(brief.get("title") or "AI Signal 每日情报")},
        },
        "elements": elements,
    }


def _brief_record(token: str, table_id: str, day: str) -> dict[str, Any] | None:
    for record in feishu.read_all_records_with_ids(token, table_id):
        if str(daily.scalar((record.get("fields") or {}).get("简报ID"))) == day:
            return record
    return None


def send(brief: dict[str, Any], base_url: str, open_id: str, force: bool = False) -> dict[str, Any]:
    if not base_url:
        raise config.ConfigError("缺少 PUBLIC_BASE_URL")
    if not open_id:
        raise config.ConfigError("缺少 FEISHU_RECIPIENT_OPEN_ID")
    token = feishu.get_tenant_access_token()
    table_id = str(brief.get("briefTableId") or config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token))
    record = _brief_record(token, table_id, str(brief["date"]))
    if not record:
        raise RuntimeError(f'多维表中不存在 {brief["date"]} 的简报')
    fields = record.get("fields") or {}
    if str(daily.scalar(fields.get("发送状态"))) == "已发送" and not force:
        return {
            "skipped": True,
            "messageId": str(daily.scalar(fields.get("消息ID")) or ""),
            "detailUrl": detail_url(base_url, str(brief["date"])),
        }
    url = detail_url(base_url, str(brief["date"]))
    try:
        message_id = feishu.send_interactive_message(token, open_id, build_card(brief, url))
        feishu.update_record(
            token,
            table_id,
            str(record["record_id"]),
            {"发送状态": "已发送", "发送时间": int(datetime.now(timezone.utc).timestamp() * 1000), "消息ID": message_id},
        )
    except Exception:
        feishu.update_record(token, table_id, str(record["record_id"]), {"发送状态": "失败"})
        raise
    return {"skipped": False, "messageId": message_id, "detailUrl": url}


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="本次生成的简报 JSON")
    parser.add_argument("--date", help="未提供 input 时发送指定日期，默认最新")
    parser.add_argument("--base-url", default=config.PUBLIC_BASE_URL)
    parser.add_argument("--open-id", default=config.FEISHU_RECIPIENT_OPEN_ID)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.input:
        brief = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        token = feishu.get_tenant_access_token()
        briefs = publish.load_recent_briefs(token)
        brief = next((item for item in briefs if not args.date or item["date"] == args.date), None)
        if not brief:
            raise RuntimeError("没有找到可发送的已发布简报")
    print(json.dumps(send(brief, args.base_url, args.open_id, args.force), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
