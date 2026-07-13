"""真实 RSS 情报采集：读配置、抓取、清洗去重并写入飞书。"""
from __future__ import annotations

import logging
from collections import Counter

import requests

from . import config, feishu, process, rss, sources

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
    """跨轮去重后再限制 arXiv，避免旧论文占满本轮名额。"""
    seen_this_run: set[str] = set()
    arxiv_count = 0
    new_items: list[dict] = []
    for item in cleaned:
        key = str(item.get("duplicate_key") or "").strip()
        if key and (key in existing or key in seen_this_run):
            continue
        is_arxiv = str(item.get("source_id") or "").startswith("arxiv-") or "arxiv.org/" in str(item.get("url") or "")
        if is_arxiv and arxiv_count >= config.MAX_ARXIV_ITEMS:
            continue
        if key:
            seen_this_run.add(key)
        arxiv_count += int(is_arxiv)
        new_items.append(item)
    return new_items


def run() -> int:
    config.validate()

    token = feishu.get_tenant_access_token()
    log.info("已获取飞书 tenant_access_token")
    feishu.ensure_entry_enrichment_fields(token)

    records = feishu.read_param_records(token)
    log.info("读到源配置 %d 条", len(records))

    feed_sources = sources.map_feed_sources(records)
    log.info("启用的 RSS 源 %d 个", len(feed_sources))

    raw_items: list[dict] = []
    if feed_sources:
        raw_items += rss.fetch_feed_sources(feed_sources)
    log.info("抓取到原始条目 %d 条", len(raw_items))

    cleaned = process.process_and_clean(raw_items)
    log.info("清洗过滤后 %d 条", len(cleaned))

    existing = feishu.read_existing_dedup_keys(token)
    log.info("飞书已存去重键 %d 个", len(existing))

    new_items = filter_new_items(cleaned, existing)
    log.info("跨轮去重后待入库 %d 条", len(new_items))

    # 采集健康度回写：本轮尝试过的每个源，记录 通过/最近采集时间/条目数/查重过滤
    # 条目数 = 最终录入数；查重过滤 = 采集到但因已存在被去重的数量（采集到 - 最终录入）
    attempted_ids = {f["id"] for f in feed_sources}
    cleaned_counts = Counter(str(it.get("source_id") or "") for it in cleaned)
    final_counts = Counter(str(it.get("source_id") or "") for it in new_items)
    id_to_record: dict[str, str] = {}
    for rec in records:
        sid = sources.cell((rec.get("fields") or {}).get("source_id"))
        if sid:
            id_to_record[str(sid).strip()] = rec.get("record_id")
    try:
        feishu.update_param_collect_stats(
            token, attempted_ids, cleaned_counts, final_counts, id_to_record
        )
    except feishu.FeishuError as exc:
        log.warning("回写源采集健康度失败: %s", exc)

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
