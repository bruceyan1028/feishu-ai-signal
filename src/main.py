"""真实 RSS 情报采集：读配置、抓取、清洗去重并写入飞书。"""
from __future__ import annotations

import logging

import requests

from . import config, feishu, process, rss, sources, typed_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ingest")


def _trigger_dify(items: list[dict]) -> None:
    if not config.DIFY_WEBHOOK_URL:
        return
    for item in items:
        try:
            requests.post(
                config.DIFY_WEBHOOK_URL,
                json=process.build_dify_payload(item),
                timeout=30,
            )
        except requests.RequestException as exc:
            log.warning("Dify 触发失败 %s: %s", item.get("url"), exc)


def filter_new_items(cleaned: list[dict], existing: set[str]) -> list[dict]:
    """跨轮去重后按质量分优先截断 arXiv，避免低质论文占满本轮名额。"""
    seen_this_run: set[str] = set()
    non_arxiv: list[dict] = []
    arxiv_items: list[dict] = []
    for item in cleaned:
        key = str(item.get("duplicate_key") or "").strip()
        if key and (key in existing or key in seen_this_run):
            continue
        if key:
            seen_this_run.add(key)
        is_arxiv = str(item.get("source_id") or "").startswith("arxiv-") or "arxiv.org/" in str(
            item.get("url") or ""
        )
        if is_arxiv:
            arxiv_items.append(item)
        else:
            non_arxiv.append(item)

    arxiv_items.sort(
        key=lambda it: (
            -float(it.get("quality_score") or 0),
            -int(it.get("published_ms") or 0),
        )
    )
    return non_arxiv + arxiv_items[: config.MAX_ARXIV_ITEMS]


def run() -> int:
    config.validate()

    token = feishu.get_tenant_access_token()
    log.info("已获取飞书 tenant_access_token")
    feishu.ensure_entry_enrichment_fields(token)
    try:
        feishu.ensure_paper_config_fields(token)
    except feishu.FeishuError as exc:
        log.warning("补齐论文配置字段失败: %s", exc)
    try:
        feishu.ensure_source_type_field(token, config.FEISHU_PARAM_TABLE_ID)
        feishu.ensure_source_type_field(token, config.FEISHU_SOURCE_TABLE_ID)
    except feishu.FeishuError as exc:
        log.warning("补齐来源类型字段失败: %s", exc)

    records = feishu.read_param_records(token)
    log.info("读到源配置 %d 条", len(records))

    type_configs = typed_config.load_typed_configs(token)
    log.info("加载类型筛选配置：命中 %d 个源", len(type_configs))

    feed_sources = sources.map_feed_sources(records)
    for feed in feed_sources:
        cfg = type_configs.get(feed.get("id") or "") or {}
        feed["source_type"] = sources.infer_signal_format(
            feed.get("id") or "",
            endpoint=feed.get("url") or "",
            extra=feed.get("extra_config"),
            fetch_method=feed.get("fetch_method") or "",
            entity_type=cfg.get("entity_type"),
            explicit_type=feed.get("source_type"),
        )
    paper_n = sum(1 for f in feed_sources if f.get("source_type") == sources.SIGNAL_FORMAT_PAPER)
    log.info("启用的 RSS 源 %d 个（其中论文 %d）", len(feed_sources), paper_n)

    raw_items: list[dict] = []
    if feed_sources:
        raw_items += rss.fetch_feed_sources(feed_sources)
    log.info("抓取到原始条目 %d 条", len(raw_items))

    drop_stats: dict[str, int] = {}
    cleaned = process.process_and_clean(raw_items, type_configs, drop_stats)
    log.info("清洗过滤后 %d 条", len(cleaned))

    existing = feishu.read_existing_dedup_keys(token)
    log.info("飞书已存去重键 %d 个", len(existing))

    new_items = filter_new_items(cleaned, existing)
    log.info(
        "跨轮去重后待入库 %d 条（清洗 %d → 去重/截断后 %d）",
        len(new_items),
        len(cleaned),
        len(new_items),
    )
    arxiv_in = sum(
        1
        for it in new_items
        if str(it.get("source_id") or "").startswith("arxiv-")
        or "arxiv.org/" in str(it.get("url") or "")
    )
    if arxiv_in:
        log.info("其中 arXiv %d 条（上限 %d）", arxiv_in, config.MAX_ARXIV_ITEMS)

    # 回写本轮采集统计（最近采集时间 / 条目数 / 查重过滤；即使 0 条入库也要写）
    attempted_ids = {f["id"] for f in feed_sources}
    try:
        feishu.sync_param_collect_stats(
            token,
            records,
            attempted_ids,
            cleaned,
            new_items,
            drop_stats,
        )
    except feishu.FeishuError as exc:
        log.warning("回写源采集统计失败: %s", exc)

    if not new_items:
        log.info("全部已入库，结束")
        return 0

    fields_list = [process.format_for_feishu(item) for item in new_items]
    created = feishu.batch_create_records(token, fields_list)
    log.info("写入飞书完成，共 %d 条", created)

    _trigger_dify(new_items)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
