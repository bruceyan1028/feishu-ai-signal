"""从母版 base 导出默认配置，生成 src/seed_default.json。

给维护者用：改完母版里的信号源/各级参数后，跑一次本脚本刷新种子文件，
让别人 `python -m src.bootstrap` 建出的新库与母版保持一致。条目表不导出。

用法（需 .env 里配好指向母版的 FEISHU_APP_ID/SECRET/BASE_ID 及各表 id）：
    python -m tools.export_seed
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

# 先加载 .env，再读 config（与 bootstrap 一致）
_env = Path(__file__).resolve().parents[1] / ".env"
if _env.is_file():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src import config  # noqa: E402

H = config.FEISHU_HOST
BASE = config.FEISHU_BASE_ID
S = requests.Session()

# 逻辑表名 -> 母版 table_id（默认取 config 里作者母版的 id）
TABLES = {
    "信号源表": config.FEISHU_SOURCE_TABLE_ID,
    "一级参数": config.FEISHU_PARAM_TABLE_ID,
    "二级参数-论文": config.FEISHU_PAPER_CONFIG_TABLE_ID,
    "二级参数-公众号": config.FEISHU_WECHAT_CONFIG_TABLE_ID,
    "二级参数-视频": config.FEISHU_VIDEO_CONFIG_TABLE_ID,
    "二级参数-社媒": config.FEISHU_SOCIAL_CONFIG_TABLE_ID,
    "二级参数-GitHub": config.FEISHU_GITHUB_CONFIG_TABLE_ID,
}
# 一级参数的运行时统计字段不导出
STAT_FIELDS = {"通过", "最近采集时间", "条目数", "查重过滤", "时间窗过滤"}


def _token() -> str:
    r = S.post(
        f"{H}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET},
        timeout=30,
    ).json()
    return r["tenant_access_token"]


def _field_types(token: str, tid: str) -> dict[str, int]:
    d = S.get(
        f"{H}/open-apis/bitable/v1/apps/{BASE}/tables/{tid}/fields?page_size=100",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    ).json()["data"]["items"]
    return {f["field_name"]: f["type"] for f in d}


def _records(token: str, tid: str) -> list[dict]:
    out, pt = [], ""
    while True:
        u = f"{H}/open-apis/bitable/v1/apps/{BASE}/tables/{tid}/records?page_size=500" + (f"&page_token={pt}" if pt else "")
        d = S.get(u, headers={"Authorization": f"Bearer {token}"}, timeout=30).json()["data"]
        out += [r["fields"] for r in d["items"]]
        if not d.get("has_more"):
            break
        pt = d.get("page_token")
    return out


def _flat_text(v):
    if isinstance(v, list):
        return "".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or ""
    return v


def _one_name(v):
    if isinstance(v, list) and v:
        v = v[0]
    return (v.get("text") or v.get("name")) if isinstance(v, dict) else v


def _multi(v):
    v = v if isinstance(v, list) else [v]
    return [((x.get("text") or x.get("name")) if isinstance(x, dict) else x) for x in v if x]


def _url(v):
    if isinstance(v, list) and v:
        v = v[0]
    if isinstance(v, dict):
        link = v.get("link") or v.get("text")
        return {"link": link, "text": link} if link else None
    return {"link": v, "text": v} if isinstance(v, str) and v else None


def _clean(token: str, tid: str, drop: set[str] = frozenset()) -> list[dict]:
    ft = _field_types(token, tid)
    rows = []
    for rec in _records(token, tid):
        row = {}
        for k, val in rec.items():
            if k in drop or val in (None, "", []):
                continue
            t = ft.get(k)
            if t == 5:            # 时间字段不导出
                continue
            elif t == 1:
                cv = _flat_text(val)
            elif t == 2:
                cv = val if isinstance(val, (int, float)) else None
            elif t == 3:
                cv = _one_name(val)
            elif t == 4:
                cv = _multi(val)
            elif t == 7:
                cv = bool(val)
            elif t == 15:
                cv = _url(val)
            else:
                cv = None
            if cv not in (None, "", []):
                row[k] = cv
        if row:
            rows.append(row)
    return rows


def main() -> None:
    token = _token()
    bundle: dict[str, list[dict]] = {}
    for name, tid in TABLES.items():
        if not tid:
            continue
        drop = STAT_FIELDS if name == "一级参数" else frozenset()
        bundle[name] = _clean(token, tid, drop)
    out = Path(__file__).resolve().parents[1] / "src" / "seed_default.json"
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写出", out, {k: len(v) for k, v in bundle.items()})


if __name__ == "__main__":
    main()
