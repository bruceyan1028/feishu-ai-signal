"""真实情报采集：读配置、抓取多类信号、清洗去重并写入飞书。"""
from __future__ import annotations

import argparse
import logging

import requests

from . import (
    config,
    feishu,
    podcast,
    process,
    rss,
    scrape,
    social,
    sources,
    typed_config,
    video,
)

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


def _drop_still_too_short(items: list[dict]) -> list[dict]:
    """复判内容长度：清洗阶段放行的摘要型条目，补全后仍太短就丢掉。"""
    kept, dropped = [], 0
    for item in items:
        if item.get("needs_fulltext") and len(str(item.get("raw_content") or "")) < int(
            item.get("min_content_chars") or 0
        ):
            dropped += 1
            continue
        kept.append(item)
    if dropped:
        log.info("补全后仍过短丢弃 %d 条", dropped)
    return kept


def _prepare_scrape_sources(
    *, feishu_records: list[dict], type_configs: dict
) -> list[dict]:
    """筛出正式 Scrape 源并补齐抽取所需的类型/榜单参数。"""
    feeds = sources.map_scrape_sources(feishu_records)
    for feed in feeds:
        cfg = type_configs.get(feed.get("id") or "") or {}
        if cfg.get("entity_type"):
            feed["source_type"] = sources.infer_signal_format(
                feed.get("id") or "",
                endpoint=feed.get("url") or "",
                extra=feed.get("extra_config"),
                fetch_method="Scrape",
                entity_type=cfg.get("entity_type"),
                explicit_type=feed.get("source_type"),
            )
        if cfg.get("entity_type") == "github":
            feed["github_config"] = cfg.get("params") or {}
        feed["cohort"] = sources.scrape_cohort(
            str(feed.get("id") or ""),
            category=str(feed.get("category") or ""),
            url=str(feed.get("url") or ""),
        )
    return feeds


