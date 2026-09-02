"""逐源 capture_spec：LLM 首筛产出的每源定制抓取规格。

背景：抓取链路原本是「通用兜底」——列表页链接用通用正则抽、日期用六级 cascade
猜、标题用 <title>/<h1> 试探。对绝大多数源够用，但对个别源（列表页是 JS 渲染、
日期藏在 jsonld、标题在 <h2>）要么经常抓空，要么日期识别失败整源被判无日期。

capture_spec 把每个源的「结构」在接入时确认一次，之后采集直接读它走定制分支，
不再依赖通用兜底。spec 由 src/probe.py（LLM 首筛 / 手动重筛）生成，存进
git 跟踪的 src/capture_specs.json——需要可 diff、可回滚、与代码同 commit。

**spec 管「去哪里抓」，不管「什么是 AI 信号」。** 标题/正文清洗、关键词闸、打分
一律照旧走 process.process_and_clean。spec 不能替代那一层过滤。

本模块只做加载 / 读写 / 确定性校验 / 选项解析，**不含 LLM**——LLM 在 src/probe.py。
"""  # noqa: E501
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SPEC_FILE = Path(__file__).resolve().parent / "capture_specs.json"

# spec 缺失/坏文件时直接抛，不在采集时静默退化成「没有 spec」——否则改版后
# 该源会在无人知晓的情况下退回通用兜底。
class CaptureSpecError(RuntimeError):
    """capture_specs.json 缺失或不是合法 JSON。"""


def _empty_spec() -> dict[str, Any]:
    return {
        "version": 0,
        "probed_at": "",
        "enabled": False,
        "route": {},
        "list": {},
        "date": {},
        "expect": {},
    }


def normalize_spec(value: Any) -> dict[str, Any]:
    """把一道裸 spec 补全成完整结构。缺字段用空值兜底，不报错。

    宽松处理：LLM 可能只返回 route.date.selector 没返回 route.list，这是合法的，
    缺的部分继续走通用兜底。只有顶层不是对象才判非法。
    """
    if not isinstance(value, dict):
        raise CaptureSpecError(f"capture_spec 条目应是对象，拿到 {type(value).__name__}")
    spec = _empty_spec()
    for key in ("version", "probed_at", "enabled", "route", "list", "date", "expect"):
        if key in value and value[key] is not None:
            spec[key] = value[key]
    # _auto：自动重筛的熔断计数（spec_recheck 维护），随 spec 一起持久化
    if isinstance(value.get("_auto"), dict):
        spec["_auto"] = dict(value["_auto"])
    # enabled 默认取「任何 route 配置存在」；显式 false 才关闭
    if "enabled" not in value:
        spec["enabled"] = bool(spec["route"])
    return spec


def load(path: Path | str = SPEC_FILE) -> dict[str, Any]:
    """读全量 spec 表：{source_id: spec}。

    文件缺失返回空 dict（还没有源接入过 spec），坏 JSON 抛 CaptureSpecError。
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CaptureSpecError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise CaptureSpecError(f"{path} 顶层应是对象，拿到 {type(raw).__name__}")
    return {source_id: normalize_spec(value) for source_id, value in raw.items()}


def save(specs: dict[str, Any], path: Path | str = SPEC_FILE) -> None:
    """写回全量 spec 表：紧凑单行 JSON，保留键顺序。

    probe --apply 与自动重筛后调用，随后由调用方负责 git add/commit。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(specs, ensure_ascii=False, indent=2, separators=(",", ": "))
    path.write_text(text + "\n", encoding="utf-8")


def spec_for(source_id: str, specs: dict[str, Any]) -> dict[str, Any] | None:
    """取某源 spec；未接入 / enabled 为 false / 无任何 route 配置 → 返回 None。"""
    value = specs.get(source_id)
    if value is None:
        return None
    spec = normalize_spec(value)
    if not spec["enabled"]:
        return None
    route = spec.get("route") or {}
    if not (route.get("list") or route.get("date") or route.get("article")):
        return None
    return spec


def _expect_ok(actual: int, expected: Any) -> bool:
    """实际值 vs 期望值：期望缺失/非数/≤0 都视为「无此约束」。"""
    if expected is None:
        return True
    try:
        expected = float(expected)
    except (TypeError, ValueError):
        return True
    if expected <= 0:
        return True
    return actual >= expected


def find_violations(
    fetch_stats: dict[str, Any],
    sources: dict[str, Any],
    specs: dict[str, Any],
    funnel: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """确定性校验：run() 收尾比对每条 attempted 源的实际产出 vs spec 期望值。

    这是「改版即报错 → 触发重筛」的依据。**无 LLM**，纯字段比对，因此可以放进
    每日构建而不烧钱。判定三类违规，都会写进 fetch.error 或单独记录：

      1. 源级 error 已是 spec_mismatch（抓取/抽链阶段已判）
      2. 抽到链接数 < spec.expect.min_links
      3. 日期命中率 < spec.expect.date_min_ratio（用清洗漏斗的
         missing_or_invalid_date 计数算：kept / (kept + missing_or_invalid_date)）

    returns: [{source_id, violated: [...], expected, actual}]
    """
    violations: list[dict[str, Any]] = []
    for source_id, stat in fetch_stats.items():
        raw_spec = specs.get(source_id)
        if raw_spec is None:
            continue
        spec = normalize_spec(raw_spec)
        if not spec["enabled"]:
            continue
        expect = spec.get("expect") or {}
        expect = expect if isinstance(expect, dict) else {}

        violated: list[str] = []
        actual: dict[str, Any] = {}

        error = str((stat or {}).get("error") or "")
        if error == "spec_mismatch":
            violated.append("spec_mismatch")

        min_links = expect.get("min_links")
        if min_links:
            links = int((stat or {}).get("links") or 0)
            actual["links"] = links
            if not _expect_ok(links, min_links):
                violated.append("min_links")

        date_min_ratio = expect.get("date_min_ratio")
        # 只有该源在漏斗里确实有记录，才判日期命中率；没记录 = 本轮没这源的数据，
        # 不能当成「命中率 0」——否则没有漏斗数据的源会被误判违规。
        if date_min_ratio and source_id in (funnel or {}):
            kept = int((funnel or {}).get(source_id, {}).get("kept") or 0)
            date_miss = int(
                (funnel or {}).get(source_id, {}).get("missing_or_invalid_date") or 0
            )
            total = kept + date_miss
            ratio = kept / total if total else 0.0
            actual["date_hit_ratio"] = round(ratio, 3)
            if ratio < float(date_min_ratio):
                violated.append("date_min_ratio")

        if violated:
            violations.append(
                {
                    "source_id": source_id,
                    "violated": violated,
                    "expected": expect,
                    "actual": actual,
                }
            )
    return violations
