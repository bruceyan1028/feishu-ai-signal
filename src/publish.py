"""将飞书中的真实简报生成为 GitHub Pages 静态站。"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from . import (
    cluster,
    config,
    daily,
    feishu,
    openai_charts,
    paper_fulltext,
    policy_document,
    rss,
    source_view,
    sources,
)

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "index.html"
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
# 日报重建会清空 site/，周报、战略时间线和看板快照必须单独保留，直到下一期覆盖。
_PERSISTENT_DATA_GLOBS = ("weekly-*.json", "timeline-latest.json", "dashboard-latest.json", "heatmap.json", "heatmap-trends.json")


def stash_persistent_site_data(site: Path) -> dict[str, bytes]:
    data_dir = Path(site) / "data"
    kept: dict[str, bytes] = {}
    if not data_dir.is_dir():
        return kept
    for pattern in _PERSISTENT_DATA_GLOBS:
        for path in data_dir.glob(pattern):
            kept[path.name] = path.read_bytes()
    return kept


def restore_persistent_site_data(site: Path, kept: dict[str, bytes]) -> None:
    if not kept:
        return
    data_dir = Path(site) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, content in kept.items():
        dest = data_dir / name
        if not dest.exists():
            dest.write_bytes(content)


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
        "category": sources.normalize_category(daily.scalar(fields.get("分类")) or "其他"),
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
    """发布前按载体重整封面，避免旧记录中的作者头像和广告继续出现在网页。

    入选文章若尚未由模型选过图，会再读原文候选，选定封面并把插图挂到分析小节。
    """
    signals = [
        signal
        for brief in briefs
        for signal in brief.get("signals") or []
        if signal.get("contentType") in {"文章", "公众号", "视频", "播客"}
    ]
    article_titles = {
        str(signal.get("url") or ""): str(
            signal.get("title") or signal.get("titleCn") or ""
        )
        for signal in signals
        if signal.get("contentType") in {"文章", "公众号"}
        and str(signal.get("url") or "").startswith(("http://", "https://"))
        and (signal.get("mediaAssets") or {}).get("curatedBy") != "llm"
    }
    article_urls = sorted(article_titles)
    article_media: dict[str, dict[str, Any]] = {}
    if article_urls:
        with ThreadPoolExecutor(max_workers=min(8, len(article_urls))) as executor:
            fetched_media = executor.map(
                rss.fetch_article_media,
                article_urls,
                [article_titles[url] for url in article_urls],
            )
            article_media = {
                url: bundle
                for url, bundle in zip(article_urls, fetched_media)
                if bundle.get("cover") or bundle.get("images") or bundle.get("candidates")
            }
    for signal in signals:
        if (signal.get("mediaAssets") or {}).get("curatedBy") == "llm":
            continue
        media, cover = rss.select_pushed_article_images(
            signal,
            article_media.get(str(signal.get("url") or ""), {}),
        )
        signal["mediaAssets"] = media
        signal["imageUrl"] = cover


def mirror_huxiu_images(
    briefs: list[dict[str, Any]],
    output_dir: Path | str,
) -> None:
    """把虎嗅正文图落到站内，避免 img.huxiucdn.com 的 Referer 防盗链。"""
    destination = Path(output_dir)
    downloaded: dict[str, str] = {}
    for brief in briefs:
        for signal in brief.get("signals") or []:
            if (urlsplit(str(signal.get("url") or "")).hostname or "").lower().removeprefix(
                "www."
            ) != "huxiu.com":
                continue
            media = dict(signal.get("mediaAssets") or {})
            mirrored: list[dict[str, Any]] = []
            original_primary = str(signal.get("imageUrl") or "")
            new_primary = ""
            prefix = _SAFE_FILENAME_RE.sub(
                "-", str(signal.get("recordId") or "huxiu")
            ).strip("-")
            for index, image in enumerate(media.get("images") or [], 1):
                if not isinstance(image, dict):
                    continue
                source_url = str(image.get("url") or "")
                if (
                    urlsplit(source_url).hostname or ""
                ).lower().removeprefix("www.") != "img.huxiucdn.com":
                    mirrored.append(image)
                    continue
                relative = downloaded.get(source_url)
                if not relative:
                    try:
                        response = requests.get(
                            source_url,
                            headers={
                                "User-Agent": (
                                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 Chrome/138 Safari/537.36"
                                )
                            },
                            timeout=30,
                        )
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[
                            0
                        ].lower()
                        extension = _IMAGE_EXTENSIONS.get(content_type)
                        if not extension or len(response.content) > 12_000_000:
                            continue
                        destination.mkdir(parents=True, exist_ok=True)
                        filename = f"{prefix}-{index}{extension}"
                        (destination / filename).write_bytes(response.content)
                        relative = f"media/huxiu/{filename}"
                        downloaded[source_url] = relative
                    except requests.RequestException:
                        continue
                local_image = {**image, "url": relative}
                mirrored.append(local_image)
                if source_url == original_primary:
                    new_primary = relative
            media["images"] = mirrored
            signal["mediaAssets"] = media
            signal["imageUrl"] = new_primary or (
                str(mirrored[0].get("url") or "") if mirrored else ""
            )


def build_site(
    briefs: list[dict[str, Any]],
    site_dir: Path | str = ROOT / "site",
    params: list[dict[str, Any]] | None = None,
) -> Path:
    if not briefs:
        raise RuntimeError("没有可发布的已发布简报")
    site = Path(site_dir)
    kept = stash_persistent_site_data(site)
    if site.exists():
        shutil.rmtree(site)
    data_dir = site / "data"
    data_dir.mkdir(parents=True)
    paper_media_dir = site / "media" / "papers"
    policy_media_dir = site / "media" / "policies"
    openai_chart_dir = site / "media" / "openai-charts"
    huxiu_media_dir = site / "media" / "huxiu"
    shutil.copy2(TEMPLATE, site / "index.html")
    rendered_openai_charts: dict[str, list[dict[str, str]]] = {}
    for brief in briefs:
        for signal in brief.get("signals") or []:
            article_url = str(signal.get("url") or "")
            if not openai_charts.is_openai_article(article_url):
                continue
            if article_url not in rendered_openai_charts:
                files = openai_charts.write_article_charts(
                    article_url,
                    openai_chart_dir,
                    str(signal.get("recordId") or "openai"),
                )
                rendered_openai_charts[article_url] = [
                    {
                        "url": f"media/openai-charts/{item['filename']}",
                        "alt": item["alt"],
                        "kind": "openai-vega-chart",
                    }
                    for item in files
                ]
            chart_images = rendered_openai_charts[article_url]
            if not chart_images:
                continue
            media = dict(signal.get("mediaAssets") or {})
            existing = [
                item
                for item in media.get("images") or []
                if not str(item.get("url") or "").startswith("media/openai-charts/")
            ]
            media["images"] = chart_images + existing
            signal["mediaAssets"] = media
            signal["imageUrl"] = chart_images[0]["url"]
    mirror_huxiu_images(briefs, huxiu_media_dir)
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
    # 公开站只读快照：writable 为空，前端据此把开关渲染成不可点。
    (data_dir / "sources.json").write_text(
        json.dumps(
            source_view.build_payload(params or [], briefs=briefs, writable=False),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (site / ".nojekyll").write_text("", encoding="utf-8")
    restore_persistent_site_data(site, kept)
    return site


def export_weekly(
    payload: dict[str, Any], site_dir: Path | str = ROOT / "site"
) -> Path:
    """只更新周报 JSON 与前端模板，不触发耗时的正文媒体重新抓取。"""
    week_id = str(payload.get("weekId") or "").strip()
    if not week_id or not payload.get("signals"):
        raise RuntimeError("周报缺少 weekId 或 signals")
    site = Path(site_dir)
    data_dir = site / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATE, site / "index.html")
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    (data_dir / f"weekly-{week_id}.json").write_text(content, encoding="utf-8")
    (data_dir / "weekly-latest.json").write_text(content, encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    return site


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="优先加入本次生成的简报 JSON")
    parser.add_argument("--weekly-input", help="只发布本次生成的周报 JSON")
    parser.add_argument("--site-dir", default=str(ROOT / "site"))
    args = parser.parse_args()
    if args.weekly_input:
        payload = json.loads(Path(args.weekly_input).read_text(encoding="utf-8"))
        print(export_weekly(payload, args.site_dir))
        return 0
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
    site = build_site(briefs, args.site_dir, params=params)
    print(site)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
