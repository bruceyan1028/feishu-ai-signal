"""将飞书中的真实简报生成为 GitHub Pages 静态站。"""
from __future__ import annotations

import argparse
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import cluster, config, daily, feishu, paper_fulltext, policy_document, rss

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "index.html"


def _json_cell(value: Any, fallback: Any) -> Any:
    raw = daily.scalar(value)
    if not raw:
        return fallback
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return fallback


def _date_from_ms(value: Any) -> str:
    try:
        stamp = int(float(daily.scalar(value)))
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(stamp / 1000, CN_TZ).strftime("%Y-%m-%d")


def _signal_from_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    urgency = daily.URGENCY_TO_CN.get(str(daily.scalar(fields.get("紧迫度"))), "中")
    published = _date_from_ms(fields.get("发布时间"))
    try:
        published_ms = int(float(daily.scalar(fields.get("发布时间")) or 0))
    except (TypeError, ValueError):
        published_ms = 0
    paper_metrics = _json_cell(fields.get("论文指标"), {})
    if not isinstance(paper_metrics, dict):
        paper_metrics = {}
    full_text = paper_metrics.get("full_text") or {}
    if not isinstance(full_text, dict):
        full_text = {}
    signal = {
        "recordId": str(record.get("record_id") or ""),
        "sourceId": str(daily.scalar(fields.get("source_id")) or ""),
        "title": str(daily.scalar(fields.get("标题")) or ""),
        "titleCn": str(daily.scalar(fields.get("中文标题")) or daily.scalar(fields.get("标题")) or ""),
        "source": str(daily.scalar(fields.get("来源")) or ""),
        "url": daily.link(fields.get("链接")),
        "category": str(daily.scalar(fields.get("分类")) or "其他"),
        "contentType": daily.content_type(fields),
        "publishedDate": published,
        "publishedAtMs": published_ms,
        "summary": str(daily.scalar(fields.get("中文摘要")) or ""),
        "deepAnalysis": str(daily.scalar(fields.get("AI深度解读")) or ""),
        "why": str(daily.scalar(fields.get("为何重要")) or ""),
        "impact": int(float(daily.scalar(fields.get("影响分")) or 0)),
        "novelty": int(float(daily.scalar(fields.get("新颖度")) or 0)),
        "actionability": int(float(daily.scalar(fields.get("可行动性")) or 0)),
        "urgency": urgency,
        "tags": [str(daily.scalar(item)) for item in fields.get("主题") or []],
        "imageUrl": daily.link(fields.get("图片链接")),
        "mediaAssets": daily.media_assets(fields.get("媒体资源")),
        "editorialStructure": "source" if daily.editorial_structure_mode(fields) else "",
    }
    if full_text:
        signal.update(
            {
                "pdfUrl": str(full_text.get("pdf_url") or ""),
                "paperFullTextSource": str(full_text.get("source") or ""),
                "paperPages": int(full_text.get("pages") or 0),
                "paperCaptions": list(full_text.get("captions") or [])[:24],
                "paperVisualPages": list(full_text.get("visual_pages") or [])[:4],
            }
        )
    return signal


def _within_source_window(
    signal: dict[str, Any],
    brief_date: str,
    lookback_hours: dict[str, int],
) -> bool:
    """历史简报重建时重验来源时效，避免旧的错误信号永久留在详情页。"""
    source_id = str(signal.get("sourceId") or "")
    if source_id not in lookback_hours:
        return True
    try:
        published_ms = int(signal.get("publishedAtMs") or 0)
        brief_end = datetime.strptime(brief_date, "%Y-%m-%d").replace(
            tzinfo=CN_TZ
        ) + timedelta(days=1)
    except (TypeError, ValueError):
        return False
    if published_ms <= 0:
        return False
    hours = max(1, min(7 * 24, int(lookback_hours[source_id])))
    published = datetime.fromtimestamp(published_ms / 1000, CN_TZ)
    return brief_end - timedelta(hours=hours) <= published <= brief_end


