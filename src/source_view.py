"""把飞书一级参数记录转成「数据源」页面用的展示模型。

只导出用户判断一个源是否可信所需的字段。keyword_regex、min_content_chars、
dedup_key、extra_config 一律不出现在前端载荷里：站点是公开的，这些规则既泄露
筛选策略，对读者也没有可读性。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import sources

CN_TZ = timezone(timedelta(hours=8))
SEED_FILE = Path(__file__).resolve().parent / "seed_default.json"

STATUS_ACTIVE = "active"
STATUS_EXPERIMENTAL = "experimental"
STATUS_PAUSED = "paused"
STATUS_ORDER = (STATUS_ACTIVE, STATUS_EXPERIMENTAL, STATUS_PAUSED)
STATUS_LABELS = {
    STATUS_ACTIVE: "已接入",
    STATUS_EXPERIMENTAL: "待测",
    STATUS_PAUSED: "已暂停",
}
LABEL_TO_STATUS = {label: code for code, label in STATUS_LABELS.items()}

PRIORITY_TO_CN = {"P0": "高", "P1": "中", "P2": "低"}
CN_TO_PRIORITY = {label: code for code, label in PRIORITY_TO_CN.items()}
PRIORITY_ORDER = ("高", "中", "低")

# 本地配置服务支持的写操作；静态站点导出时为空，前端据此决定控件是否可点。
WRITABLE_CAPABILITIES = ("status", "priority", "create", "delete", "config")

FETCH_METHODS = ("RSS", "Scrape", "Bridge", "Social", "Media", "Podcast", "API", "Manual")
TIERS = ("L1", "L2", "L3", "L4")
FORMATS = (
    sources.SIGNAL_FORMAT_PAPER,
    sources.SIGNAL_FORMAT_WEB,
    sources.SIGNAL_FORMAT_VIDEO,
    sources.SIGNAL_FORMAT_SOCIAL,
    sources.SIGNAL_FORMAT_WECHAT,
    sources.SIGNAL_FORMAT_PODCAST,
    sources.SIGNAL_FORMAT_GITHUB,
    sources.SIGNAL_FORMAT_OTHER,
)

# 改这些字段等于改了采集链路的行为，按源状态约定必须退回「待测」重新验收。
# name / notes / tier / dimension 只影响展示与排序，不在其中。
RULE_FIELDS = frozenset(
    {
        "endpoint",
        "fetchMethod",
        "format",
        "lookbackWindow",
        "keywordRegex",
        "minContentChars",
        "dedupKey",
        "extraConfig",
    }
)

# parse_lookback_hours 对认不出的写法静默回落成 168h，所以写入侧必须从严。
_LOOKBACK_RE = re.compile(r"^\d+(?:\.\d+)?[hd]$", re.I)
_MAX_TEXT = 4000


def normalize_status(raw: Any) -> str:
    text = str(sources.cell(raw) or "").strip().lower()
    if text in STATUS_LABELS:
        return text
    if text in LABEL_TO_STATUS:
        return LABEL_TO_STATUS[text]
    # 配置台的开关传中文标签，这里统一归一成 status 码
    if text in {"已接入", "启用", "on"}:
        return STATUS_ACTIVE
    if text in {"待测", "测试中"}:
        return STATUS_EXPERIMENTAL
    return STATUS_PAUSED


def normalize_priority(raw: Any) -> str:
    text = str(sources.cell(raw) or "").strip().upper()
    if text in PRIORITY_TO_CN:
        return PRIORITY_TO_CN[text]
    if text in CN_TO_PRIORITY:
        return text
    return "中"


def _format_stamp(raw: Any) -> str:
    try:
        stamp = int(float(sources.cell(raw) or 0))
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp / 1000, CN_TZ).strftime("%m-%d %H:%M")


def _int_cell(raw: Any) -> int:
    try:
        return int(float(sources.cell(raw) or 0))
    except (TypeError, ValueError):
        return 0


def brief_counts(briefs: list[dict[str, Any]] | None) -> dict[str, int]:
    """统计每个源在近几期简报里入选了多少条。

    这和参数表回写的「条目数」是两个口径：后者是采集入库量，这里是真正被选进
    简报的量。一个源天天入库却从不入选，是用户最该看到的信号。
    """
    counts: dict[str, int] = {}
    for brief in briefs or []:
        for signal in brief.get("signals") or []:
            source_id = str(signal.get("sourceId") or "").strip()
            if source_id:
                counts[source_id] = counts.get(source_id, 0) + 1
    return counts


def build_source(record: dict[str, Any], *, brief_count: int = 0) -> dict[str, Any]:
    fields = record.get("fields") or {}
    source_id = str(sources.cell(fields.get("source_id")) or "").strip()
    status = normalize_status(fields.get("status"))
    return {
        "id": source_id,
        "recordId": str(record.get("record_id") or ""),
        "name": str(sources.cell(fields.get("name")) or source_id),
        "url": str(sources.cell(fields.get("endpoint")) or ""),
        "status": status,
        "statusLabel": STATUS_LABELS[status],
        "on": status == STATUS_ACTIVE,
        "format": sources.normalize_signal_format(fields.get("来源类型"))
        or sources.SIGNAL_FORMAT_OTHER,
        "type": sources.normalize_category(fields.get("dimension"), default="其他"),
        "tier": str(sources.cell(fields.get("tier")) or ""),
        "priority": normalize_priority(fields.get("priority")),
        "fetchMethod": str(sources.cell(fields.get("fetch_method")) or ""),
        "lookback": str(sources.cell(fields.get("lookback_window")) or ""),
        "last": _format_stamp(fields.get("最近采集时间")),
        "perDay": _int_cell(fields.get("条目数")),
        "briefCount": int(brief_count),
    }


def _text_cell(fields: dict[str, Any], key: str) -> str:
    return str(sources.cell(fields.get(key)) or "").strip()


def _regex_ok(pattern: str) -> bool:
    if not pattern:
        return True
    try:
        re.compile(pattern, re.I)
    except re.error:
        return False
    return True


def _parse_extra(text: str) -> dict[str, Any] | None:
    """解析 extra_config；存着坏 JSON 时返回 None，让调用方原样回显好让人修。"""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_detail(record: dict[str, Any]) -> dict[str, Any]:
    """本地配置台专用的完整配置视图。

    keyword_regex / min_content_chars / extra_config 这些筛选规则刻意不进
    build_payload——那份载荷会被 publish 导出成公开站点的 sources.json。这里是
    只在回环地址上提供的按需接口，公开快照里不存在，所以不会泄露筛选策略。
    """
    fields = record.get("fields") or {}
    extra_text = _text_cell(fields, "extra_config")
    extra = _parse_extra(extra_text)
    keyword_regex = _text_cell(fields, "keyword_regex")
    lookback = _text_cell(fields, "lookback_window")
    return {
        "recordId": str(record.get("record_id") or ""),
        "id": _text_cell(fields, "source_id"),
        "config": {
            "name": _text_cell(fields, "name"),
            "endpoint": _text_cell(fields, "endpoint"),
            "fetchMethod": _text_cell(fields, "fetch_method"),
            "format": sources.normalize_signal_format(fields.get("来源类型")) or "",
            "dimension": _text_cell(fields, "dimension"),
            "tier": _text_cell(fields, "tier"),
            "priority": normalize_priority(fields.get("priority")),
            "status": normalize_status(fields.get("status")),
            "lookbackWindow": lookback,
            "keywordRegex": keyword_regex,
            "minContentChars": _int_cell(fields.get("min_content_chars")),
            "dedupKey": _text_cell(fields, "dedup_key"),
            "extraConfig": extra_text,
            "notes": _text_cell(fields, "notes"),
        },
        # 采集时这份配置实际会被解读成什么。keyword_min_hits 和 title_exclude_regex
        # 藏在 extra_config 里，不摊开的话改一次要回代码里对一次。
        "effective": {
            "lookbackHours": sources.parse_lookback_hours(lookback),
            "keywordMinHits": max(1, int((extra or {}).get("keyword_min_hits") or 1)),
            "titleExcludeRegex": str((extra or {}).get("title_exclude_regex") or ""),
            "extraConfigValid": extra is not None,
            "keywordRegexValid": _regex_ok(keyword_regex),
        },
        "runtime": {
            "last": _format_stamp(fields.get("最近采集时间")),
            "perDay": _int_cell(fields.get("条目数")),
            "dupFiltered": _int_cell(fields.get("查重过滤")),
            "windowFiltered": _int_cell(fields.get("时间窗过滤")),
        },
        "meta": {
            "fetchMethods": list(FETCH_METHODS),
            "formats": list(FORMATS),
            "tiers": list(TIERS),
            "priorities": list(PRIORITY_ORDER),
            "statuses": [{"code": c, "label": STATUS_LABELS[c]} for c in STATUS_ORDER],
            "ruleFields": sorted(RULE_FIELDS),
        },
    }


def _clean_text(raw: Any, label: str, *, required: bool = False) -> str:
    text = str(raw or "").strip()
    if required and not text:
        raise ValueError(f"{label}不能为空")
    if len(text) > _MAX_TEXT:
        raise ValueError(f"{label}超长（{len(text)} 字符，上限 {_MAX_TEXT}）")
    return text


def _pick(raw: Any, allowed: tuple[str, ...], label: str) -> str:
    text = str(raw or "").strip()
    if text not in allowed:
        raise ValueError(f"{label}只能是 {' / '.join(allowed)}，收到「{text}」")
    return text


def normalize_lookback_window(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if not _LOOKBACK_RE.match(text) and text not in {"每日", "每周"}:
        raise ValueError(
            f"lookback_window 只认 24h / 7d 这类写法，收到「{text}」；"
            "认不出的值会被静默当成 168h，源看起来正常但时间窗是错的"
        )
    return text


def normalize_keyword_regex(raw: Any) -> str:
    text = _clean_text(raw, "keyword_regex")
    if not text:
        return ""
    try:
        re.compile(text, re.I)
    except re.error as exc:
        raise ValueError(
            f"keyword_regex 不是合法正则：{exc}；"
            "采集时坏正则会被静默换成默认关键词，整源的主题门等于失效"
        ) from exc
    return text


def normalize_extra_config(raw: Any) -> str:
    text = _clean_text(raw, "extra_config")
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"extra_config 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("extra_config 必须是 JSON 对象")
    title_exclude = parsed.get("title_exclude_regex")
    if title_exclude and not _regex_ok(str(title_exclude)):
        raise ValueError(f"extra_config.title_exclude_regex 不是合法正则：{title_exclude}")
    if "keyword_min_hits" in parsed:
        try:
            hits = int(parsed["keyword_min_hits"])
        except (TypeError, ValueError) as exc:
            raise ValueError("extra_config.keyword_min_hits 必须是整数") from exc
        if hits < 1:
            raise ValueError("extra_config.keyword_min_hits 至少为 1")
    # 存回单行紧凑 JSON，跟参数表里现有行的写法保持一致
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def normalize_min_content_chars(raw: Any) -> int:
    try:
        value = int(float(str(raw).strip() or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"min_content_chars 必须是整数，收到「{raw}」") from exc
    if value < 0 or value > 100000:
        raise ValueError(f"min_content_chars 超出合理范围（0-100000），收到 {value}")
    return value


def normalize_endpoint(raw: Any) -> str:
    text = _clean_text(raw, "采集地址", required=True)
    if not text.startswith(("http://", "https://")):
        raise ValueError(f"采集地址必须以 http:// 或 https:// 开头，收到「{text}」")
    return text


# api 字段名 -> (飞书字段名, 校验器)
_CONFIG_SPECS: dict[str, tuple[str, Any]] = {
    "name": ("name", lambda v: _clean_text(v, "来源名称", required=True)),
    "endpoint": ("endpoint", normalize_endpoint),
    "fetchMethod": ("fetch_method", lambda v: _pick(v, FETCH_METHODS, "fetch_method")),
    "format": ("来源类型", lambda v: _pick(v, FORMATS, "来源类型")),
    "dimension": ("dimension", lambda v: _clean_text(v, "dimension", required=True)),
    "tier": ("tier", lambda v: _pick(v, TIERS, "tier")),
    "lookbackWindow": ("lookback_window", normalize_lookback_window),
    "keywordRegex": ("keyword_regex", normalize_keyword_regex),
    "minContentChars": ("min_content_chars", normalize_min_content_chars),
    "dedupKey": ("dedup_key", lambda v: _clean_text(v, "dedup_key")),
    "extraConfig": ("extra_config", normalize_extra_config),
    "notes": ("notes", lambda v: _clean_text(v, "notes")),
}

CONFIG_KEYS = frozenset(_CONFIG_SPECS)


def _comparable(key: str, value: Any) -> Any:
    """比对用的等价形式：extra_config 的键顺序不影响采集行为，重排不算改动。"""
    if key == "extraConfig" and value:
        try:
            return json.dumps(json.loads(value), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return value
    return value


def normalize_config_patch(
    body: dict[str, Any], current: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """校验配置台提交的字段，返回 (飞书字段, 实际发生变化的 api 字段名)。

    校验一律从严：lookback_window 和 keyword_regex 认不出的值在采集时都会被静默
    回落成默认行为，这类错误在漏斗统计里跟「正常过滤」长得一模一样，事后极难查。
    与当前值相同的字段不写回，避免无意义的飞书写入和误触发状态降级。
    """
    fields: dict[str, Any] = {}
    changed: list[str] = []
    for key, (feishu_key, validator) in _CONFIG_SPECS.items():
        if key not in body:
            continue
        value = validator(body[key])
        # 当前值也过一遍同样的规范化再比：extra_config 在飞书里可能带空格，纯格式
        # 差异不该被当成规则改动去触发状态降级。现存的坏值过不了校验，视为有改动。
        raw_current = current.get(key)
        try:
            normalized_current = (
                validator(raw_current) if raw_current not in (None, "") else raw_current
            )
        except ValueError:
            normalized_current = object()
        if _comparable(key, value) == _comparable(key, normalized_current):
            continue
        changed.append(key)
        # endpoint 在参数表里是 URL 类型字段，写纯字符串会被飞书拒掉
        fields[feishu_key] = {"link": value, "text": value} if key == "endpoint" else value
    return fields, changed


def _ordered_values(items: list[str], preferred: tuple[str, ...] = ()) -> list[str]:
    seen: list[str] = [value for value in preferred if value in items]
    for value in items:
        if value and value not in seen:
            seen.append(value)
    return seen


def build_meta(source_list: list[dict[str, Any]], *, writable: bool = False) -> dict[str, Any]:
    return {
        "types": _ordered_values(sorted({s["type"] for s in source_list if s["type"]})),
        "formats": _ordered_values(sorted({s["format"] for s in source_list if s["format"]})),
        "priorities": list(PRIORITY_ORDER),
        "statuses": [
            {"code": code, "label": STATUS_LABELS[code]} for code in STATUS_ORDER
        ],
        "writable": list(WRITABLE_CAPABILITIES) if writable else [],
    }


def records_from_seed(seed_file: Path = SEED_FILE) -> list[dict[str, Any]]:
    """把仓库里的配置快照包成飞书记录形状。

    只用于本机没有飞书凭据时的预览：seed 由 export_seed 导出，运行时统计
    （最近采集时间、条目数）不在里面，所以那两列会是空的。
    """
    bundle = json.loads(seed_file.read_text(encoding="utf-8"))
    return [{"record_id": "", "fields": row} for row in bundle.get("一级参数") or []]


def build_payload(
    records: list[dict[str, Any]],
    *,
    briefs: list[dict[str, Any]] | None = None,
    writable: bool = False,
    origin: str = "feishu",
) -> dict[str, Any]:
    counts = brief_counts(briefs)
    source_list = [
        build_source(record, brief_count=counts.get(
            str(sources.cell((record.get("fields") or {}).get("source_id")) or "").strip(), 0
        ))
        for record in records
    ]
    source_list = [s for s in source_list if s["id"] or s["name"]]
    source_list.sort(
        key=lambda s: (
            STATUS_ORDER.index(s["status"]),
            PRIORITY_ORDER.index(s["priority"]),
            s["name"],
        )
    )
    return {
        "generatedAt": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        "origin": origin,
        "briefWindowDays": len(briefs or []),
        "sources": source_list,
        "meta": build_meta(source_list, writable=writable),
    }


def _local_briefs(data_dir: Path, limit: int = 7) -> list[dict[str, Any]]:
    briefs: list[dict[str, Any]] = []
    for path in sorted(data_dir.glob("brief-20*.json"), reverse=True)[:limit]:
        try:
            briefs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return briefs


def run() -> int:
    """本地生成 site/data/sources.json；没有飞书凭据时退回仓库里的配置快照。"""
    import argparse

    from . import feishu

    parser = argparse.ArgumentParser(description="导出数据源页面用的 sources.json")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "site" / "data" / "sources.json"))
    parser.add_argument("--seed", action="store_true", help="跳过飞书，直接用 seed_default.json")
    args = parser.parse_args()
    out = Path(args.out)
    origin = "feishu"
    if args.seed:
        records, origin = records_from_seed(), "seed"
    else:
        try:
            records = feishu.read_param_records(feishu.get_tenant_access_token())
        except Exception as exc:  # noqa: BLE001 - 本地预览允许降级，但要说清楚
            print(f"读取飞书失败（{exc}），改用仓库配置快照，采集统计列会是空的")
            records, origin = records_from_seed(), "seed"
    payload = build_payload(records, briefs=_local_briefs(out.parent), origin=origin)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out} · {len(payload['sources'])} 个源 · 来源 {origin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
