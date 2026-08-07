"""本地数据源配置服务：静态托管 site/ 目录，并提供 /api/sources 读写飞书一级参数表。

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
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import config, feishu, source_view, sources

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
API_PREFIX = "/api/sources"
_SOURCE_ID_RE = re.compile(r"[^a-z0-9]+")


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
        status = source_view.normalize_status(body.get("status"))
        fields["status"] = status
    if "priority" in body:
        priority = str(body.get("priority") or "")
        code = source_view.CN_TO_PRIORITY.get(priority)
        if not code:
            raise ApiError(f"未知的优先级：{priority}")
        fields["priority"] = code
    if not fields:
        raise ApiError("没有可更新的字段")
    return fields


def apply_patch(record_id: str, body: dict[str, Any]) -> dict[str, Any]:
    # 先校验再取 token：请求本身不合法时应回 400，而不是被飞书的报错盖住。
    fields = _patch_fields(body)
    token = feishu.get_tenant_access_token()
    feishu.update_record(token, config.FEISHU_PARAM_TABLE_ID, record_id, fields)
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

    def _record_id(self) -> str:
        record_id = unquote(self.path[len(API_PREFIX) :].strip("/"))
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
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != API_PREFIX:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._dispatch(lambda: create_source(self._read_body(), self.site_dir))

    def do_PATCH(self) -> None:  # noqa: N802
        if not self.path.startswith(f"{API_PREFIX}/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._dispatch(lambda: apply_patch(self._record_id(), self._read_body()))

    def do_DELETE(self) -> None:  # noqa: N802
        if not self.path.startswith(f"{API_PREFIX}/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._dispatch(lambda: delete_source(self._record_id()))

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
