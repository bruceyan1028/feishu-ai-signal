"""按 source_id 批量修正「一级参数」表的字段，同时改 seed 快照与飞书线上表。

配置的权威在飞书，仓库里的 src/seed_default.json 只是快照；两边任改一边都会漂移。
这个工具把一份补丁同时落到两处：先改 seed（不联网也能做），再读飞书线上值算差异，
加 --write 才真正写回。补丁文件是 {source_id: {字段: 新值}}：

    {
      "qbitai": {"lookback_window": "24h", "tier": "L3"},
      "jiemian": {"extra_config": {"keyword_min_hits": 2}}
    }

extra_config 给 dict 时会合并进现有 JSON（只覆盖给出的键），给字符串则整体替换。

用法：
    python -m tools.patch_param_table tools/patches/xxx.json            # 改 seed + 打印线上差异
    python -m tools.patch_param_table tools/patches/xxx.json --write    # 再写回飞书
    python -m tools.patch_param_table tools/patches/xxx.json --seed-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEED_FILE = ROOT / "src" / "seed_default.json"

_env = ROOT / ".env"
if _env.is_file():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _merge_extra(current: Any, patch: Any) -> str:
    if not isinstance(patch, dict):
        return str(patch)
    try:
        base = json.loads(current) if isinstance(current, str) and current.strip() else {}
    except json.JSONDecodeError:
        base = {}
    if not isinstance(base, dict):
        base = {}
    base.update(patch)
    return json.dumps(base, ensure_ascii=False)


def _norm(value: Any) -> str:
    """比较用：数字与字符串同形，None 与空串同形。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def apply_to_seed(patch: dict[str, dict[str, Any]], path: Path = SEED_FILE) -> dict[str, dict[str, tuple[Any, Any]]]:
    bundle = json.loads(path.read_text(encoding="utf-8"))
    rows = {row["source_id"]: row for row in bundle["一级参数"] if row.get("source_id")}
    missing = sorted(set(patch) - set(rows))
    if missing:
        raise SystemExit(f"seed 里找不到这些 source_id：{missing}")
    changes: dict[str, dict[str, tuple[Any, Any]]] = {}
    for sid, fields in patch.items():
        row = rows[sid]
        for key, value in fields.items():
            new = _merge_extra(row.get(key), value) if key == "extra_config" else value
            old = row.get(key)
            if _norm(old) == _norm(new):
                continue
            changes.setdefault(sid, {})[key] = (old, new)
            row[key] = new
    for row in bundle["一级参数"]:
        # export_seed 写出的行是按键排序的，这里保持同样形状，diff 才干净
        ordered = {k: row[k] for k in sorted(row)}
        row.clear()
        row.update(ordered)
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    return changes


def diff_against_feishu(patch: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    from src import feishu, sources

    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    by_id: dict[str, dict[str, Any]] = {}
    for rec in records:
        fields = rec.get("fields") or {}
        sid = str(sources.cell(fields.get("source_id")) or "").strip()
        if sid:
            by_id[sid] = {"record_id": rec.get("record_id"), "fields": fields}
    missing = sorted(set(patch) - set(by_id))
    if missing:
        print(f"⚠ 飞书表里找不到这些 source_id，跳过：{missing}", file=sys.stderr)
    updates: list[dict[str, Any]] = []
    for sid, fields in patch.items():
        live = by_id.get(sid)
        if not live:
            continue
        pending: dict[str, Any] = {}
        for key, value in fields.items():
            current = sources.cell(live["fields"].get(key))
            new = _merge_extra(current, value) if key == "extra_config" else value
            if _norm(current) != _norm(new):
                pending[key] = new
                print(f"  {sid:<28} {key:<18} {_norm(current)[:60]!r} -> {_norm(new)[:60]!r}")
        if pending:
            updates.append({"record_id": live["record_id"], "fields": pending, "source_id": sid})
    return updates


def write_to_feishu(updates: list[dict[str, Any]]) -> int:
    from src import config, feishu

    token = feishu.get_tenant_access_token()
    payload = [{"record_id": u["record_id"], "fields": u["fields"]} for u in updates]
    return feishu.batch_update_records(token, config.FEISHU_PARAM_TABLE_ID, payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patch", type=Path)
    ap.add_argument("--write", action="store_true", help="把差异写回飞书一级参数表")
    ap.add_argument("--seed-only", action="store_true", help="只改 seed，不连飞书")
    args = ap.parse_args()

    patch = json.loads(args.patch.read_text(encoding="utf-8"))
    changes = apply_to_seed(patch)
    touched = sum(len(v) for v in changes.values())
    print(f"seed_default.json：{len(changes)} 个源、{touched} 个字段有改动")
    for sid, fields in changes.items():
        for key, (old, new) in fields.items():
            print(f"  {sid:<28} {key:<18} {_norm(old)[:60]!r} -> {_norm(new)[:60]!r}")
    if args.seed_only:
        return 0

    print("\n飞书线上表 vs 补丁：")
    try:
        updates = diff_against_feishu(patch)
    except Exception as exc:  # noqa: BLE001 - 连不上飞书时 seed 已改好，明确提示即可
        print(f"⚠ 读取飞书失败，未比对线上值：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not updates:
        print("  线上值已与补丁一致，无需写回")
        return 0
    if not args.write:
        print(f"\n{len(updates)} 条记录待写回；加 --write 执行")
        return 0
    n = write_to_feishu(updates)
    print(f"已写回飞书 {n} 条记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
