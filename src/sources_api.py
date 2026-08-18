"""本地配置服务：静态托管 site/，提供信号源、周报队列和动向追踪 API。

只监听回环地址。它持有飞书凭据，而公开的 GitHub Pages 站点是纯静态的，拿不到也
不该拿到这些凭据——那份站点读的是 publish 导出的 data/sources.json 只读快照。
采集本身仍然跑在 GitHub Actions 上，这个服务不参与定时任务。

    python -m src.sources_api           # http://127.0.0.1:8787
"""
from __future__ import annotations

import argparse
import functools
import json
import re
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import config, feishu, source_view, sources, timeline

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
API_PREFIX = "/api/sources"
PENDING_PREFIX = "/api/report-pending"
TIMELINE_PREFIX = "/api/tracked-entities"
_SOURCE_ID_RE = re.compile(r"[^a-z0-9]+")
_TRACKED_ENTITY_WRITE_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def slugify_source_id(name: str, taken: set[str]) -> str:
    base = _SOURCE_ID_RE.sub("-", str(name or "").strip().lower()).strip("-")
    base = base or "source"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def load_local_briefs(site_dir: Path, limit: int = 7) -> list[dict[str, Any]]:
    """读已发布的简报快照，用来算「近几期入选条数」。缺文件时静默降级为空。"""
    data_dir = Path(site_dir) / "data"
    if not data_dir.is_dir():
        return []
    briefs: list[dict[str, Any]] = []
    for path in sorted(data_dir.glob("brief-20*.json"), reverse=True)[:limit]:
        try:
            briefs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return briefs


def read_payload(site_dir: Path) -> dict[str, Any]:
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    return source_view.build_payload(
        records, briefs=load_local_briefs(site_dir), writable=True
    )