def load_entry_pool(token: str) -> dict[str, dict[str, Any]]:
    """全量条目池，用于给简报信号补齐同事件的其它源头。"""
    records = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    return {str(record.get("record_id")): _signal_from_record(record) for record in records}


def load_recent_briefs(
    token: str,
    days: int = 7,
    entries: dict[str, dict[str, Any]] | None = None,
    lookback_hours: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    table_id = config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token)
    brief_records = feishu.read_all_records_with_ids(token, table_id)
    entries = load_entry_pool(token) if entries is None else entries
    briefs: list[dict[str, Any]] = []
    for record in brief_records:
        fields = record.get("fields") or {}
        if str(daily.scalar(fields.get("状态"))) != "已发布":
            continue
        date = str(daily.scalar(fields.get("简报ID")) or _date_from_ms(fields.get("简报日期")))
        signal_ids = [str(item) for item in _json_cell(fields.get("信号记录ID"), [])]
        signals = [entries[record_id] for record_id in signal_ids if record_id in entries]
        if lookback_hours:
            signals = [
                signal
                for signal in signals
                if _within_source_window(signal, date, lookback_hours)
            ]
        if not date or not signals:
            continue
        # 从全量条目池补齐同事件其它源头，供详情页「事件聚合」展示
        pool = list(entries.values())
        if lookback_hours:
            pool = [
                signal
                for signal in pool
                if _within_source_window(signal, date, lookback_hours)
            ]
        signals = cluster.enrich_with_pool(signals, pool, threshold=0.85)
        briefs.append(
            {
                "date": date,
                "title": str(daily.scalar(fields.get("简报标题")) or f"AI Signal 每日情报 · {date}"),
                "intro": str(daily.scalar(fields.get("导语")) or ""),
                "bullets": _json_cell(fields.get("关键要点"), []),
                "signals": signals,
                "briefRecordId": str(record.get("record_id") or ""),
                "briefTableId": table_id,
            }
        )
    briefs.sort(key=lambda item: item["date"], reverse=True)
    return briefs[:days]


def curate_web_media(briefs: list[dict[str, Any]]) -> None:
    """发布前按载体重整封面，避免旧记录中的作者头像和广告继续出现在网页。"""
    signals = [
        signal
        for brief in briefs
        for signal in brief.get("signals") or []
        if signal.get("contentType") in {"文章", "公众号", "视频", "播客"}
    ]
    article_urls = sorted(
        {
            str(signal.get("url") or "")
            for signal in signals
            if signal.get("contentType") in {"文章", "公众号"}
            and str(signal.get("url") or "").startswith(("http://", "https://"))
        }
    )
    covers: dict[str, str] = {}
    if article_urls:
        with ThreadPoolExecutor(max_workers=min(8, len(article_urls))) as executor:
            covers = {
                url: cover
                for url, cover in zip(article_urls, executor.map(rss.fetch_article_image, article_urls))
                if cover
            }
    for signal in signals:
        media, cover = rss.curate_display_media(
            signal,
            covers.get(str(signal.get("url") or ""), ""),
        )
        signal["mediaAssets"] = media
        signal["imageUrl"] = cover


