"""为「参数表」「条目表」对齐并回填「来源类型」。

用法：
  python -m src.backfill_source_type
  python -m src.backfill_source_type --entry-only
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from . import config, feishu, sources, typed_config as tcfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("backfill_source_type")


def _param_format(
    fields: dict,
    type_configs: dict[str, dict],
) -> str:
    sid = str(sources.cell(fields.get("source_id")) or "").strip()
    endpoint = sources.normalize_endpoint(sources.cell(fields.get("endpoint")))
    fetch_method = str(sources.cell(fields.get("fetch_method")) or "")
    entity = (type_configs.get(sid) or {}).get("entity_type")
    # 回填按配置/启发式重算，不以已有字段为准（便于纠正）
    return sources.infer_signal_format(
        sid,
        endpoint=endpoint,
        extra=sources._parse_extra(fields),  # noqa: SLF001 - shared helper
        fetch_method=fetch_method,
        entity_type=entity,
        explicit_type=None,
    )


def align_entry_source_type(token: str) -> dict[str, int]:
    """条目表：迁移旧英文枚举 → 标准 7 项，并收缩字段选项与另两表一致。"""
    table_id = config.FEISHU_ENTRY_TABLE_ID
    expanded = feishu.ensure_source_type_options_include_standard(token, table_id)
    log.info("条目表选项扩容: %s", expanded)

    records = feishu.list_table_records(token, table_id)
    updates: list[dict] = []
    before = Counter()
    after = Counter()
    for rec in records:
        fields = rec.get("fields") or {}
        raw = sources.cell(fields.get("来源类型"))
        before[raw or "(empty)"] += 1
        if not raw:
            after["(empty)"] += 1
            continue
        normalized = sources.normalize_signal_format(raw)
        # Research / Company Blog 等旧值走别名；已是标准值保持不动
        if normalized not in feishu.SIGNAL_FORMAT_OPTIONS:
            # 兜底：按 source_id / 链接再推断
            sid = str(sources.cell(fields.get("source_id")) or "")
            link = fields.get("链接")
            url = ""
            if isinstance(link, dict):
                url = str(link.get("link") or "")
            normalized = sources.infer_signal_format(sid, endpoint=url, explicit_type=None)
        after[normalized] += 1
        if str(raw) == normalized:
            continue
        updates.append({"record_id": rec["record_id"], "fields": {"来源类型": normalized}})

    n = feishu.batch_update_records(token, table_id, updates)
    log.info("条目表记录迁移 %d/%d；迁移前 %s → 后 %s", n, len(records), dict(before), dict(after))

    aligned = feishu.align_source_type_field_options(token, table_id)
    log.info("条目表选项对齐: %s", aligned)
    return {"entry_updated": n, "entry_total": len(records)}


def run(*, entry_only: bool = False) -> dict[str, int]:
    config.validate()
    token = feishu.get_tenant_access_token()
    result: dict[str, int] = {}

    if not entry_only:
        feishu.ensure_source_type_field(token, config.FEISHU_PARAM_TABLE_ID)
        # 参数表选项也强制对齐为标准 7 项
        info = feishu.align_source_type_field_options(token, config.FEISHU_PARAM_TABLE_ID)
        log.info("参数表选项对齐: %s", info)

        type_configs = tcfg.load_typed_configs(token)
        paper_ids = {
            sid for sid, cfg in type_configs.items() if cfg.get("entity_type") == "paper"
        } | set(sources._PAPER_IDS)  # noqa: SLF001
        log.info("论文配置/已知论文源 %d 个", len(paper_ids))

        # --- 参数表 ---
        param_records = feishu.list_table_records(token, config.FEISHU_PARAM_TABLE_ID)
        param_updates: list[dict] = []
        param_counts: Counter[str] = Counter()
        for rec in param_records:
            fields = rec.get("fields") or {}
            sid = str(sources.cell(fields.get("source_id")) or "").strip()
            fmt = _param_format(fields, type_configs)
            if sid in paper_ids or sid.startswith("arxiv-"):
                fmt = sources.SIGNAL_FORMAT_PAPER
            param_counts[fmt] += 1
            current = sources.normalize_signal_format(
                sources.cell(fields.get("来源类型")) or sources.cell(fields.get("source_type"))
            )
            if current == fmt:
                continue
            param_updates.append({"record_id": rec["record_id"], "fields": {"来源类型": fmt}})

        n_param = feishu.batch_update_records(token, config.FEISHU_PARAM_TABLE_ID, param_updates)
        log.info("参数表回填 %d/%d：%s", n_param, len(param_records), dict(param_counts))
        result["param_updated"] = n_param

    result.update(align_entry_source_type(token))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="对齐并回填来源类型")
    parser.add_argument("--entry-only", action="store_true", help="只处理条目表")
    args = parser.parse_args()
    run(entry_only=args.entry_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
