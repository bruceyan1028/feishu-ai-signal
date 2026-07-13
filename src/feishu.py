"""飞书多维表交互：获取 token、读配置表、读去重键、批量写记录。

对应 n8n 节点：Get Feishu Token / Read Param Records / Dedup Against Feishu / Create Feishu Record
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)

_SESSION = requests.Session()


class FeishuError(RuntimeError):
    pass


def get_tenant_access_token() -> str:
    """对应 Get Feishu Token 节点。"""
    resp = _SESSION.post(
        f"{config.FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        headers={"Content-Type": "application/json"},
        json={"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"Feishu token failed: {data.get('code')} {data.get('msg')}")
    return data["tenant_access_token"]


def read_param_records(token: str) -> list[dict[str, Any]]:
    """对应 Read Param Records 节点：读取「源配置表」全部记录。"""
    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{config.FEISHU_PARAM_TABLE_ID}/records?page_size=500"
    )
    resp = _SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(
            f"Feishu list records failed: {data.get('code')} {data.get('msg')}"
        )
    return (data.get("data") or {}).get("items") or []


def _read_cell_key(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        x = value[0]
        if isinstance(x, dict):
            return x.get("text")
        return x
    if isinstance(value, dict):
        return value.get("text")
    return str(value)


def read_existing_dedup_keys(token: str) -> set[str]:
    """对应 Dedup Against Feishu 节点：分页读回「信号条目表」已存的「去重键」。"""
    field_param = requests.utils.quote(json.dumps(["去重键"], ensure_ascii=False))
    existing: set[str] = set()
    page_token = ""
    guard = 0
    while guard < 50:
        url = (
            f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
            f"/tables/{config.FEISHU_ENTRY_TABLE_ID}/records"
            f"?page_size=500&field_names={field_param}"
        )
        if page_token:
            url += f"&page_token={requests.utils.quote(page_token)}"
        resp = _SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(
                f"Feishu read existing keys failed: {data.get('code')} {data.get('msg')}"
            )
        payload = data.get("data") or {}
        for rec in payload.get("items") or []:
            key = _read_cell_key((rec.get("fields") or {}).get("去重键"))
            if key:
                existing.add(str(key).strip())
        page_token = payload.get("page_token", "") if payload.get("has_more") else ""
        guard += 1
        if not page_token:
            break
    return existing


def read_all_records(
    token: str, table_id: str, field_names: list[str] | None = None
) -> list[dict[str, Any]]:
    """分页读取指定表的全部记录，返回 fields 列表。"""
    base = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{table_id}/records?page_size=500"
    )
    if field_names:
        base += f"&field_names={requests.utils.quote(json.dumps(field_names, ensure_ascii=False))}"
    out: list[dict[str, Any]] = []
    page_token = ""
    guard = 0
    while guard < 100:
        url = base + (f"&page_token={requests.utils.quote(page_token)}" if page_token else "")
        resp = _SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(
                f"Feishu read records failed: {data.get('code')} {data.get('msg')}"
            )
        payload = data.get("data") or {}
        for rec in payload.get("items") or []:
            out.append(rec.get("fields") or {})
        page_token = payload.get("page_token", "") if payload.get("has_more") else ""
        guard += 1
        if not page_token:
            break
    return out


def read_all_records_with_ids(
    token: str, table_id: str, field_names: list[str] | None = None
) -> list[dict[str, Any]]:
    """分页读取记录并保留 record_id，供分析回写和简报引用使用。"""
    base = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{table_id}/records?page_size=500"
    )
    if field_names:
        base += f"&field_names={requests.utils.quote(json.dumps(field_names, ensure_ascii=False))}"
    out: list[dict[str, Any]] = []
    page_token = ""
    while True:
        url = base + (f"&page_token={requests.utils.quote(page_token)}" if page_token else "")
        resp = _SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(f"Feishu read records failed: {data.get('code')} {data.get('msg')}")
        payload = data.get("data") or {}
        out.extend(
            {"record_id": rec.get("record_id"), "fields": rec.get("fields") or {}}
            for rec in payload.get("items") or []
        )
        page_token = payload.get("page_token", "") if payload.get("has_more") else ""
        if not page_token:
            return out


def batch_update_records(
    token: str, table_id: str, records: list[dict[str, Any]], chunk: int = 500
) -> int:
    """批量更新任意数据表记录。"""
    if not records:
        return 0
    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{table_id}/records/batch_update"
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    updated = 0
    for i in range(0, len(records), chunk):
        batch = records[i : i + chunk]
        resp = _SESSION.post(url, headers=headers, json={"records": batch}, timeout=60)
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(f"Feishu batch_update failed: {data.get('code')} {data.get('msg')}")
        updated += len(batch)
    return updated


def list_tables(token: str) -> list[dict[str, Any]]:
    url = f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}/tables?page_size=100"
    resp = _SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"Feishu list tables failed: {data.get('code')} {data.get('msg')}")
    return (data.get("data") or {}).get("items") or []


def ensure_daily_brief_table(token: str) -> str:
    """幂等创建每日简报表并返回 table_id。"""
    for table in list_tables(token):
        if table.get("name") == "每日简报":
            return str(table["table_id"])
    fields = [
        {"field_name": "简报ID", "type": 1},
        {"field_name": "简报日期", "type": 5},
        {"field_name": "简报标题", "type": 1},
        {"field_name": "导语", "type": 1},
        {"field_name": "关键要点", "type": 1},
        {"field_name": "信号记录ID", "type": 1},
        {"field_name": "状态", "type": 3, "property": {"options": [{"name": x} for x in ("草稿", "已发布")]}},
        {"field_name": "网页路径", "type": 1},
        {"field_name": "发送状态", "type": 3, "property": {"options": [{"name": x} for x in ("待发送", "已发送", "失败")]}},
        {"field_name": "消息ID", "type": 1},
        {"field_name": "发送时间", "type": 5},
    ]
    url = f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}/tables"
    resp = _SESSION.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"table": {"name": "每日简报", "default_view_name": "全部", "fields": fields}},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"Feishu create brief table failed: {data.get('code')} {data.get('msg')}")
    payload = data.get("data") or {}
    return str(payload.get("table_id") or (payload.get("table") or {}).get("table_id"))


def create_record(token: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{table_id}/records"
    )
    resp = _SESSION.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"fields": fields},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"Feishu create record failed: {data.get('code')} {data.get('msg')}")
    return (data.get("data") or {}).get("record") or {}


def update_record(
    token: str, table_id: str, record_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{table_id}/records/{record_id}"
    )
    resp = _SESSION.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"fields": fields},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"Feishu update record failed: {data.get('code')} {data.get('msg')}")
    return (data.get("data") or {}).get("record") or {}


def send_interactive_message(token: str, open_id: str, card: dict[str, Any]) -> str:
    """以应用机器人身份向指定 open_id 发送交互卡片。"""
    url = f"{config.FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=open_id"
    resp = _SESSION.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuError(f"Feishu send message failed: {data.get('code')} {data.get('msg')}")
    return str(((data.get("data") or {}).get("message_id")) or "")


def batch_create_records(token: str, fields_list: list[dict[str, Any]], chunk: int = 100) -> int:
    """对应 Create Feishu Record 节点：改用 batch_create 批量写入以减少请求数。"""
    if not fields_list:
        return 0
    created = 0
    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{config.FEISHU_ENTRY_TABLE_ID}/records/batch_create"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    for i in range(0, len(fields_list), chunk):
        batch = fields_list[i : i + chunk]
        body = {"records": [{"fields": f} for f in batch]}
        resp = _SESSION.post(url, headers=headers, json=body, timeout=60)
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(
                f"Feishu batch_create failed: {data.get('code')} {data.get('msg')}"
            )
        created += len(batch)
        log.info("已写入飞书 %d/%d 条", created, len(fields_list))
    return created


def update_param_collect_stats(
    token: str,
    attempted_ids: set[str],
    cleaned_counts: dict[str, int],
    final_counts: dict[str, int],
    id_to_record: dict[str, str],
    chunk: int = 500,
) -> int:
    """采集健康度回写：对本轮尝试过的每个源，写回「通过 / 最近采集时间 / 条目数 / 查重过滤」。

    - 通过：本轮是否成功采集到条目（清洗过滤后条目数 > 0）
    - 最近采集时间：本轮采集时刻（毫秒时间戳）
    - 条目数：本轮最终录入飞书的条目数（跨轮去重后）
    - 查重过滤：本轮成功采集到但因已存在被去重过滤掉的条目数（采集到 - 最终录入）
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    records: list[dict[str, Any]] = []
    for sid in attempted_ids:
        rid = id_to_record.get(sid)
        if not rid:
            continue
        collected = int(cleaned_counts.get(sid, 0))
        final = int(final_counts.get(sid, 0))
        deduped = max(collected - final, 0)
        records.append(
            {
                "record_id": rid,
                "fields": {
                    "通过": collected > 0,
                    "最近采集时间": now_ms,
                    "条目数": final,
                    "查重过滤": deduped,
                },
            }
        )
    if not records:
        return 0

    url = (
        f"{config.FEISHU_HOST}/open-apis/bitable/v1/apps/{config.FEISHU_BASE_ID}"
        f"/tables/{config.FEISHU_PARAM_TABLE_ID}/records/batch_update"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    updated = 0
    for i in range(0, len(records), chunk):
        batch = records[i : i + chunk]
        resp = _SESSION.post(url, headers=headers, json={"records": batch}, timeout=60)
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(
                f"Feishu param stats update failed: {data.get('code')} {data.get('msg')}"
            )
        updated += len(batch)
    log.info("已回写源采集健康度 %d 个源", updated)
    return updated
