"""对比「运行时从飞书读到的配置」与「导出成本地文件后读到的配置」。

迁移到仓库为准之前必须先证明两条路径解析结果一致，否则切过去会静默改变
筛选行为。分两层比对：

- 解析层（默认，真正重要）：把两边的记录喂给 sources 的 5 个 mapper 和
  typed_config._parse_row，比对流水线实际消费的对象。
- 字段层（--fields）：逐字段比对 sources.cell() 的输出。这层允许存在
  数字字段 str/int 的表示差异，只要解析层一致就不影响行为。

用法：
    python -m tools.verify_config_parity
    python -m tools.verify_config_parity --fields --raw
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

_env = Path(__file__).resolve().parents[1] / ".env"
if _env.is_file():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src import config, feishu, sources, typed_config  # noqa: E402
from tools import export_seed  # noqa: E402

PARAM_TABLE = ("一级参数", config.FEISHU_PARAM_TABLE_ID)
TYPED_TABLES: list[tuple[str, str, str]] = [
    ("二级参数-论文", "paper", config.FEISHU_PAPER_CONFIG_TABLE_ID),
    ("二级参数-公众号", "wechat", config.FEISHU_WECHAT_CONFIG_TABLE_ID),
    ("二级参数-视频", "video", config.FEISHU_VIDEO_CONFIG_TABLE_ID),
    ("二级参数-社媒", "social", config.FEISHU_SOCIAL_CONFIG_TABLE_ID),
    ("二级参数-GitHub", "github", config.FEISHU_GITHUB_CONFIG_TABLE_ID),
]

SKIP = export_seed.STATE_FIELDS

MAPPERS = {
    "feed": sources.map_feed_sources,
    "media": sources.map_media_sources,
    "podcast": sources.map_podcast_sources,
    "social": sources.map_social_sources,
    "scrape": sources.map_scrape_sources,
}


def _shape(value: Any) -> str:
    if isinstance(value, list):
        inner = {type(x).__name__ for x in value}
        keys = sorted({k for x in value if isinstance(x, dict) for k in x})
        return f"list[{','.join(sorted(inner))}]{keys or ''}"
    if isinstance(value, dict):
        return f"dict{sorted(value)}"
    return type(value).__name__


def _key_of(row: dict[str, Any]) -> str:
    for candidate in ("source_id", "名称", "name"):
        got = sources.cell(row.get(candidate))
        if got:
            return str(got)
    return "<unkeyed>"


def _norm(value: Any) -> Any:
    """数字的 str/int 表示差异不算行为差异，比对前统一。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            num = float(value)
        except ValueError:
            return value
        return int(num) if num.is_integer() else num
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _diff(label: str, live: Any, filed: Any, out: list[str]) -> None:
    live, filed = _norm(live), _norm(filed)
    if live == filed:
        return
    if isinstance(live, dict) and isinstance(filed, dict):
        for key in sorted(set(live) | set(filed)):
            a, b = live.get(key), filed.get(key)
            if (a in (None, "", [], {})) and (b in (None, "", [], {})):
                continue
            _diff(f"{label}.{key}", a, b, out)
        return
    out.append(f"    {label}\n        飞书={live!r}\n        文件={filed!r}")


def _compare_keyed(
    label: str,
    live: list[dict[str, Any]],
    filed: list[dict[str, Any]],
    key: str,
) -> int:
    live_by = {str(r.get(key)): r for r in live}
    filed_by = {str(r.get(key)): r for r in filed}
    problems = 0

    only_live = sorted(set(live_by) - set(filed_by))
    only_file = sorted(set(filed_by) - set(live_by))
    if only_live or only_file:
        problems += len(only_live) + len(only_file)
        print(f"[{label}] 源集合不一致")
        if only_live:
            print(f"    仅飞书有: {only_live}")
        if only_file:
            print(f"    仅文件有: {only_file}")

    lines: list[str] = []
    for k in sorted(set(live_by) & set(filed_by)):
        a = {kk: vv for kk, vv in live_by[k].items() if kk != "record_id"}
        b = {kk: vv for kk, vv in filed_by[k].items() if kk != "record_id"}
        _diff(k, a, b, lines)
    if lines:
        problems += len(lines)
        print(f"[{label}] 解析结果差异 {len(lines)} 处")
        for line in lines[:30]:
            print(line)

    print(f"[{label}] 源 {len(live_by)} / 文件 {len(filed_by)}，差异 {problems}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fields", action="store_true", help="附带字段层比对")
    ap.add_argument("--raw", action="store_true", help="附带原始值形状统计")
    args = ap.parse_args()

    token = export_seed._token()
    problems = 0
    shapes: Counter[str] = Counter()
    multi_segment: list[str] = []

    name, tid = PARAM_TABLE
    live_param = feishu.read_param_records(token)
    exported_param = export_seed._clean(token, tid, SKIP)
    filed_param = [{"record_id": "", "fields": row} for row in exported_param]

    for rec in live_param:
        for field, value in (rec.get("fields") or {}).items():
            shapes[_shape(value)] += 1
            if isinstance(value, list) and len(value) > 1 and all(
                isinstance(x, dict) and "text" in x for x in value
            ):
                multi_segment.append(f"{name}/{field}")

    print(f"=== 解析层：一级参数（飞书 {len(live_param)} 行 / 导出 {len(filed_param)} 行）===")
    for mapper_name, mapper in MAPPERS.items():
        # mapper 输出以 id 为主键；record_id 两边必然不同，_compare_keyed 已排除
        problems += _compare_keyed(
            f"mapper:{mapper_name}", mapper(live_param), mapper(filed_param), "id"
        )

    print("\n=== 解析层：二级参数 ===")
    for table_name, entity, table_id in TYPED_TABLES:
        if not table_id:
            print(f"[skip] {table_name}: 未配置 table_id")
            continue
        live_rows = feishu.read_all_records(token, table_id)
        filed_rows = export_seed._clean(token, table_id)
        for row in live_rows:
            for field, value in row.items():
                shapes[_shape(value)] += 1

        def parsed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for fields in rows:
                sid = str(feishu._read_cell_key(fields.get("source_id")) or "").strip()
                if not sid:
                    continue
                out.append({"source_id": sid, **typed_config._parse_row(entity, fields)})
            return out

        problems += _compare_keyed(
            f"typed:{entity}", parsed(live_rows), parsed(filed_rows), "source_id"
        )

    if args.fields:
        print("\n=== 字段层（数字 str/int 差异已归一）===")
        live_by = {_key_of(r.get("fields") or {}): (r.get("fields") or {}) for r in live_param}
        filed_by = {_key_of(r): r for r in exported_param}
        lines: list[str] = []
        for key in sorted(set(live_by) & set(filed_by)):
            for field in sorted(set(live_by[key]) | set(filed_by[key])):
                if field in SKIP:
                    continue
                a, b = sources.cell(live_by[key].get(field)), sources.cell(filed_by[key].get(field))
                if (a in (None, "", [])) and (b in (None, "", [])):
                    continue
                _diff(f"{key}.{field}", a, b, lines)
        print(f"[一级参数] 字段层差异 {len(lines)}")
        for line in lines[:30]:
            print(line)
        problems += len(lines)

    if multi_segment:
        print(f"\n富文本多段字段 {len(multi_segment)} 处（导出拼接 vs 运行时取首段）")
        for line in sorted(set(multi_segment)):
            print("   ", line)

    if args.raw:
        print("\n原始值形状统计：")
        for shape, count in shapes.most_common():
            print(f"    {count:5d}  {shape}")

    print(f"\n总差异 {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
