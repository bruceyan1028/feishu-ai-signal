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

# 卡片只当目录：板块与条目都设上限，超出的靠网页简报承载。
MAX_GROUPS = 4
MAX_ITEMS_PER_GROUP = 3
MAX_CARD_ITEMS = 8

CATEGORY_ICONS = {
    "前沿模型公司": "🚀",
    "技术研究开源": "🧪",
    "算力芯片云": "⚡",
    "政策监管地缘": "🏛️",
    "模型评测基准": "📊",
    "产品化企业采用": "🏢",
    "创业融资并购": "💰",
    "中文科技媒体": "📰",
    "中文综合媒体": "📰",
}

# 板块色块：取飞书官方颜色枚举，100 级作底、基础色作字，深色主题下由客户端自动换算。
CATEGORY_THEMES = {
    "前沿模型公司": ("blue-100", "blue"),
    "技术研究开源": ("turquoise-100", "turquoise"),
    "算力芯片云": ("orange-100", "orange"),
    "政策监管地缘": ("red-100", "red"),
    "模型评测基准": ("purple-100", "purple"),
    "产品化企业采用": ("indigo-100", "indigo"),
    "创业融资并购": ("lime-100", "lime"),
    "中文科技媒体": ("wathet-100", "wathet"),
    "中文综合媒体": ("grey-100", "grey"),
}
DEFAULT_THEME = ("grey-100", "grey")


def detail_url(base_url: str, day: str) -> str:
    return f"{base_url.rstrip('/')}/?{urlencode({'date': day})}"


def weekly_detail_url(base_url: str, week_id: str) -> str:
    query = urlencode({"page": "tasks", "tab": "report", "week": week_id})
    return f"{base_url.rstrip('/')}/?{query}"


def _short(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip("，。、；：,. ") + "…"


def group_signals(signals: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """按分类聚合，保留原有排序（已按质量与影响分排过）。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        name = str(signal.get("category") or "其他").strip() or "其他"
        grouped.setdefault(name, []).append(signal)
    ordered = sorted(grouped.items(), key=lambda item: -len(item[1]))[:MAX_GROUPS]
    result: list[tuple[str, list[dict[str, Any]]]] = []
    used = 0
    for name, items in ordered:
        if used >= MAX_CARD_ITEMS:
            break
        take = items[: min(MAX_ITEMS_PER_GROUP, MAX_CARD_ITEMS - used)]
        used += len(take)
        result.append((name, take))
    return result


def _band(background: str, font: str, text: str, *, size: str) -> dict[str, Any]:
    """整行色块 + 居中文字，用来切分标题和各板块。"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": background,
        "horizontal_spacing": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "elements": [
                    {
                        "tag": "markdown",
                        "text_align": "center",
                        "text_size": size,
                        "content": f"<font color='{font}'>**{text}**</font>",
                    }
                ],
            }
        ],
    }


def _signal_blocks(signal: dict[str, Any]) -> list[dict[str, Any]]:
    """标题可点进原文，来源与形态降到辅助字号的灰字。"""
    title = _short(signal.get("titleCn") or signal.get("title"), 34)
    link = str(signal.get("url") or "").strip()
    meta = " · ".join(
        part
        for part in (
            str(signal.get("source") or "").strip(),
            str(signal.get("contentType") or "").strip(),
        )
        if part
    )
    return [
        {
            "tag": "markdown",
            "content": f"**[{title}]({link})**" if link.startswith("http") else f"**{title}**",
        },
        {
            "tag": "markdown",
            "text_size": "notation",
            "content": f"<font color='grey'>{meta}</font>",
        },
    ]


def build_card(brief: dict[str, Any], url: str) -> dict[str, Any]:
    """纯文字目录卡：分板块的标题清单，导语、摘要与解读都交给网页。"""
    signals = [item for item in (brief.get("signals") or []) if isinstance(item, dict)]
    groups = group_signals(signals)
    shown = sum(len(items) for _, items in groups)

    title = str(brief.get("title") or "AI Signal 每日情报")
    elements: list[dict[str, Any]] = [_band("red-100", "red", title, size="heading")]
    for name, items in groups:
        background, font = CATEGORY_THEMES.get(name, DEFAULT_THEME)
        icon = CATEGORY_ICONS.get(name, "🔎")
        elements.append(_band(background, font, f"{icon} {name}", size="normal"))
        for signal in items:
            elements.extend(_signal_blocks(signal))

    elements.extend(
        [
            {"tag": "hr"},
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
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"今日入选 {len(signals)} 条，卡片列出 {shown} 条",
                    }
                ],
            },
        ]
    )
    return {"config": {"wide_screen_mode": True}, "elements": elements}


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