def build_site(briefs: list[dict[str, Any]], site_dir: Path | str = ROOT / "site") -> Path:
    if not briefs:
        raise RuntimeError("没有可发布的已发布简报")
    site = Path(site_dir)
    if site.exists():
        shutil.rmtree(site)
    data_dir = site / "data"
    data_dir.mkdir(parents=True)
    paper_media_dir = site / "media" / "papers"
    policy_media_dir = site / "media" / "policies"
    shutil.copy2(TEMPLATE, site / "index.html")
    rendered: dict[str, list[dict[str, str]]] = {}
    for brief in briefs:
        for signal in brief.get("signals") or []:
            pdf_url = str(signal.get("pdfUrl") or "")
            pages = list(signal.get("paperVisualPages") or [])
            if signal.get("paperFullTextSource") != "pdf" or not pdf_url or not pages:
                continue
            key = str(signal.get("recordId") or pdf_url)
            if key not in rendered:
                files = paper_fulltext.write_visual_page_images(
                    pdf_url,
                    pages,
                    paper_media_dir,
                    key,
                    list(signal.get("paperCaptions") or []),
                )
                rendered[key] = [
                    {
                        "url": f"media/papers/{item['filename']}",
                        "alt": item["alt"],
                        "kind": "pdf-page",
                    }
                    for item in files
                ]
            if rendered[key]:
                media = dict(signal.get("mediaAssets") or {})
                existing = list(media.get("images") or [])
                known = {str(item.get("url") or "") for item in rendered[key]}
                media["images"] = rendered[key] + [
                    item for item in existing if str(item.get("url") or "") not in known
                ]
                signal["mediaAssets"] = media
    rendered_policy_documents: dict[str, list[dict[str, str]]] = {}
    for brief in briefs:
        for signal in brief.get("signals") or []:
            media = dict(signal.get("mediaAssets") or {})
            documents = [
                document
                for document in media.get("documents") or []
                if isinstance(document, dict)
                and document.get("fullTextSource") == "pdf"
                and document.get("visualPages")
            ]
            policy_images: list[dict[str, str]] = []
            for document_index, document in enumerate(documents, 1):
                pdf_url = str(document.get("url") or "")
                if pdf_url not in rendered_policy_documents:
                    files = policy_document.write_visual_images(
                        pdf_url,
                        list(document.get("visualPages") or []),
                        policy_media_dir,
                        f"{signal.get('recordId') or 'policy'}-d{document_index}",
                    )
                    rendered_policy_documents[pdf_url] = [
                        {
                            "url": f"media/policies/{item['filename']}",
                            "alt": item["alt"],
                        }
                        for item in files
                    ]
                policy_images.extend(rendered_policy_documents[pdf_url])
            if policy_images:
                existing = list(media.get("images") or [])
                known = {str(item.get("url") or "") for item in policy_images}
                media["images"] = policy_images + [
                    item
                    for item in existing
                    if str(item.get("url") or "") not in known
                    and not str(item.get("url") or "").startswith("media/policies/")
                ]
                signal["mediaAssets"] = media
    for brief in briefs:
        content = json.dumps(brief, ensure_ascii=False, indent=2)
        (data_dir / f'brief-{brief["date"]}.json').write_text(content, encoding="utf-8")
    (data_dir / "brief-latest.json").write_text(
        json.dumps(briefs[0], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (site / ".nojekyll").write_text("", encoding="utf-8")
    return site


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="优先加入本次生成的简报 JSON")
    parser.add_argument("--site-dir", default=str(ROOT / "site"))
    args = parser.parse_args()
    token = feishu.get_tenant_access_token()
    entries = load_entry_pool(token)
    params = feishu.read_param_records(token)
    lookback_hours = daily._lookback_hours_map(params)
    briefs = load_recent_briefs(
        token,
        entries=entries,
        lookback_hours=lookback_hours,
    )
    if args.input:
        current = json.loads(Path(args.input).read_text(encoding="utf-8"))
        # 当天简报走的是本地 JSON，同样要补齐同事件其它源头，否则只有历史简报有事件聚合
        pool = [
            signal
            for signal in entries.values()
            if _within_source_window(signal, current["date"], lookback_hours)
        ]
        current["signals"] = cluster.enrich_with_pool(
            current.get("signals") or [], pool, threshold=0.85
        )
        briefs = [current, *[item for item in briefs if item["date"] != current["date"]]][:7]
    curate_web_media(briefs)
    site = build_site(briefs, args.site_dir)
    print(site)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