def _patch_fields(body: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "status" in body:
        raw = str(body.get("status") or "").strip()
        # normalize_status 对认不出的值兜底成 paused，用在写入上会把打错的字段
        # 静默变成停用，所以这里只认白名单。
        if raw not in source_view.STATUS_LABELS and raw not in source_view.LABEL_TO_STATUS:
            raise ApiError(f"未知的状态：{raw}")
        fields["status"] = source_view.normalize_status(raw)
    if "priority" in body:
        priority = str(body.get("priority") or "")
        code = source_view.CN_TO_PRIORITY.get(priority)
        if not code:
            raise ApiError(f"未知的优先级：{priority}")
        fields["priority"] = code
    if not fields:
        raise ApiError("没有可更新的字段")
    return fields


def sync_source_table_status(token: str, name: str, status: str) -> None:
    """把参数表状态同步到信号源表的「自动化状态」，避免两表漂移。

    信号源表是人工清单，不影响采集，同步失败不该让开关操作整体失败。
    """
    table_id = getattr(config, "FEISHU_SOURCE_TABLE_ID", "")
    if not table_id or not name:
        return
    label = source_view.STATUS_LABELS.get(status)
    if not label:
        return
    try:
        for record in feishu.read_all_records_with_ids(token, table_id):
            fields = record.get("fields") or {}
            if str(sources.cell(fields.get("名称")) or "").strip() != name.strip():
                continue
            if str(sources.cell(fields.get("自动化状态")) or "").strip() == label:
                return
            feishu.update_record(
                token, table_id, str(record.get("record_id") or ""), {"自动化状态": label}
            )
            return
    except feishu.FeishuError:
        return


def apply_patch(record_id: str, body: dict[str, Any]) -> dict[str, Any]:
    # 先校验再取 token：请求本身不合法时应回 400，而不是被飞书的报错盖住。
    fields = _patch_fields(body)
    token = feishu.get_tenant_access_token()
    feishu.update_record(token, config.FEISHU_PARAM_TABLE_ID, record_id, fields)
    if "status" in fields:
        name = ""
        for record in feishu.read_param_records(token):
            if str(record.get("record_id") or "") == record_id:
                name = str(sources.cell((record.get("fields") or {}).get("name")) or "")
                break
        sync_source_table_status(token, name, str(fields["status"]))
    return {"ok": True}


def create_source(body: dict[str, Any], site_dir: Path) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError("来源名称不能为空")
    if not str(body.get("url") or "").strip():
        raise ApiError("采集地址不能为空")
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    taken = {
        str(sources.cell((r.get("fields") or {}).get("source_id")) or "") for r in records
    }
    source_id = str(body.get("id") or "").strip() or slugify_source_id(name, taken)
    if source_id in taken:
        raise ApiError(f"source_id 已存在：{source_id}")
    fields = {
        "source_id": source_id,
        "name": name,
        "endpoint": str(body.get("url") or "").strip(),
        "fetch_method": str(body.get("fetchMethod") or "RSS").strip(),
        "dimension": str(body.get("type") or "其他").strip(),
        "来源类型": str(body.get("format") or "纯网页").strip(),
        "priority": source_view.CN_TO_PRIORITY.get(str(body.get("priority") or "中"), "P1"),
        "tier": str(body.get("tier") or "L3").strip(),
        "lookback_window": str(body.get("lookback") or "7d").strip(),
        # 新接入的源一律先落 experimental：配置存在不等于链路已验证。
        "status": source_view.STATUS_EXPERIMENTAL,
    }
    record = feishu.create_record(token, config.FEISHU_PARAM_TABLE_ID, fields)
    return {"source": source_view.build_source(record)}


def delete_source(record_id: str) -> dict[str, Any]:
    token = feishu.get_tenant_access_token()
    feishu.delete_record(token, config.FEISHU_PARAM_TABLE_ID, record_id)
    return {"ok": True}


def _pending_table_id(token: str) -> str:
    return config.FEISHU_WEEKLY_PENDING_TABLE_ID or feishu.ensure_weekly_pending_table(token)


def read_pending_payload() -> dict[str, Any]:
    token = feishu.get_tenant_access_token()
    records = feishu.read_all_records_with_ids(token, _pending_table_id(token))
    items = []
    for record in records:
        fields = record.get("fields") or {}
        status = str(sources.cell(fields.get("状态")) or "")
        if status == "已移除":
            continue
        items.append(
            {
                "id": str(record.get("record_id") or ""),
                "recordId": str(sources.cell(fields.get("条目记录ID")) or ""),
                "title": str(sources.cell(fields.get("中文标题")) or ""),
                "targetWeek": str(sources.cell(fields.get("目标周期")) or ""),
                "status": status,
                "addedAt": int(float(sources.cell(fields.get("添加时间")) or 0)),
            }
        )
    items.sort(key=lambda item: item["addedAt"], reverse=True)
    return {"items": items, "writable": True}


def create_pending(body: dict[str, Any]) -> dict[str, Any]:
    record_id = str(body.get("recordId") or "").strip()
    if not record_id:
        raise ApiError("缺少条目 recordId")
    token = feishu.get_tenant_access_token()
    entry = next(
        (
            item
            for item in feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
            if str(item.get("record_id") or "") == record_id
        ),
        None,
    )
    if not entry:
        raise ApiError("信号条目不存在", HTTPStatus.NOT_FOUND)
    table_id = _pending_table_id(token)
    for existing in feishu.read_all_records_with_ids(token, table_id):
        fields = existing.get("fields") or {}
        if (
            str(sources.cell(fields.get("条目记录ID")) or "") == record_id
            and str(sources.cell(fields.get("状态")) or "") == "待纳入"
        ):
            return {"item": {"id": existing.get("record_id"), "recordId": record_id}}
    entry_fields = entry.get("fields") or {}
    title = str(
        sources.cell(entry_fields.get("中文标题"))
        or sources.cell(entry_fields.get("标题"))
        or record_id
    )
    created = feishu.create_record(
        token,
        table_id,
        {
            "条目记录ID": record_id,
            "中文标题": title,
            "添加人open_id": str(body.get("openId") or "").strip(),
            "添加时间": int(time.time() * 1000),
            "目标周期": str(body.get("targetWeek") or "").strip(),
            "状态": "待纳入",
        },
    )
    return {
        "item": {
            "id": str(created.get("record_id") or ""),
            "recordId": record_id,
            "title": title,
            "status": "待纳入",
        }
    }


def remove_pending(queue_record_id: str) -> dict[str, Any]:
    token = feishu.get_tenant_access_token()
    feishu.update_record(
        token, _pending_table_id(token), queue_record_id, {"状态": "已移除"}
    )
    return {"ok": True}


def read_timeline_payload() -> dict[str, Any]:
    payload = timeline.sync()
    timeline.write_payload(payload, SITE_DIR / "data" / "timeline-latest.json")
    payload["writable"] = True
    return payload


def _create_tracked_entity_unlocked(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError("追踪对象名称不能为空")
    entity_type = str(body.get("type") or "机构").strip()
    if entity_type not in {"机构", "人物", "技术"}:
        raise ApiError(f"未知的追踪对象类型：{entity_type}")
    token = feishu.get_tenant_access_token()
    table_id = (
        config.FEISHU_TRACKED_ENTITY_TABLE_ID
        or feishu.ensure_tracked_entity_table(token)
    )
    records = feishu.read_all_records_with_ids(token, table_id)
    entities = [timeline.entity_from_record(record) for record in records]
    if any(entity["name"].lower() == name.lower() for entity in entities):
        raise ApiError(f"已在追踪：{name}")
    taken = {entity["id"] for entity in entities}
    entity_id = timeline.slugify_entity_id(name, taken)

    def term_text(key: str) -> str:
        value = body.get(key) or ""
        if isinstance(value, list):
            return "，".join(str(item).strip() for item in value if str(item).strip())
        return str(value).strip()

    created = feishu.create_record(
        token,
        table_id,
        {
            "entity_id": entity_id,
            "名称": name,
            "类型": entity_type,
            "别名": term_text("aliases"),
            "关键词": term_text("keywords"),
            "排除词": term_text("excludes"),
            "状态": "active",
            "回溯天数": int(
                body.get("lookbackDays") or config.TIMELINE_DEFAULT_LOOKBACK_DAYS
            ),
            "最低影响分": int(
                body.get("minImpact") or config.TIMELINE_DEFAULT_MIN_IMPACT
            ),
            "创建时间": int(time.time() * 1000),
        },
    )
    payload = timeline.sync(entity_id)
    timeline.write_payload(payload, SITE_DIR / "data" / "timeline-latest.json")
    entity = next(
        (item for item in payload["entities"] if item["id"] == entity_id),
        {"id": entity_id, "name": name, "events": []},
    )
    entity["recordId"] = str(created.get("record_id") or entity.get("recordId") or "")
    return {"entity": entity, "createdEvents": payload["createdEvents"]}


def create_tracked_entity(body: dict[str, Any]) -> dict[str, Any]:
    # ThreadingHTTPServer 可能同时收到双击请求；锁住“查重 → 创建”避免重复对象。
    with _TRACKED_ENTITY_WRITE_LOCK:
        return _create_tracked_entity_unlocked(body)


def delete_tracked_entity(record_id: str) -> dict[str, Any]:
    token = feishu.get_tenant_access_token()
    entity_table_id = (
        config.FEISHU_TRACKED_ENTITY_TABLE_ID
        or feishu.ensure_tracked_entity_table(token)
    )
    record = next(
        (
            item
            for item in feishu.read_all_records_with_ids(token, entity_table_id)
            if str(item.get("record_id") or "") == record_id
        ),
        None,
    )
    if not record:
        raise ApiError("追踪对象不存在", HTTPStatus.NOT_FOUND)
    entity_id = timeline.entity_from_record(record)["id"]
    event_table_id = (
        config.FEISHU_TRACKED_EVENT_TABLE_ID
        or feishu.ensure_tracked_event_table(token)
    )
    event_record_ids = [
        str(item.get("record_id") or "")
        for item in feishu.read_all_records_with_ids(token, event_table_id)
        if str(
            sources.cell((item.get("fields") or {}).get("entity_id")) or ""
        ) == entity_id
    ]
    feishu.batch_delete_records(token, event_table_id, event_record_ids)
    feishu.delete_record(token, entity_table_id, record_id)
    payload = timeline.sync()
    timeline.write_payload(payload, SITE_DIR / "data" / "timeline-latest.json")
    return {"ok": True}


class SourcesHandler(SimpleHTTPRequestHandler):
    site_dir: Path = SITE_DIR

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(f"请求体不是合法 JSON：{exc}") from exc

    def _record_id(self, prefix: str = API_PREFIX) -> str:
        record_id = unquote(self.path[len(prefix) :].strip("/"))
        if not record_id or "/" in record_id:
            raise ApiError("缺少 recordId")
        return record_id

    def _dispatch(self, handler) -> None:
        try:
            self._send_json(handler())
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001 - 单机服务，错误直接回给页面
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.BAD_GATEWAY)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.rstrip("/") == API_PREFIX:
            self._dispatch(lambda: read_payload(self.site_dir))
            return
        if self.path.rstrip("/") == PENDING_PREFIX:
            self._dispatch(read_pending_payload)
            return
        if self.path.rstrip("/") == TIMELINE_PREFIX:
            self._dispatch(read_timeline_payload)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == API_PREFIX:
            self._dispatch(lambda: create_source(self._read_body(), self.site_dir))
            return
        if path == PENDING_PREFIX:
            self._dispatch(lambda: create_pending(self._read_body()))
            return
        if path == TIMELINE_PREFIX:
            self._dispatch(lambda: create_tracked_entity(self._read_body()))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:  # noqa: N802
        if not self.path.startswith(f"{API_PREFIX}/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._dispatch(lambda: apply_patch(self._record_id(), self._read_body()))

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith(f"{API_PREFIX}/"):
            self._dispatch(lambda: delete_source(self._record_id()))
            return
        if self.path.startswith(f"{PENDING_PREFIX}/"):
            self._dispatch(
                lambda: remove_pending(self._record_id(PENDING_PREFIX))
            )
            return
        if self.path.startswith(f"{TIMELINE_PREFIX}/"):
            self._dispatch(
                lambda: delete_tracked_entity(self._record_id(TIMELINE_PREFIX))
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        if "/api/" in self.path:
            super().log_message(fmt, *args)


def serve(host: str, port: int, site_dir: Path) -> None:
    handler = functools.partial(SourcesHandler, directory=str(site_dir))
    SourcesHandler.site_dir = site_dir
    with ThreadingHTTPServer((host, port), handler) as httpd:
        print(f"数据源配置台 http://{host}:{port}/  （站点目录 {site_dir}）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")


def run() -> int:
    parser = argparse.ArgumentParser(description="本地数据源配置服务")
    parser.add_argument("--host", default="127.0.0.1", help="默认只监听回环地址")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--site-dir", default=str(SITE_DIR))
    args = parser.parse_args()
    site_dir = Path(args.site_dir)
    if not site_dir.is_dir():
        raise SystemExit(f"站点目录不存在：{site_dir}（先跑一次 python -m src.publish）")
    # 缺凭据时页面会静默退回只读快照，看起来像「配置台没生效」。宁可起不来也别装作能写。
    try:
        config.validate()
        feishu.get_tenant_access_token()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"连不上飞书，配置台无法写回：{exc}\n"
            "先把凭据加载进当前 shell：set -a; . ./.env; set +a"
        ) from exc
    serve(args.host, args.port, site_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