def _unique_ids(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in (values or []) if item.strip()))


def resolve_targets(
    open_ids: list[str] | None = None,
    chat_ids: list[str] | None = None,
) -> list[tuple[str, str]]:
    """群聊优先：有 chat_id 就只发群，不再逐人私聊。"""
    chats = _unique_ids(chat_ids)
    if chats:
        return [("chat_id", chat_id) for chat_id in chats]
    return [("open_id", open_id) for open_id in _unique_ids(open_ids)]


def recipient_label(receive_id: str, receive_id_type: str, index: int) -> str:
    if receive_id_type == "chat_id":
        return config.FEISHU_RECIPIENT_CHAT_NAME_BY_ID.get(receive_id) or f"chat_{index}"
    return config.FEISHU_RECIPIENT_NAME_BY_OPEN_ID.get(receive_id) or f"recipient_{index}"


def recipient_statuses(
    targets: list[tuple[str, str]],
    message_ids: dict[str, str],
    failures: dict[str, str],
) -> dict[str, str]:
    """以名称而非 id 输出发送状态。"""
    statuses: dict[str, str] = {}
    for index, (receive_id_type, receive_id) in enumerate(targets, 1):
        name = recipient_label(receive_id, receive_id_type, index)
        statuses[name] = "success" if receive_id in message_ids else "failed"
    return statuses


def _send_card_to_targets(
    token: str, card: dict[str, Any], targets: list[tuple[str, str]]
) -> tuple[dict[str, str], dict[str, str]]:
    message_ids: dict[str, str] = {}
    failures: dict[str, str] = {}
    for index, (receive_id_type, receive_id) in enumerate(targets, 1):
        try:
            message_ids[receive_id] = feishu.send_interactive_message(
                token, receive_id, card, receive_id_type=receive_id_type
            )
        except Exception as exc:  # noqa: BLE001
            failures[receive_id] = str(exc)
            log.warning(
                "发送给 %s 失败: %s",
                recipient_label(receive_id, receive_id_type, index),
                exc,
            )
    return message_ids, failures


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
    targets: list[tuple[str, str]],
    message_ids: dict[str, str],
    failures: dict[str, str],
) -> tuple[dict[str, str], str]:
    statuses = recipient_statuses(targets, message_ids, failures)
    report_to = config.FEISHU_DELIVERY_REPORT_OPEN_ID
    if not report_to:
        return statuses, ""
    message_id = feishu.send_interactive_message(
        token,
        report_to,
        build_delivery_report_card(day, statuses),
        receive_id_type="open_id",
    )
    return statuses, message_id


def send_many(
    brief: dict[str, Any],
    base_url: str,
    open_ids: list[str] | None = None,
    force: bool = False,
    chat_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not base_url:
        raise config.ConfigError("缺少 PUBLIC_BASE_URL")
    targets = resolve_targets(open_ids, chat_ids)
    if not targets:
        raise config.ConfigError("缺少 FEISHU_RECIPIENT_CHAT_IDS 或 FEISHU_RECIPIENT_OPEN_IDS")
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
    try:
        card = build_card(brief, url)
    except Exception:
        feishu.update_record(token, table_id, str(record["record_id"]), {"发送状态": "失败"})
        raise
    # 多个目标互不牵连：一个 chat_id / open_id 失效不该让其它接收方也收不到
    message_ids, failures = _send_card_to_targets(token, card, targets)
    if not message_ids:
        feishu.update_record(token, table_id, str(record["record_id"]), {"发送状态": "失败"})
        try:
            send_delivery_report(token, str(brief["date"]), targets, message_ids, failures)
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
            targets,
            message_ids,
            failures,
        )
    except Exception as exc:  # noqa: BLE001
        statuses = recipient_statuses(targets, message_ids, failures)
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
    brief: dict[str, Any],
    base_url: str,
    open_ids: list[str] | None = None,
    force: bool = False,
    chat_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not base_url:
        raise config.ConfigError("缺少 PUBLIC_BASE_URL")
    targets = resolve_targets(open_ids, chat_ids)
    if not targets:
        raise config.ConfigError("缺少 FEISHU_RECIPIENT_CHAT_IDS 或 FEISHU_RECIPIENT_OPEN_IDS")
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
    message_ids, failures = _send_card_to_targets(token, card, targets)
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
    parser.add_argument("--chat-ids", default=",".join(config.FEISHU_RECIPIENT_CHAT_IDS))
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
    chat_ids = [item.strip() for item in args.chat_ids.split(",") if item.strip()]
    sender = send_weekly_many if brief.get("weekId") else send_many
    print(
        json.dumps(
            sender(brief, args.base_url, recipients, args.force, chat_ids=chat_ids),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
