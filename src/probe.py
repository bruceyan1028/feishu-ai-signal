"""逐源 capture_spec 的 LLM 首筛 / 重筛工具（管理员或自动重筛入口）。

第一次接入一个源，或在源改版导致 spec_check 报错后，跑本工具让 LLM 看一遍
列表页 + 采样文章，产出该源的定制抓取规格（列表链接 selector、日期字段位置、
正文/标题 selector、期望产出），存进 git 跟踪的 src/capture_specs.json。

含一次确定性验证：LLM 给出的 selector 会在真实页面上跑一遍，命中率达不到
expect 就判定「该源尚不合格」，--apply 时只写 spec，status 降级交给调用方。

用法：
  python -m src.probe --source anthropic-news            # 只探测+LLM生成，不写
  python -m src.probe --source anthropic-news --apply    # 写回 capture_specs.json
  python -m src.probe --source anthropic-news --all      # 全部已接入源体检（供管理员）

说明：本工具只读页面生成 spec，不触发采集、不写飞书 status。自动重筛由
spec_recheck 调用本工具 --apply 后，另行把违规源 status 降级为 experimental。
"""  # noqa: E501
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import capture_spec, config, scrape  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("probe")

# 验收不过（selector 在真实页面抽不到足够链接）。--apply 时 spec 仍写回供人复核，
# 但退出码非 0，好让 spec_recheck 据此累计熔断次数。
EXIT_SPEC_REJECTED = 4

_LLM_SYSTEM = (
    "你是信息源抓取链路的结构分析助手。给你一段列表页 HTML 与一篇样文，"
    "你输出该源「从哪里抓」的定制规格 JSON。只判断结构，不做主题筛选，"
    "不判断内容是否为 AI 信号。"
)


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
    config.LLM_API_KEY = os.environ.get("LLM_API_KEY", config.LLM_API_KEY).strip()
    config.LLM_BASE_URL = os.environ.get("LLM_BASE_URL", config.LLM_BASE_URL).strip() or config.LLM_BASE_URL
    config.LLM_MODEL = os.environ.get("LLM_MODEL", config.LLM_MODEL).strip() or config.LLM_MODEL


def _fetch_list_html(feed: dict[str, Any]) -> str:
    """取列表页 HTML（直连优先，绕过 Jina，让 LLM 看到原始结构）。"""
    extra = feed.get("extra_config") or {}
    force_direct = bool(extra.get("force_direct"))
    resp = scrape._safe_direct_get(feed.get("url") or "")
    if resp or force_direct:
        return resp
    return scrape._safe_jina_get(feed.get("url") or "", True)


def _build_prompt(source_id: str, list_html: str, sample_links: list[dict[str, str]]) -> str:
    sample = "\n".join(
        f"- {link.get('title')}  (url: {link.get('url')})" for link in sample_links[:6]
    )
    prompt = (
        "源 source_id = " + source_id + "\n"
        "列表页 HTML（已截断）：\n" + list_html[:3000] + "\n\n"
        "从列表页抽到的链接样本（供你反推 selector）：\n" + sample + "\n\n"
        "请输出 JSON，字段：\n"
        '  {"route": {"list": {"selector": "CSS 选择列表链接容器, 如 .card a", '
        '"min_links": 5}, "date": {"selector": "CSS 选择日期元素, 可为空串", '
        '"fallback": "meta 或 jsonld 或 time 或 空"}, "article": {"title": '
        '"h1 或 title 选择器", "body": "article 或 main 选择器"}}, '
        '"expect": {"min_links": 5, "date_min_ratio": 0.8}}\n'
        "如果页面没找到列表链接，返回 {\"route\": {}, \"expect\": {}}。"
        "只输出 JSON，不要额外文字。"
    )
    return prompt


