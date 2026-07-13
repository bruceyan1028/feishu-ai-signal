"""生成真实每日情报简报，并将分析与简报结果写回飞书多维表。"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, feishu, report, rss, sources

log = logging.getLogger("daily")
CN_TZ = timezone(timedelta(hours=8))
TOPIC_OPTIONS = {"AI", "LLM", "Agent", "RAG", "推理", "多模态", "开源", "硬件", "监管", "融资", "产品", "其他"}
URGENCY_TO_TABLE = {"高": "High", "中": "Medium", "低": "Low"}
URGENCY_TO_CN = {value: key for key, value in URGENCY_TO_TABLE.items()}


def brief_bullet_title(text: str, suggested: str = "") -> str:
    """确保简报标题表达具体结论，而不是“要点 1”一类占位文案。"""
    title = suggested.strip()
    if title and not re.fullmatch(r"要点\s*\d*", title):
        return title
    for separator in ("：", ":", "，", "。", "；", ";"):
        if separator in text:
            return text.split(separator, 1)[0].strip()[:28]
    return text.strip()[:28]


def scalar(value: Any) -> Any:
    if isinstance(value, list):
        if not value:
            return ""
        return scalar(value[0])
    if isinstance(value, dict):
        return value.get("text") or value.get("link") or value.get("name") or ""
    return value if value is not None else ""


def link(value: Any) -> str:
    if isinstance(value, list):
        return link(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "")
    return str(value or "")


def media_assets(value: Any) -> dict[str, Any]:
    raw = scalar(value)
    if not raw:
        return {"images": [], "videos": []}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {"images": [], "videos": []}
    except (TypeError, ValueError):
        return {"images": [], "videos": []}


def content_type(fields: dict[str, Any]) -> str:
    """从来源字段与链接识别需要显式展示的内容载体。"""
    source = str(scalar(fields.get("来源")) or "")
    source_type = str(scalar(fields.get("来源类型")) or "")
    url = link(fields.get("链接"))
    text = f"{source} {source_type} {url}".lower()
    if any(token in text for token in ("mp.weixin.qq.com", "weixin.qq.com", "微信公众号", "公众号")):
        return "公众号"
    if any(token in text for token in ("youtube.com", "youtu.be", "bilibili.com", "vimeo.com", "视频")):
        return "视频"
    if any(token in text for token in ("arxiv.org", "openreview.net", "doi.org", "学术论文")):
        return "论文"
    if source_type.lower() == "social" or any(
        token in text for token in ("x.com/", "twitter.com/", "weibo.com/", "linkedin.com/")
    ):
        return "社交媒体帖子"
    return ""


def date_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp() * 1000)


def today_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def _priority_map(param_records: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in param_records:
        fields = record.get("fields") or {}
        source_id = str(sources.cell(fields.get("source_id")) or "")
        if source_id:
            result[source_id] = str(sources.cell(fields.get("priority")) or "P2")
    return result


def _rss_source_ids(param_records: list[dict[str, Any]]) -> set[str]:
    return {
        str(sources.cell((record.get("fields") or {}).get("source_id")) or "")
        for record in param_records
        if sources.cell((record.get("fields") or {}).get("status")) == "active"
        and sources.cell((record.get("fields") or {}).get("fetch_method")) == "RSS"
    } - {""}


def select_candidates(
    records: list[dict[str, Any]],
    priorities: dict[str, str],
    allowed_source_ids: set[str] | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """取近七日 RSS 信号；官方优先，arXiv 总数不超过配置上限。"""
    now = now or datetime.now(timezone.utc)
    cutoff_ms = int((now - timedelta(days=7)).timestamp() * 1000)
    candidates = []
    for record in records:
        fields = record.get("fields") or {}
        stamp = int(float(scalar(fields.get("发布时间")) or scalar(fields.get("采集时间")) or 0))
        if stamp < cutoff_ms:
            continue
        source_id = str(scalar(fields.get("source_id")) or "")
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            continue
        candidates.append(
            {
                "record_id": record.get("record_id"),
                "fields": fields,
                "source_id": source_id,
                "priority": priorities.get(source_id, "P2"),
                "stamp": stamp,
            }
        )
    candidates.sort(key=lambda item: ({"P0": 0, "P1": 1, "P2": 2}.get(item["priority"], 3), -item["stamp"]))
    selected: list[dict[str, Any]] = []
    arxiv_count = 0
    for item in candidates:
        is_arxiv = item["source_id"].startswith("arxiv-") or "arxiv.org/" in link(item["fields"].get("链接"))
        if is_arxiv and arxiv_count >= config.MAX_ARXIV_ITEMS:
            continue
        selected.append(item)
        arxiv_count += int(is_arxiv)
        if len(selected) >= (limit or config.DAILY_CANDIDATE_LIMIT):
            break
    return selected


def analyze_signal(fields: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""你是资深 AI 行业分析师。只依据给定原文输出严格 JSON，不得虚构。
字段：title_cn（准确简洁的中文标题）、summary_cn（中文1-2句）、why（中文1句）、impact/novelty/actionability（0-100整数）、
urgency（高/中/低）、topics（从 AI、LLM、Agent、RAG、推理、多模态、开源、硬件、监管、融资、产品、其他中选2-4个）。
标题：{scalar(fields.get("标题"))}
来源：{scalar(fields.get("来源"))}
分类：{scalar(fields.get("分类"))}
原文节选：{str(scalar(fields.get("原文")))[:4000]}"""
    raw = report._llm_json(prompt)
    topics = [str(topic) for topic in raw.get("topics") or [] if str(topic) in TOPIC_OPTIONS][:4]
    return {
        "title_cn": str(raw.get("title_cn") or scalar(fields.get("标题"))).strip(),
        "summary_cn": str(raw.get("summary_cn") or "").strip(),
        "why": str(raw.get("why") or "").strip(),
        "impact": max(0, min(100, int(raw.get("impact") or 0))),
        "novelty": max(0, min(100, int(raw.get("novelty") or 0))),
        "actionability": max(0, min(100, int(raw.get("actionability") or 0))),
        "urgency": str(raw.get("urgency")) if raw.get("urgency") in URGENCY_TO_TABLE else "中",
        "topics": topics or ["其他"],
    }