def run(methods: set[str] | None = None) -> int:
    enabled = methods or {"RSS", "Scrape", "Media", "Social"}
    config.validate()

    token = feishu.get_tenant_access_token()
    log.info("已获取飞书 tenant_access_token")
    feishu.ensure_entry_enrichment_fields(token)
    try:
        feishu.ensure_paper_config_fields(token)
    except feishu.FeishuError as exc:
        log.warning("补齐论文配置字段失败: %s", exc)
    try:
        feishu.ensure_social_config_fields(token)
    except feishu.FeishuError as exc:
        log.warning("补齐社媒配置字段失败: %s", exc)
    try:
        feishu.ensure_source_type_field(token, config.FEISHU_PARAM_TABLE_ID)
        feishu.ensure_source_type_field(token, config.FEISHU_SOURCE_TABLE_ID)
        feishu.ensure_select_option(token, config.FEISHU_PARAM_TABLE_ID, "fetch_method", "Social")
        feishu.ensure_select_option(token, config.FEISHU_SOURCE_TABLE_ID, "获取方式", "Social")
        feishu.ensure_select_option(token, config.FEISHU_ENTRY_TABLE_ID, "路由来源", "Social")
        feishu.ensure_select_option(token, config.FEISHU_PARAM_TABLE_ID, "fetch_method", "Podcast")
        feishu.ensure_select_option(token, config.FEISHU_SOURCE_TABLE_ID, "获取方式", "Podcast")
        feishu.ensure_select_option(token, config.FEISHU_ENTRY_TABLE_ID, "路由来源", "Podcast")
    except feishu.FeishuError as exc:
        log.warning("补齐来源类型/采集方式字段失败: %s", exc)

    records = feishu.read_param_records(token)
    log.info("读到源配置 %d 条", len(records))

    type_configs = typed_config.load_typed_configs(token)
    log.info("加载类型筛选配置：命中 %d 个源", len(type_configs))

    feed_sources = sources.map_feed_sources(records) if "RSS" in enabled else []
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

    scrape_sources = (
        _prepare_scrape_sources(feishu_records=records, type_configs=type_configs)
        if "Scrape" in enabled
        else []
    )
    log.info("启用的 Scrape 源 %d 个", len(scrape_sources))

    media_sources = sources.map_media_sources(records) if "Media" in enabled else []
    log.info("启用的 Media 视频源 %d 个", len(media_sources))

    podcast_sources = sources.map_podcast_sources(records) if "Podcast" in enabled else []
    log.info("启用的 Podcast 源 %d 个", len(podcast_sources))

    social_params = {
        source_id: cfg.get("params") or {}
        for source_id, cfg in type_configs.items()
        if cfg.get("entity_type") == "social"
    }
    social_sources = (
        sources.map_social_sources(records, social_params)
        if "Social" in enabled
        else []
    )
    log.info("启用的 Social 源 %d 个（账号 %d 个）", len(social_sources), sum(
        len(feed.get("accounts") or []) for feed in social_sources
    ))

    raw_items: list[dict] = []
    social_batch = social.SocialFetchBatch()
    if feed_sources:
        raw_items += rss.fetch_feed_sources(feed_sources)
    if scrape_sources:
        # 官方博客（Anthropic/Meta 等）与中文站点只有 Scrape 一条路，缺了它们
        # 重大发布只能靠公众号转述进来
        try:
            raw_items += scrape.fetch_scrape_sources(scrape_sources, engine="auto")
        except Exception as exc:  # noqa: BLE001 - 与 RSS 一致：单条链路失败不拖垮整轮
            log.warning("Scrape 采集失败，本轮跳过：%s", exc)
    if media_sources:
        try:
            raw_items += video.fetch_video_sources(media_sources)
        except Exception as exc:  # noqa: BLE001 - 不让视频接口故障拖垮其它来源
            log.warning("Media 视频采集失败，本轮跳过：%s", exc)
    if podcast_sources:
        raw_items += podcast.fetch_podcast_sources(podcast_sources)
    if social_sources:
        try:
            social_batch = social.fetch_social_sources(social_sources)
            recent_records = feishu.read_all_records(
                token,
                config.FEISHU_ENTRY_TABLE_ID,
                ["标题", "原文", "来源", "发布时间"],
            )
            recent_texts, account_counts = social.recent_context(recent_records)
            social_items, social_stats = social.filter_social_items(
                social_batch.items,
                recent_texts=recent_texts,
                existing_account_counts=account_counts,
            )
            raw_items += social_items
            log.info(
                "Social 筛选漏斗 %s；API 读取 %d 条",
                social_stats,
                sum(social_batch.read_counts.values()),
            )
        except Exception as exc:  # noqa: BLE001 - 不让社媒接口故障拖垮其它来源
            log.warning("Social 采集失败，本轮跳过：%s", exc)
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
    # 播客只对跨轮去重后的新 episode 做长音频转录和分层摘要，避免重复付费。
    new_items, podcast_stats = podcast.enrich_podcast_items(new_items)
    if podcast_stats:
        log.info("Podcast 处理漏斗 %s", podcast_stats)
    # 只对确定入库的普通条目回源补全正文：RSS 常常只给一段摘要。
    rss.backfill_full_text(new_items)
    new_items = _drop_still_too_short(new_items)

    arxiv_in = sum(
        1
        for it in new_items
        if str(it.get("source_id") or "").startswith("arxiv-")
        or "arxiv.org/" in str(it.get("url") or "")
    )
    if arxiv_in:
        log.info("其中 arXiv %d 条（上限 %d）", arxiv_in, config.MAX_ARXIV_ITEMS)

    # 回写本轮采集统计（最近采集时间 / 条目数 / 查重过滤；即使 0 条入库也要写）
    attempted_ids = (
        {f["id"] for f in feed_sources}
        | {f["id"] for f in scrape_sources}
        | {f["id"] for f in media_sources}
        | {f["id"] for f in podcast_sources}
        | {f["id"] for f in social_sources}
    )
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
        if social_batch.cursor_states:
            feishu.update_social_cursor_states(token, social_sources, social_batch.cursor_states)
        log.info("全部已入库，结束")
        return 0

    fields_list = [process.format_for_feishu(item) for item in new_items]
    created = feishu.batch_create_records(token, fields_list)
    log.info("写入飞书完成，共 %d 条", created)
    if social_batch.cursor_states:
        feishu.update_social_cursor_states(token, social_sources, social_batch.cursor_states)

    _trigger_dify(new_items)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="执行正式信号采集")
    parser.add_argument(
        "--method",
        action="append",
        choices=["RSS", "Scrape", "Media", "Social", "Podcast"],
        help="只运行指定采集方式，可重复传入；默认运行全部",
    )
    args = parser.parse_args()
    raise SystemExit(run(set(args.method or [])))
