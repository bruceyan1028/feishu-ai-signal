"""发送飞书每日情报或 AI 周报卡片，并回写发送状态。"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import config, daily, feishu, publish

log = logging.getLogger(__name__)


def detail_url(base_url: str, day: str) -> str:
    return f"{base_url.rstrip('/')}/?{urlencode({'date': day})}"


def weekly_detail_url(base_url: str, week_id: str) -> str:
    query = urlencode({"page": "tasks", "tab": "report", "week": week_id})
    return f"{base_url.rstrip('/')}/?{query}"


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


def build_weekly_card(brief: dict[str, Any], url: str) -> dict[str, Any]:
    metric_text = " · ".join(
        f"{item.get('label')} {item.get('value')}" for item in brief.get("metrics") or []
    )
    by_id = {
        str(signal.get("recordId") or ""): signal
        for signal in brief.get("signals") or []
    }
    top = [
        by_id[record_id]
        for record_id in brief.get("topSignals") or []
        if record_id in by_id
    ][:3]
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{brief.get('headline', '')}**\n\n{brief.get('thesis', '')}\n\n"
                    f"{metric_text}"
                ).strip(),
            },
        },
        {"tag": "hr"},
    ]
    for index, signal in enumerate(top, 1):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{index}. {signal.get('titleCn') or signal.get('title', '')}**\n"
                        f"{signal.get('source', '')} · 影响分 {signal.get('impact', 0)}\n"
                        f"{signal.get('summary', '')}"
                    ),
                },
            }
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "查看完整 AI 周报"},
                    "url": url,
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": str(brief.get("title") or "AI Signal 自动周报"),
            },
        },
        "elements": elements,
    }


def _brief_record(token: str, table_id: str, day: str) -> dict[str, Any] | None:
    for record in feishu.read_all_records_with_ids(token, table_id):
        if str(daily.scalar((record.get("fields") or {}).get("简报ID"))) == day:
            return record
    return None


def recipient_statuses(
    recipients: list[str],
    message_ids: dict[str, str],
    failures: dict[str, str],
) -> dict[str, str]:
    """以人名而非 open_id 输出逐人发送状态。"""
    statuses: dict[str, str] = {}
    for index, open_id in enumerate(recipients, 1):
        name = config.FEISHU_RECIPIENT_NAME_BY_OPEN_ID.get(open_id) or f"recipient_{index}"
        statuses[name] = "success" if open_id in message_ids else "failed"
    return statuses


def build_delivery_report_card(day: str, statuses: dict[str, str]) -> dict[str, Any]:
    lines = [
        f"{'✅' if status == 'success' else '❌'} {name}："
        f"{'发送成功' if status == 'success' else '发送失败'}"
        for name, status in statuses.items()
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if all(status == "success" for status in statuses.values()) else "orange",
            "title": {"tag": "plain_text", "content": f"每日简报发送结果 · {day}"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "“发送成功”表示飞书接口已接受消息，不代表对方已读。",
                    }
                ],
            },
        ],
    }


def send_delivery_report(
    token: str,
    day: str,
    recipients: list[str],
    message_ids: dict[str, str],
    failures: dict[str, str],
) -> tuple[dict[str, str], str]:
    statuses = recipient_statuses(recipients, message_ids, failures)
    target = config.FEISHU_DELIVERY_REPORT_OPEN_ID
    if not target:
        return statuses, ""
    message_id = feishu.send_interactive_message(
        token,
        target,
        build_delivery_report_card(day, statuses),
    )
    return statuses, message_id


def send_many(
    brief: dict[str, Any], base_url: str, open_ids: list[str], force: bool = False
) -> dict[str, Any]:
    if not base_url:
        raise config.ConfigError("缺少 PUBLIC_BASE_URL")
    recipients = list(dict.fromkeys(item.strip() for item in open_ids if item.strip()))
    if not recipients:
        raise config.ConfigError("缺少 FEISHU_RECIPIENT_OPEN_IDS")
    token = feishu.get_tenant_access_token()
    table_id = str(brief.get("briefTableId") or config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token))
    record = _brief_record(token, table_id, str(brief["date"]))
    if not record:
        raise RuntimeError(f'多维表中不存在 {brief["date"]} 的简报')
    fields = record.get("fields") or {}
    if str(daily.scalar(fields.get("发送状态"))) == "已发送" and not force:
        return {
            "skipped": True,
            "messageIds": str(daily.scalar(fields.get("消息ID")) or ""),
            "detailUrl": detail_url(base_url, str(brief["date"])),
        }
    url = detail_url(base_url, str(brief["date"]))
    message_ids: dict[str, str] = {}
    failures: dict[str, str] = {}
    try:
        card = build_card(brief, url)
    except Exception:
        feishu.update_record(token, table_id, str(record["record_id"]), {"发送状态": "失败"})
        raise
    # 逐人发送互不牵连：一个 open_id 失效不该让其他收件人也收不到
    for open_id in recipients:
        try:
            message_ids[open_id] = feishu.send_interactive_message(token, open_id, card)
        except Exception as exc:  # noqa: BLE001
            failures[open_id] = str(exc)
            name = config.FEISHU_RECIPIENT_NAME_BY_OPEN_ID.get(open_id) or "未命名收件人"
            log.warning("发送给 %s 失败: %s", name, exc)
    if not message_ids:
        feishu.update_record(token, table_id, str(record["record_id"]), {"发送状态": "失败"})
        try:
            send_delivery_report(token, str(brief["date"]), recipients, message_ids, failures)
        except Exception as exc:  # noqa: BLE001
            log.warning("发送每日结果汇总失败: %s", exc)
        raise RuntimeError(f"所有收件人都发送失败: {json.dumps(failures, ensure_ascii=False)}")
    feishu.update_record(
        token,
        table_id,
        str(record["record_id"]),
        {
            "发送状态": "已发送",
            "发送时间": int(datetime.now(timezone.utc).timestamp() * 1000),
            "消息ID": json.dumps(message_ids, ensure_ascii=False),
        },
    )
    report_message_id = ""
    try:
        statuses, report_message_id = send_delivery_report(
            token,
            str(brief["date"]),
            recipients,
            message_ids,
            failures,
        )
    except Exception as exc:  # noqa: BLE001
        statuses = recipient_statuses(recipients, message_ids, failures)
        log.warning("发送每日结果汇总失败: %s", exc)
    return {
        "skipped": False,
        "messageIds": message_ids,
        "failed": failures,
        "recipientStatuses": statuses,
        "deliveryReportMessageId": report_message_id,
        "detailUrl": url,
    }


def send_weekly_many(
    brief: dict[str, Any], base_url: str, open_ids: list[str], force: bool = False
) -> dict[str, Any]:
    if not base_url:
        raise config.ConfigError("缺少 PUBLIC_BASE_URL")
    recipients = list(dict.fromkeys(item.strip() for item in open_ids if item.strip()))
    if not recipients:
        raise config.ConfigError("缺少 FEISHU_RECIPIENT_OPEN_IDS")
    token = feishu.get_tenant_access_token()
    table_id = str(
        brief.get("weeklyTableId")
        or config.FEISHU_WEEKLY_TABLE_ID
        or feishu.ensure_weekly_report_table(token)
    )
    week_id = str(brief["weekId"])
    record = next(
        (
            item
            for item in feishu.read_all_records_with_ids(token, table_id)
            if str(daily.scalar((item.get("fields") or {}).get("周报ID"))) == week_id
        ),
        None,
    )
    if not record:
        raise RuntimeError(f"多维表中不存在 {week_id} 的周报")
    fields = record.get("fields") or {}
    url = weekly_detail_url(base_url, week_id)
    if str(daily.scalar(fields.get("发送状态"))) == "已发送" and not force:
        return {
            "skipped": True,
            "messageIds": str(daily.scalar(fields.get("消息ID")) or ""),
            "detailUrl": url,
        }
    card = build_weekly_card(brief, url)
    message_ids: dict[str, str] = {}
    failures: dict[str, str] = {}
    for open_id in recipients:
        try:
            message_ids[open_id] = feishu.send_interactive_message(token, open_id, card)
        except Exception as exc:  # noqa: BLE001
            failures[open_id] = str(exc)
            log.warning("周报发送给 %s 失败: %s", open_id, exc)
    if not message_ids:
        feishu.update_record(
            token, table_id, str(record["record_id"]), {"发送状态": "失败"}
        )
        raise RuntimeError(
            f"所有收件人都发送失败: {json.dumps(failures, ensure_ascii=False)}"
        )
    feishu.update_record(
        token,
        table_id,
        str(record["record_id"]),
        {
            "发送状态": "已发送",
            "发送时间": int(datetime.now(timezone.utc).timestamp() * 1000),
            "消息ID": json.dumps(message_ids, ensure_ascii=False),
        },
    )
    return {
        "skipped": False,
        "messageIds": message_ids,
        "failed": failures,
        "detailUrl": url,
    }


def send(brief: dict[str, Any], base_url: str, open_id: str, force: bool = False) -> dict[str, Any]:
    """向单个接收人发送，保留原有调用兼容性。"""
    result = send_many(brief, base_url, [open_id], force)
    message_ids = result.pop("messageIds", {})
    if isinstance(message_ids, dict):
        result["messageId"] = message_ids.get(open_id, "")
    else:
        result["messageId"] = message_ids
    return result


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="本次生成的简报 JSON")
    parser.add_argument("--date", help="未提供 input 时发送指定日期，默认最新")
    parser.add_argument("--base-url", default=config.PUBLIC_BASE_URL)
    parser.add_argument("--open-id", default=config.FEISHU_RECIPIENT_OPEN_ID)
    parser.add_argument("--open-ids", default=",".join(config.FEISHU_RECIPIENT_OPEN_IDS))
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
    recipients = [item.strip() for item in args.open_ids.split(",") if item.strip()]
    if not recipients and args.open_id:
        recipients = [args.open_id]
    sender = send_weekly_many if brief.get("weekId") else send_many
    print(json.dumps(sender(brief, args.base_url, recipients, args.force), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