def _signal_from_fields(record_id: str, fields: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    published = int(float(scalar(fields.get("发布时间")) or 0))
    return {
        "recordId": record_id,
        "title": str(scalar(fields.get("标题"))),
        "titleCn": analysis["title_cn"],
        "source": str(scalar(fields.get("来源"))),
        "url": link(fields.get("链接")),
        "category": str(scalar(fields.get("分类")) or "其他"),
        "contentType": content_type(fields),
        "publishedDate": datetime.fromtimestamp(published / 1000, CN_TZ).strftime("%Y-%m-%d") if published else "",
        "summary": analysis["summary_cn"],
        "why": analysis["why"],
        "impact": analysis["impact"],
        "novelty": analysis["novelty"],
        "actionability": analysis["actionability"],
        "urgency": analysis["urgency"],
        "tags": analysis["topics"],
        "imageUrl": link(fields.get("图片链接")),
        "mediaAssets": media_assets(fields.get("媒体资源")),
    }


def _existing_analysis(fields: dict[str, Any]) -> dict[str, Any] | None:
    if scalar(fields.get("状态")) != "已分析" or not scalar(fields.get("中文摘要")):
        return None
    table_urgency = str(scalar(fields.get("紧迫度")) or "Medium")
    topics = fields.get("主题") if isinstance(fields.get("主题"), list) else []
    return {
        "title_cn": str(scalar(fields.get("中文标题")) or scalar(fields.get("标题"))),
        "summary_cn": str(scalar(fields.get("中文摘要"))),
        "why": str(scalar(fields.get("为何重要"))),
        "impact": int(float(scalar(fields.get("影响分")) or 0)),
        "novelty": int(float(scalar(fields.get("新颖度")) or 0)),
        "actionability": int(float(scalar(fields.get("可行动性")) or 0)),
        "urgency": URGENCY_TO_CN.get(table_urgency, "中"),
        "topics": [str(scalar(x)) for x in topics] or ["其他"],
    }


def _upsert_brief(token: str, table_id: str, payload: dict[str, Any]) -> str:
    brief_id = payload["date"]
    fields = {
        "简报ID": brief_id,
        "简报日期": date_ms(brief_id),
        "简报标题": payload["title"],
        "导语": payload["intro"],
        "关键要点": json.dumps(payload["bullets"], ensure_ascii=False),
        "信号记录ID": json.dumps([s["recordId"] for s in payload["signals"]], ensure_ascii=False),
        "状态": "已发布",
        "网页路径": f"/?date={brief_id}",
    }
    existing = feishu.read_all_records_with_ids(token, table_id, ["简报ID"])
    match = next((r for r in existing if str(scalar(r["fields"].get("简报ID"))) == brief_id), None)
    if match:
        feishu.update_record(token, table_id, match["record_id"], fields)
        return str(match["record_id"])
    fields["发送状态"] = "待发送"
    return str(feishu.create_record(token, table_id, fields).get("record_id") or "")


def generate(day: str | None = None) -> dict[str, Any]:
    if not config.LLM_API_KEY:
        raise config.ConfigError("生成真实简报需要 LLM_API_KEY")
    token = feishu.get_tenant_access_token()
    feishu.ensure_entry_enrichment_fields(token)
    params = feishu.read_param_records(token)
    entries = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    candidates = select_candidates(entries, _priority_map(params), _rss_source_ids(params))
    if not candidates:
        raise RuntimeError("近七日没有可用于简报的 RSS 信号")

    updates: list[dict[str, Any]] = []
    analyzed: list[dict[str, Any]] = []
    for index, item in enumerate(candidates, 1):
        fields = item["fields"]
        analysis = _existing_analysis(fields)
        if analysis is None:
            log.info("分析 %d/%d: %s", index, len(candidates), scalar(fields.get("标题")))
            analysis = analyze_signal(fields)
            updates.append(
                {
                    "record_id": item["record_id"],
                    "fields": {
                        "中文标题": analysis["title_cn"],
                        "中文摘要": analysis["summary_cn"],
                        "为何重要": analysis["why"],
                        "影响分": analysis["impact"],
                        "新颖度": analysis["novelty"],
                        "可行动性": analysis["actionability"],
                        "紧迫度": URGENCY_TO_TABLE[analysis["urgency"]],
                        "主题": analysis["topics"],
                        "状态": "已分析",
                    },
                }
            )
            if len(updates) >= 3:
                feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)
                updates.clear()
        analyzed.append(_signal_from_fields(str(item["record_id"]), fields, analysis))
    feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)

    analyzed.sort(key=lambda s: (s["impact"], s["novelty"], s["actionability"]), reverse=True)
    signals = analyzed[: config.DAILY_SIGNAL_LIMIT]
    image_updates = []
    seen_images: set[str] = set()
    for signal in signals:
        media = signal.get("mediaAssets") or {"images": [], "videos": []}
        if "arxiv.org/" in str(signal.get("url") or "") and not media.get("images"):
            figures = rss.fetch_arxiv_figures(str(signal.get("url") or ""))
            if figures:
                media["images"] = figures
                signal["mediaAssets"] = media
                image_updates.append(
                    {
                        "record_id": signal["recordId"],
                        "fields": {"媒体资源": json.dumps(media, ensure_ascii=False)},
                    }
                )
        image_url = str(signal.get("imageUrl") or "").strip()
        image_key = image_url.split("?", 1)[0].split("#", 1)[0].lower()
        if image_key in seen_images:
            image_url = ""
        if not image_url:
            candidate = rss.fetch_article_image(str(signal.get("url") or ""))
            candidate_key = candidate.split("?", 1)[0].split("#", 1)[0].lower()
            if candidate_key and candidate_key not in seen_images:
                image_url = candidate
                image_updates.append(
                    {
                        "record_id": signal["recordId"],
                        "fields": {"图片链接": {"link": image_url, "text": "原文配图"}},
                    }
                )
        signal["imageUrl"] = image_url
        if image_url:
            seen_images.add(image_url.split("?", 1)[0].split("#", 1)[0].lower())
    if image_updates:
        merged_updates: dict[str, dict[str, Any]] = {}
        for update in image_updates:
            record_id = str(update["record_id"])
            merged_updates.setdefault(record_id, {}).update(update["fields"])
        feishu.batch_update_records(
            token,
            config.FEISHU_ENTRY_TABLE_ID,
            [{"record_id": record_id, "fields": fields} for record_id, fields in merged_updates.items()],
        )

    numbered = "\n".join(f'[{i}] {s["title"]} — {s["summary"]}' for i, s in enumerate(signals, 1))
    synth = report._llm_json(
        "你是 AI 情报主编。只依据以下信号输出严格 JSON："
        "intro 为2句中文导语；bullets 为3-6个对象，每个必须含 title、text 和 refs（引用编号数组）。"
        "title 必须是概括具体结论的中文短标题，严禁使用“要点1”“要点2”等占位标题；"
        "text 不要重复 title。\n" + numbered
    )
    bullets = []
    for item in synth.get("bullets") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        bullets.append(
            {
                "title": brief_bullet_title(text, str(item.get("title") or "")),
                "text": text,
                "refs": [int(x) for x in item.get("refs") or [] if str(x).isdigit()],
            }
        )
        if len(bullets) == 6:
            break
    if not bullets:
        bullets = [
            {
                "title": brief_bullet_title(signal["summary"], signal["titleCn"]),
                "text": signal["summary"],
                "refs": [index],
            }
            for index, signal in enumerate(signals[:3], 1)
        ]
    payload = {
        "date": day or today_cn(),
        "title": f"AI Signal 每日情报 · {day or today_cn()}",
        "intro": str(synth.get("intro") or "今日 AI 信号已完成采集与分析。"),
        "bullets": bullets,
        "signals": signals,
    }
    table_id = config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token)
    payload["briefRecordId"] = _upsert_brief(token, table_id, payload)
    payload["briefTableId"] = table_id
    return payload


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="简报日期，默认北京时间今天")
    parser.add_argument("--output", default="output/daily-brief.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    payload = generate(args.date)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已生成 %s，共 %d 条信号", payload["date"], len(payload["signals"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
