"""仓库为准的配置读取层：把 seed_default.json 当成采集配置的权威来源。

以前配置只活在飞书多维表格里，仓库里那份 JSON 是事后导出的快照。快照会漂：
迁移前实测线上 116 个源、仓库只有 108 个，8 个播客源（5 个在跑）整个缺失，
另有 7 个源的 status 从未回写。快照漂了没人看得见，直到有人拿仓库重建库。

这里把方向反过来——文件是权威，飞书降级成给人看的镜像。换来的是每次改源都有
git 历史可 diff 可回滚、配置能和依赖它的代码同一个 commit 生效、离线无凭据。

返回形状与 feishu.read_param_records / typed_config.load_typed_configs 完全一致，
所以下游 mapper 一行都不用改。等价性由 tools/verify_config_parity.py 守着。

**这里只有配置，没有运行时状态。** 采集游标（社媒 since_id）、每轮统计不在文件里，
它们每轮都变，混进来会在回滚配置时把状态一起退回去。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import typed_config

log = logging.getLogger(__name__)

SEED_FILE = Path(__file__).resolve().parent / "seed_default.json"

PARAM_TABLE = "一级参数"

# 逻辑表名 -> typed_config 的 entity_type
TYPED_TABLES: tuple[tuple[str, str], ...] = (
    ("二级参数-论文", "paper"),
    ("二级参数-公众号", "wechat"),
    ("二级参数-视频", "video"),
    ("二级参数-社媒", "social"),
    ("二级参数-GitHub", "github"),
)


class ConfigStoreError(RuntimeError):
    """配置文件缺失或不是合法 JSON。"""


def load_bundle(path: Path | str = SEED_FILE) -> dict[str, list[dict[str, Any]]]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigStoreError(f"读不到配置文件 {path}：{exc}") from exc
    except ValueError as exc:
        raise ConfigStoreError(f"配置文件不是合法 JSON {path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigStoreError(f"配置文件顶层应是对象：{path}")
    return {key: value for key, value in raw.items() if isinstance(value, list)}


def read_param_records(path: Path | str = SEED_FILE) -> list[dict[str, Any]]:
    """一级参数，形状对齐 feishu.read_param_records。

    record_id 用 source_id 顶上：文件里没有飞书那套记录 id，而 source_id 本来就是
    这张表的主键，用它做本地标识既稳定又可读。
    """
    rows = load_bundle(path).get(PARAM_TABLE) or []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("source_id") or "").strip()
        if not source_id:
            log.warning("一级参数有行缺 source_id，跳过：%s", row.get("name") or row)
            continue
        if source_id in seen:
            log.warning("一级参数 source_id 重复，保留先读到的：%s", source_id)
            continue
        seen.add(source_id)
        records.append({"record_id": source_id, "fields": dict(row)})
    return records


def load_typed_configs(path: Path | str = SEED_FILE) -> dict[str, dict[str, Any]]:
    """5 张二级参数表，形状对齐 typed_config.load_typed_configs。"""
    bundle = load_bundle(path)
    return typed_config.build_typed_configs(
        (entity, [row for row in (bundle.get(table) or []) if isinstance(row, dict)])
        for table, entity in TYPED_TABLES
    )
