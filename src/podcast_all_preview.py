"""生成不受时间窗口限制的全播客源本地预览。"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, daily, feishu, podcast, process, publish, sources

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def _existing_podcast_signals(token: str) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    for record in feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID):
        fields = record.get("fields") or {}
        if daily.content_type(fields) != "播客":
            continue
        source_id = str(sources.cell(fields.get("source_id")) or "")
        title = str(sources.cell(fields.get("标题")) or "")
        result[(source_id, title)] = publish._signal_from_record(record)
    return result


def _preview_signal(item: dict[str, Any], source_id: str) -> dict[str, Any]:
    fields = process.format_for_feishu(item)
    signal = publish._signal_from_record(
        {"record_id": f"preview-{source_id}", "fields": fields}
    )
    signal["recordId"] = f"preview-{source_id}"
    return signal


def _prepare_item(item: dict[str, Any], feed: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(item)
    title = str(prepared.get("title") or "")
    url = str(prepared.get("url") or "")
    published_ms = process.parse_date_ms(prepared.get("published_raw"))
    prepared.update(
        {
            "source": str(feed.get("name") or feed.get("id") or "播客"),
            "source_id": str(feed.get("id") or ""),
            "source_type": sources.SIGNAL_FORMAT_PODCAST,
            "fetch_method": "Podcast",
            "category": str(feed.get("category") or "其他"),
            "tier": str(feed.get("tier") or "L4"),
            "published_ms": published_ms or int(time.time() * 1000),
            "collected_ms": int(time.time() * 1000),
            "raw_content": process.strip_html_body(prepared.get("body") or ""),
            "duplicate_key": process.build_dedup_key(url, title, prepared.get("feed") or feed),
        }
    )
    return prepared


def generate(output: Path | str) -> dict[str, Any]:
    config.validate()
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    feeds = sources.map_podcast_sources(records, allow_experimental=True)
    existing = _existing_podcast_signals(token)
    signals = []
    statuses = []
    for feed in feeds:
        source_id = str(feed.get("id") or "")
        name = str(feed.get("name") or source_id)
        preview_feed = {
            **feed,
            "extra_config": {
                **(feed.get("extra_config") or {}),
                "max_items": 1,
            },
        }
        try:
            items = podcast.fetch_source(preview_feed)
        except Exception as exc:  # noqa: BLE001 - 单源失败应显示在预览摘要中
            statuses.append(
                {
                    "sourceId": source_id,
                    "name": name,
                    "ok": False,
                    "error": str(exc),
                }
            )
            continue
        if not items:
            statuses.append(
                {
                    "sourceId": source_id,
                    "name": name,
                    "ok": False,
                    "error": "RSS 无有效音频条目",
                }
            )
            continue
        item = _prepare_item(items[0], feed)
        title = str(item.get("title") or "")
        cached = existing.get((source_id, title))
        if cached:
            signals.append(cached)
            statuses.append(
                {"sourceId": source_id, "name": name, "ok": True, "mode": "cached"}
            )
            continue
        enriched, stats = podcast.enrich_podcast_items([item])
        if not enriched:
            statuses.append(
                {
                    "sourceId": source_id,
                    "name": name,
                    "ok": False,
                    "error": f"摘要生成失败：{stats}",
                }
            )
            continue
        signals.append(_preview_signal(enriched[0], source_id))
        statuses.append(
            {"sourceId": source_id, "name": name, "ok": True, "mode": "preview"}
        )

    signals.sort(
        key=lambda signal: (
            int(signal.get("impact") or 0),
            str(signal.get("publishedDate") or ""),
        ),
        reverse=True,
    )
    ok_count = sum(bool(item["ok"]) for item in statuses)
    failed = [item["name"] for item in statuses if not item["ok"]]
    payload = {
        "date": "podcast-all",
        "title": "播客源全量预览",
        "intro": (
            f"不限制节目发布时间；{len(feeds)} 个已配置播客源中，"
            f"{ok_count} 个成功生成最新一期预览。"
        ),
        "bullets": [
            {
                "title": "覆盖情况",
                "text": f"成功预览 {ok_count}/{len(feeds)} 个播客源。",
                "refs": [],
            },
            {
                "title": "异常来源",
                "text": (
                    f"连接或摘要失败：{'、'.join(failed)}。"
                    if failed
                    else "全部播客源均可读取。"
                ),
                "refs": [],
            },
            {
                "title": "预览说明",
                "text": "本页仅供本地验收，不改变正式简报的时间窗口和入库状态。",
                "refs": [],
            },
        ],
        "signals": signals,
        "podcastSourceStatus": statuses,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default=str(ROOT / "site" / "data" / "brief-podcast-all.json")
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    payload = generate(args.output)
    log.info("播客预览已生成：%d 条 → %s", len(payload["signals"]), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