def _call_llm(prompt: str) -> dict[str, Any]:
    from src import report  # 延迟导入，保持 probe 可独立于内容处理链

    try:
        return report._llm_json(prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM 调用失败：%s", exc)
        return {}


def _validate_spec(source_id: str, spec: dict[str, Any], feed: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 产物落成可用 spec，并在真实列表页上验收一次。

    返回 (spec, 是否合格)。不合格仅记录，调用方决定写不写。
    """
    _load_dotenv()
    base = capture_spec.normalize_spec(spec)
    route = base.get("route") or {}
    expect = base.get("expect") or {}
    expect = expect if isinstance(expect, dict) else {}
    # LLM 可能漏给 min_links，用默认 5 兜底
    base.setdefault("expect", {})
    base["expect"].setdefault("min_links", 5)
    base["expect"].setdefault("date_min_ratio", 0.8)

    list_html = _fetch_list_html(feed)
    if not list_html:
        return base, False

    # 用 spec 的 list selector 实测抽链
    list_sel = ((route.get("list") or {}).get("selector")) if isinstance(route.get("list"), dict) else None
    if list_sel:
        try:
            from lxml import html as lh

            doc = lh.fromstring(list_html)
            anchors = doc.cssselect(list_sel) if hasattr(doc, "cssselect") else []
            actual = len(anchors)
        except Exception as exc:  # noqa: BLE001
            log.warning("spec list selector %r 不可用：%s", list_sel, exc)
            actual = 0
    else:
        actual = 0
    ok = actual >= int(base["expect"].get("min_links", 5))
    base["_validated"] = {"actual_links": actual, "ok": ok}
    if not ok:
        log.warning("源 %s 的 spec 在列表页实测仅抽到 %d 条链接（期望 ≥%d），判不合格", source_id, actual, base["expect"].get("min_links"))
    else:
        log.info("源 %s spec 验收通过（列表页抽到 %d 条链接）", source_id, actual)
    return base, ok


def _run_single(source_id: str, feed: dict[str, Any], *, apply: bool) -> int:
    """对单个源跑一遍探测 + LLM 生成 + 验证 + 可选写回。"""
    _load_dotenv()
    from src import capture_spec as cs

    list_html = _fetch_list_html(feed)
    if not list_html:
        log.error("源 %s 列表页抓取失败，无法探测", source_id)
        return 1
    # 复用生产抽链抓样本，保证「探的就是抓的」
    links = scrape._extract_links_for_feed(list_html, feed, use_jina=False)
    sample = links[:6]

    prompt = _build_prompt(source_id, list_html, sample)
    raw = _call_llm(prompt)
    log.info("LLM 返回 spec：%s", json.dumps(raw, ensure_ascii=False)[:300])
    if not raw:
        log.error("源 %s LLM 未产出可用 spec", source_id)
        return 1

    spec, ok = _validate_spec(source_id, raw, feed)
    if apply:
        specs = cs.load()
        spec.pop("_validated", None)
        # 保留 spec_recheck 维护的熔断计数，别被新 spec 覆盖掉
        prev = specs.get(source_id) or {}
        if isinstance(prev.get("_auto"), dict):
            spec["_auto"] = prev["_auto"]
        spec["version"] = int(prev.get("version") or 0) + 1
        spec["probed_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        specs[source_id] = spec
        cs.save(specs)
        log.info("已写回 %s", cs.SPEC_FILE)
    if not ok:
        log.warning("源 %s spec 未通过验收，%s", source_id, "已写回待人工复核" if apply else "未写回")
        return EXIT_SPEC_REJECTED
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逐源 capture_spec LLM 首筛/重筛")
    parser.add_argument("--source", help="源 id，如 anthropic-news")
    parser.add_argument("--apply", action="store_true", help="写回 capture_specs.json")
    parser.add_argument("--all", action="store_true", help="对所有 Scrape 源体检（需 --source 忽略或并行）")
    args = parser.parse_args(argv)

    if not args.source:
        parser.error("--source 必填（或 --all）")

    from src import config_store, sources

    try:
        records = config_store.read_param_records()
        type_configs = config_store.load_typed_configs()
    except Exception as exc:  # noqa: BLE001
        log.error("读仓库配置失败（当前 probe 只支持 seed 配置）：%s", exc)
        return 2

    feeds = sources.map_scrape_sources_for_diag(
        records, include_b_class=True, allow_experimental=True
    )
    matched = [f for f in feeds if f.get("id") == args.source]
    if not matched:
        log.error("找不到 Scrape 源 %r", args.source)
        return 2
    feed = matched[0]
    cfg = type_configs.get(args.source) or {}
    if cfg.get("entity_type"):
        feed["source_type"] = sources.infer_signal_format(
            args.source,
            endpoint=feed.get("url") or "",
            entity_type=cfg.get("entity_type"),
            fetch_method=feed.get("fetch_method") or "",
        )
    return _run_single(args.source, feed, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
