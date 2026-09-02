"""信号源日期链路长期维护表（三周统一窗口）。

一份可反复刷新的 HTML + 分源 JSON，回答四件事：

  1. 这个源的日期字段走哪条链路（列表阶段 / 正文阶段 / 兜底）
  2. 按抽到的发布时间排序后，窗口内条目长什么样
  3. 列表页标注快照（绿=生产会抽中，橙=规则通过但被 max_articles 截断）
  4. 三周窗口内最终相关新闻链接清单

设计约定：

  - 时间窗固定 21 天（可用 --window-days 覆盖），与各源 lookback_window 解耦；
    表里同时显示「源自身 lookback」方便对照。
  - 盘点抽链时临时抬高 max_articles（默认 60），避免 8 条截断遮住三周内的稿；
    标注快照仍按生产 max_articles 着色，反映真实截断。
  - 权威数据是 output/date-pipeline/sources/{id}.json；index.html 由此生成。
  - 默认读 seed（仓库权威配置）；--feishu 才打线上表。

用法：
    python -m tools.date_pipeline_report
    python -m tools.date_pipeline_report --sources epoch-compute,caixin,pingwest,mit-ai-risk
    python -m tools.date_pipeline_report --methods Scrape,RSS
    python -m tools.date_pipeline_report --feishu
    python -m tools.date_pipeline_report --reuse-html
    python -m tools.date_pipeline_report --skip-annotate
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output" / "date-pipeline"
SOURCES_DIR = OUT_DIR / "sources"
CN_TZ = timezone(timedelta(hours=8))
DEFAULT_WINDOW_DAYS = 21
INVENTORY_MAX_ARTICLES = 60

ROUTE_LABELS = {
    "rss_entry": "RSS 条目字段",
    "html_cascade": "HTML 通用 cascade",
    "list_neighbor": "列表邻近日期",
    "url_path": "URL 路径日期",
    "special_list": "专用列表解析器",
    "api_json": "JSON/API 字段",
    "media_api": "媒体平台 API",
    "podcast_rss": "播客 RSS",
    "unknown": "未分类",
}


@dataclass
class LinkRow:
    url: str
    title: str = ""
    published_raw: str = ""
    date_norm: str = ""
    date_tier: str = ""
    age_days: float | None = None
    in_window: bool = False
    in_production_cap: bool = False


@dataclass
class SourceCard:
    source_id: str
    name: str
    fetch_method: str
    status: str
    list_url: str
    lookback_hours: int
    lookback_label: str
    max_articles: int
    route_family: str
    route_list: str
    route_article: str
    route_notes: str = ""
    live_primary_tier: str = ""
    extracted_total: int = 0
    in_window_total: int = 0
    production_cap_in_window: int = 0
    sorted_rows: list[dict[str, Any]] = field(default_factory=list)
    final_links: list[dict[str, Any]] = field(default_factory=list)
    annotated_rel: str = ""
    raw_rel: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    timing_ms: float = 0.0
    probed_at: str = ""


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def norm_date(raw: str) -> str:
    from src import process

    ms = process.parse_date_ms(raw)
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def age_days(raw: str, *, now: datetime) -> float | None:
    from src import process

    ms = process.parse_date_ms(raw)
    if ms is None:
        return None
    return max(0.0, (now.timestamp() * 1000 - ms) / 86400000.0)


def describe_route(feed: dict[str, Any]) -> tuple[str, str, str, str]:
    """返回 (family, list_stage, article_stage, notes)。"""
    from src import rss, scrape

    method = str(feed.get("fetch_method") or "")
    sid = str(feed.get("id") or "")

    if method == "RSS":
        note = ""
        if sid in rss._UPDATED_ONLY_DATE_SOURCES:
            note = f"无 published 时允许 updated（白名单：{sid}）"
        return (
            "rss_entry",
            "feedparser：entry.published / pubDate",
            "不回源抽日期（信任 feed）",
            note,
        )
    if method == "Media":
        return ("media_api", "平台 API publishedAt", "—", "")
    if method == "Podcast":
        return ("podcast_rss", "播客 RSS published，否则 updated", "—", "")
    if method == "Social":
        return ("api_json", "X API created_at", "—", "")

    if scrape._is_json_api_feed(feed):
        if scrape._is_modelscope_feed(feed):
            return ("api_json", "ModelScope OpenAPI created_at", "—", "不经 HTML cascade")
        if scrape._is_seed_feed(feed):
            return ("api_json", "Seed 详情 API PublishDate", "—", "不经 HTML cascade")
        if scrape._is_github_feed(feed):
            return (
                "api_json",
                "GitHub release published_at（新仓用 created_at）",
                "—",
                "不经 HTML cascade",
            )
        return ("api_json", "JSON API 日期字段", "—", "")

    if scrape._is_hf_pwc_paper_feed(feed):
        return (
            "api_json",
            "HF/PwC publishedAt；必要时 arxiv id 月份校正",
            "—",
            "专用论文榜解析",
        )
    if scrape._is_anthropic_news_feed(feed):
        return (
            "special_list",
            "Anthropic PublicationList 卡片 <time>",
            "通用 HTML cascade 兜底",
            "专用 list_parser",
        )
    if str(scrape._feed_extra(feed).get("list_parser") or "").strip() == "zhipu_news":
        return (
            "special_list",
            "智谱新闻卡旁 YYYY/MM/DD",
            "通用 HTML cascade 兜底",
            "list_parser=zhipu_news",
        )

    return (
        "html_cascade",
        "① URL 路径日期 → ② 列表卡片邻近日期",
        "① meta → ② JSON-LD/SSR → ③ <time> → ④ h1 header → ⑤ 可见「发布」文本 → ⑥ URL",
        "通用 Scrape 列表/正文 cascade",
    )


def infer_live_tier(link: dict[str, Any]) -> str:
    raw = str(link.get("published_raw") or "").strip()
    if not raw:
        return "none"
    from src import scrape

    url = str(link.get("url") or "")
    url_date = scrape._published_date_from_url(url)
    if url_date and (raw == url_date or raw.startswith(url_date)):
        return "url"
    if scrape._LIST_NEAR_DATE_RE.fullmatch(raw) or scrape._LIST_NEAR_DATE_RE.match(raw):
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw):
            return "iso"
        return "list_neighbor_or_parser"
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw):
        return "iso"
    return "list_or_api"


def _map_feeds_allow_exp(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from src import sources

    return sources._map_feed_sources(records, allow_experimental=True)


def load_feeds(
    *,
    use_feishu: bool,
    methods: set[str],
    wanted: set[str],
    allow_experimental: bool,
) -> tuple[list[dict[str, Any]], str]:
    from src import config_store, sources

    if use_feishu:
        from src import config, feishu, typed_config

        config.validate()
        token = feishu.get_tenant_access_token()
        records = feishu.read_param_records(token)
        type_configs = typed_config.load_typed_configs(token)
        origin = "feishu"
    else:
        records = config_store.read_param_records()
        type_configs = config_store.load_typed_configs()
        origin = "seed"

    feeds: list[dict[str, Any]] = []
    if "Scrape" in methods:
        scrape_feeds = sources.map_scrape_sources_for_diag(
            records,
            include_b_class=True,
            allow_experimental=allow_experimental,
        )
        for feed in scrape_feeds:
            cfg = type_configs.get(feed.get("id") or "") or {}
            if cfg.get("entity_type") == "github":
                feed["github_config"] = cfg.get("params") or {}
            feeds.append(feed)
    if "RSS" in methods:
        if allow_experimental:
            feeds.extend(_map_feeds_allow_exp(records))
        else:
            feeds.extend(sources.map_feed_sources(records))

    if wanted:
        feeds = [f for f in feeds if str(f.get("id") or "") in wanted]
    feeds.sort(key=lambda f: (str(f.get("fetch_method") or ""), str(f.get("id") or "")))
    return feeds, origin


def build_rows(
    links: list[dict[str, Any]],
    *,
    now: datetime,
    window_days: int,
    production_cap: int,
) -> list[LinkRow]:
    from src import process

    enriched: list[LinkRow] = []
    for link in links:
        raw = str(link.get("published_raw") or "").strip()
        dn = norm_date(raw)
        age = age_days(raw, now=now)
        in_win = age is not None and age <= float(window_days)
        if age is not None and process.is_date_only_published(raw):
            in_win = age <= float(window_days) + 1.0
        enriched.append(
            LinkRow(
                url=str(link.get("url") or ""),
                title=str(link.get("title") or "")[:160],
                published_raw=raw,
                date_norm=dn,
                date_tier=infer_live_tier(link),
                age_days=round(age, 2) if age is not None else None,
                in_window=in_win,
            )
        )

    enriched.sort(
        key=lambda r: (
            0 if r.age_days is not None else 1,
            r.age_days if r.age_days is not None else 1e18,
            r.url,
        )
    )
    for index, row in enumerate(enriched):
        row.in_production_cap = index < production_cap
    return enriched


def probe_scrape_html(
    feed: dict[str, Any],
    *,
    now: datetime,
    window_days: int,
    reuse_html: bool,
    skip_annotate: bool,
) -> SourceCard:
    from src import scrape
    from tools import list_page_audit

    sid = str(feed.get("id") or "")
    family, route_list, route_article, notes = describe_route(feed)
    prod_cap = int(feed.get("max_articles") or 8)
    lookback_h = int(feed.get("lookback_hours") or 24)
    card = SourceCard(
        source_id=sid,
        name=str(feed.get("name") or sid),
        fetch_method="Scrape",
        status=str(feed.get("status") or "active"),
        list_url=str(feed.get("url") or ""),
        lookback_hours=lookback_h,
        lookback_label=f"{lookback_h}h",
        max_articles=prod_cap,
        route_family=family,
        route_list=route_list,
        route_article=route_article,
        route_notes=notes,
        probed_at=now.astimezone(CN_TZ).isoformat(timespec="seconds"),
    )

    t0 = time.perf_counter()
    raw_path = SOURCES_DIR / f"{sid}.list.raw.html"
    card.raw_rel = f"sources/{sid}.list.raw.html"
    links: list[dict[str, Any]] = []
    html = ""

    try:
        if scrape._is_json_api_feed(feed):
            items = scrape._fetch_json_api_items(feed)
            links = [
                {
                    "url": it.get("url") or "",
                    "title": it.get("title") or "",
                    "published_raw": it.get("published_raw") or "",
                }
                for it in items
            ]
        else:
            if reuse_html and raw_path.exists():
                html = raw_path.read_text(encoding="utf-8")
            else:
                html = scrape._safe_direct_get(feed["url"]) or ""
                raw_path.write_text(html, encoding="utf-8")
            if not html and not scrape._is_hf_pwc_paper_feed(feed):
                card.error = "list_empty_or_failed"
                card.timing_ms = round((time.perf_counter() - t0) * 1000, 1)
                return card
            inv_feed = {**feed, "max_articles": max(INVENTORY_MAX_ARTICLES, prod_cap)}
            links = scrape._extract_links_for_feed(html, inv_feed, use_jina=False)

            if not skip_annotate:
                note = list_page_audit.routing_note(feed)
                if note:
                    card.route_notes = (card.route_notes + "; " if card.route_notes else "") + note
                else:
                    rows = list_page_audit.classify_links(html, feed)
                    annotated, _regions = list_page_audit.annotate(html, feed, rows)
                    ann_path = SOURCES_DIR / f"{sid}.annotated.html"
                    ann_path.write_text(annotated, encoding="utf-8")
                    card.annotated_rel = f"sources/{sid}.annotated.html"
                    verify = list_page_audit.verify_against_pipeline(rows, html, feed)
                    if verify:
                        card.warnings.append(verify)
    except Exception as exc:  # noqa: BLE001
        card.error = f"{type(exc).__name__}: {exc}"
        card.timing_ms = round((time.perf_counter() - t0) * 1000, 1)
        return card

    rows = build_rows(links, now=now, window_days=window_days, production_cap=prod_cap)
    undated_n = sum(1 for r in rows if not r.published_raw)
    if undated_n and not scrape._is_json_api_feed(feed):
        # 列表抽不到日期时，回源抽正文级日期（限并发），否则三周窗会误报全空
        backfilled = _backfill_article_dates(
            [asdict(r) for r in rows if not r.published_raw][:24]
        )
        if backfilled:
            by_url = {r.url: r for r in rows}
            for item in backfilled:
                row = by_url.get(item["url"])
                if not row:
                    continue
                row.published_raw = item["published_raw"]
                row.date_norm = norm_date(row.published_raw)
                row.age_days = age_days(row.published_raw, now=now)
                row.date_tier = "article_cascade"
                if row.age_days is not None:
                    row.in_window = row.age_days <= float(window_days)
            from src import process

            for row in rows:
                if row.date_tier != "article_cascade":
                    continue
                if row.age_days is None:
                    row.in_window = False
                else:
                    grace = 1.0 if process.is_date_only_published(row.published_raw) else 0.0
                    row.in_window = row.age_days <= float(window_days) + grace
            rows.sort(
                key=lambda r: (
                    0 if r.age_days is not None else 1,
                    r.age_days if r.age_days is not None else 1e18,
                    r.url,
                )
            )
            for index, row in enumerate(rows):
                row.in_production_cap = index < prod_cap
            card.warnings.append(f"正文回填日期 {len(backfilled)}/{undated_n} 条")

    card.extracted_total = len(rows)
    card.sorted_rows = [asdict(r) for r in rows]
    in_win = [r for r in rows if r.in_window]
    card.in_window_total = len(in_win)
    card.production_cap_in_window = sum(1 for r in in_win if r.in_production_cap)
    card.final_links = [asdict(r) for r in in_win]

    tiers = [r.date_tier for r in rows if r.date_tier != "none"]
    if tiers:
        card.live_primary_tier = max(set(tiers), key=tiers.count)
        if card.live_primary_tier.startswith("list_neighbor"):
            card.route_family = "list_neighbor"
        elif card.live_primary_tier == "url":
            card.route_family = "url_path"
        elif card.live_primary_tier == "article_cascade":
            card.route_family = "html_cascade"
    undated = sum(1 for r in rows if not r.published_raw)
    if undated and rows:
        card.warnings.append(f"仍无日期 {undated}/{len(rows)} 条")
    if card.in_window_total == 0 and rows:
        card.warnings.append(f"抬高上限后抽出 {len(rows)} 条，但无一落入 {window_days} 天窗")

    card.timing_ms = round((time.perf_counter() - t0) * 1000, 1)
    return card


def _backfill_article_dates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对尚无 published_raw 的链接直连正文跑 extract_published_date_html。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from src import scrape

    if not rows:
        return []

    def one(row: dict[str, Any]) -> dict[str, Any] | None:
        url = str(row.get("url") or "")
        if not url:
            return None
        html = scrape._safe_direct_get(url) or ""
        if not html:
            return None
        published = scrape.extract_published_date_html(html, url)
        if not published:
            return None
        return {"url": url, "published_raw": published}

    out: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(one, row) for row in rows]
        for fut in as_completed(futures):
            try:
                hit = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if hit:
                out.append(hit)
    return out


def probe_rss(
    feed: dict[str, Any],
    *,
    now: datetime,
    window_days: int,
) -> SourceCard:
    from src import rss

    sid = str(feed.get("id") or "")
    family, route_list, route_article, notes = describe_route(feed)
    lookback_h = int(feed.get("lookback_hours") or 24)
    card = SourceCard(
        source_id=sid,
        name=str(feed.get("name") or sid),
        fetch_method="RSS",
        status=str(feed.get("status") or "active"),
        list_url=str(feed.get("url") or ""),
        lookback_hours=lookback_h,
        lookback_label=f"{lookback_h}h",
        max_articles=0,
        route_family=family,
        route_list=route_list,
        route_article=route_article,
        route_notes=notes,
        live_primary_tier="feed",
        probed_at=now.astimezone(CN_TZ).isoformat(timespec="seconds"),
    )
    t0 = time.perf_counter()
    items, stats = rss.fetch_feed_sources_with_stats([feed])
    st = stats.get(sid) or {}
    if st.get("error"):
        card.error = str(st["error"])
    links = [
        {
            "url": it.get("url") or "",
            "title": it.get("title") or "",
            "published_raw": it.get("published_raw") or "",
        }
        for it in items
    ]
    rows = build_rows(links, now=now, window_days=window_days, production_cap=len(links) or 1)
    for row in rows:
        row.date_tier = "feed" if row.published_raw else "none"
        row.in_production_cap = row.in_window
    card.extracted_total = len(rows)
    card.sorted_rows = [asdict(r) for r in rows]
    in_win = [r for r in rows if r.in_window]
    card.in_window_total = len(in_win)
    card.production_cap_in_window = len(in_win)
    card.final_links = [asdict(r) for r in in_win]
    if not in_win and rows:
        card.warnings.append(f"feed 有 {len(rows)} 条，无一落入 {window_days} 天窗")
    card.timing_ms = round((time.perf_counter() - t0) * 1000, 1)
    return card


def save_card(card: SourceCard) -> Path:
    path = SOURCES_DIR / f"{card.source_id}.json"
    path.write_text(json.dumps(asdict(card), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_html(cards: list[SourceCard], *, window_days: int, origin: str, now: datetime) -> str:
    esc = html_mod.escape
    generated = now.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M %Z")

    rows_html: list[str] = []
    details: list[str] = []
    for card in cards:
        fam = ROUTE_LABELS.get(card.route_family, card.route_family)
        snap = (
            f'<a href="{esc(card.annotated_rel)}" target="_blank">标注快照</a>'
            if card.annotated_rel
            else "—"
        )
        err_cls = "err" if card.error or card.in_window_total == 0 else ""
        rows_html.append(
            f"""<tr class="{err_cls}">
<td><a href="#src-{esc(card.source_id)}"><code>{esc(card.source_id)}</code></a></td>
<td>{esc(card.name)}</td>
<td>{esc(card.fetch_method)}</td>
<td>{esc(card.status)}</td>
<td title="{esc(card.route_list)}">{esc(fam)}<div class="muted">{esc(card.live_primary_tier or "—")}</div></td>
<td>{esc(card.lookback_label)}</td>
<td class="num">{card.extracted_total}</td>
<td class="num"><b>{card.in_window_total}</b></td>
<td class="num">{card.production_cap_in_window}</td>
<td>{snap}</td>
<td class="warn">{esc("; ".join(card.warnings[:2]) or card.error or "—")}</td>
</tr>"""
        )

        link_lis = []
        for item in card.final_links[:40]:
            cap = "·产线截断内" if item.get("in_production_cap") else ""
            link_lis.append(
                f'<li><span class="date">{esc(item.get("date_norm") or "?")}</span> '
                f'<a href="{esc(item.get("url") or "")}" target="_blank">{esc(item.get("title") or item.get("url") or "")}</a> '
                f'<span class="muted">{esc(item.get("published_raw") or "")} {cap}</span></li>'
            )
        sort_preview = []
        for item in card.sorted_rows[:15]:
            flag = "✓窗" if item.get("in_window") else ""
            sort_preview.append(
                f'<tr><td>{esc(item.get("date_norm") or "—")}</td>'
                f'<td>{esc(str(item.get("age_days") if item.get("age_days") is not None else "—"))}</td>'
                f'<td>{esc(item.get("date_tier") or "")}</td>'
                f'<td><a href="{esc(item.get("url") or "")}" target="_blank">{esc((item.get("title") or "")[:80])}</a> {flag}</td></tr>'
            )

        iframe = ""
        if card.annotated_rel:
            iframe = (
                f'<p><a href="{esc(card.annotated_rel)}" target="_blank">打开全屏标注快照</a></p>'
                f'<iframe class="snap" src="{esc(card.annotated_rel)}" title="annotated"></iframe>'
            )

        details.append(
            f"""<section class="card" id="src-{esc(card.source_id)}">
<h2><code>{esc(card.source_id)}</code> · {esc(card.name)}</h2>
<p class="meta">{esc(card.fetch_method)} · status={esc(card.status)} ·
<a href="{esc(card.list_url)}" target="_blank">{esc(card.list_url)}</a> ·
源 lookback {esc(card.lookback_label)} · 生产 max_articles={card.max_articles} ·
耗时 {card.timing_ms}ms</p>
{f'<p class="errbox">{esc(card.error)}</p>' if card.error else ''}
<h3>日期链路</h3>
<table class="inner">
<tr><th>家族</th><td>{esc(fam)}（实跑主 tier：{esc(card.live_primary_tier or "—")}）</td></tr>
<tr><th>列表阶段</th><td>{esc(card.route_list)}</td></tr>
<tr><th>正文阶段</th><td>{esc(card.route_article)}</td></tr>
<tr><th>备注</th><td>{esc(card.route_notes or "—")}</td></tr>
</table>
<h3>页面时间排序（抬高上限盘点，前 15）</h3>
<table class="inner">
<thead><tr><th>日期</th><th>龄(天)</th><th>tier</th><th>标题</th></tr></thead>
<tbody>{"".join(sort_preview) or "<tr><td colspan=4>无</td></tr>"}</tbody>
</table>
<h3>三周窗口内最终链接（{card.in_window_total}）</h3>
<ul class="final">{"".join(link_lis) or "<li>无</li>"}</ul>
<h3>页面标注快照</h3>
{iframe or "<p class='muted'>本源无 HTML 列表标注（API/RSS/专用路由）。</p>"}
{f'<p class="warnbox">{"<br>".join(esc(w) for w in card.warnings)}</p>' if card.warnings else ''}
</section>"""
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>信号源日期链路表 · {window_days} 天窗</title>
<style>
:root {{
  --bg: #f6f1e8; --ink: #1c1917; --muted: #78716c; --line: #d6d3d1;
  --accent: #0f766e; --bad: #9f1239; --card: #fffdf8;
  --sans: "IBM Plex Sans", "Source Han Sans SC", "Noto Sans SC", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 2rem 1.5rem 4rem;
  font: 14px/1.5 var(--sans); color: var(--ink);
  background:
    radial-gradient(1200px 600px at 10% -10%, #d9f0ea 0%, transparent 55%),
    linear-gradient(180deg, #efe7d8 0%, var(--bg) 40%);
}}
h1 {{ font-size: 1.75rem; letter-spacing: -0.02em; margin: 0 0 .4rem; }}
h2 {{ font-size: 1.2rem; margin: 0 0 .5rem; }}
h3 {{ font-size: .95rem; margin: 1.2rem 0 .4rem; color: var(--accent); }}
.lead {{ color: var(--muted); max-width: 70rem; }}
.legend {{
  display: grid; gap: .5rem; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  margin: 1.2rem 0 1.6rem; padding: 1rem; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px;
}}
.legend div {{ font-size: 12px; }}
.legend b {{ color: var(--accent); }}
.wrap {{ overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--card); }}
table.index {{ border-collapse: collapse; width: 100%; min-width: 960px; }}
table.index th, table.index td {{
  border-bottom: 1px solid var(--line); padding: .55rem .65rem; text-align: left; vertical-align: top;
}}
table.index th {{
  position: sticky; top: 0; background: #faf6ef; font-size: 12px;
}}
table.index tr.err {{ background: #fff1f2; }}
table.index .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
code {{ font-family: var(--mono); font-size: 12px; }}
.muted {{ color: var(--muted); font-size: 12px; }}
.warn {{ color: var(--bad); font-size: 12px; max-width: 18rem; }}
.card {{
  margin: 2rem 0; padding: 1.2rem 1.3rem; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px;
}}
.meta {{ color: var(--muted); font-size: 12px; }}
table.inner {{ border-collapse: collapse; width: 100%; }}
table.inner th, table.inner td {{ border-bottom: 1px solid var(--line); padding: .35rem .4rem; }}
table.inner th {{ width: 6rem; color: var(--muted); font-weight: 600; }}
ul.final {{ margin: .3rem 0; padding-left: 1.1rem; }}
ul.final .date {{ font-family: var(--mono); font-size: 12px; color: var(--accent); }}
iframe.snap {{
  width: 100%; height: 420px; border: 1px solid var(--line); border-radius: 6px; background: #fff;
}}
.errbox, .warnbox {{ padding: .6rem .8rem; border-radius: 6px; font-size: 13px; }}
.errbox {{ background: #ffe4e6; color: var(--bad); }}
.warnbox {{ background: #ffedd5; color: #9a3412; }}
a {{ color: #115e59; }}
.toc {{ margin: 1rem 0 2rem; font-size: 13px; }}
</style>
</head>
<body>
<h1>信号源日期链路维护表</h1>
<p class="lead">统一观察窗 <b>{window_days} 天</b> · 配置来源 <b>{esc(origin)}</b> · 生成于 {esc(generated)} ·
权威数据在 <code>output/date-pipeline/sources/*.json</code>，本页可随时
<code>python -m tools.date_pipeline_report</code> 重跑刷新。</p>

<div class="legend">
  <div><b>RSS</b> feed <code>published/pubDate</code>（少数白名单可用 <code>updated</code>）</div>
  <div><b>HTML 通用</b> 列表：URL 路径 → 邻近日期；正文：meta → JSON/SSR → time → header → 可见文本 → URL</div>
  <div><b>专用列表</b> Anthropic / 智谱等卡片内嵌日期</div>
  <div><b>API</b> ModelScope / Seed / GitHub / HF·PwC 返回字段</div>
  <div><b>排序列</b> 抬高 max_articles={INVENTORY_MAX_ARTICLES} 盘点；「产线截断内」= 落在生产 max_articles 名额里</div>
  <div><b>标注快照</b> 绿=生产会抽中，橙=规则通过但被截断，红=规则拒绝（与 list_page_audit 一致）</div>
</div>

<div class="wrap">
<table class="index">
<thead>
<tr>
<th>源 ID</th><th>名称</th><th>方式</th><th>状态</th><th>日期链路</th>
<th>源窗</th><th>抽出</th><th>{window_days}d内</th><th>产线额内∩窗</th>
<th>快照</th><th>告警</th>
</tr>
</thead>
<tbody>
{"".join(rows_html)}
</tbody>
</table>
</div>

<div class="toc">共 {len(cards)} 个源 · 窗内合计 {sum(c.in_window_total for c in cards)} 条链接</div>
{"".join(details)}
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default="", help="逗号分隔 source_id")
    parser.add_argument("--methods", default="Scrape,RSS", help="Scrape,RSS")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--feishu", action="store_true", help="读飞书而非 seed")
    parser.add_argument("--experimental", action="store_true")
    parser.add_argument("--reuse-html", action="store_true")
    parser.add_argument("--skip-annotate", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    methods = {m.strip() for m in args.methods.split(",") if m.strip()}
    wanted = {s.strip() for s in args.sources.split(",") if s.strip()}
    now = datetime.now(timezone.utc)

    feeds, origin = load_feeds(
        use_feishu=args.feishu,
        methods=methods,
        wanted=wanted,
        allow_experimental=args.experimental,
    )
    if not feeds:
        print("没有匹配的源")
        return 1

    print(
        f"配置={origin} 源={len(feeds)} 窗口={args.window_days}d "
        f"methods={','.join(sorted(methods))}"
    )

    cards: list[SourceCard] = []
    for index, feed in enumerate(feeds, 1):
        sid = str(feed.get("id") or "")
        method = str(feed.get("fetch_method") or "")
        print(f"[{index}/{len(feeds)}] {method} {sid} …", flush=True)
        if method == "RSS":
            card = probe_rss(feed, now=now, window_days=args.window_days)
        else:
            card = probe_scrape_html(
                feed,
                now=now,
                window_days=args.window_days,
                reuse_html=args.reuse_html,
                skip_annotate=args.skip_annotate,
            )
        save_card(card)
        cards.append(card)
        print(
            f"    → 抽出 {card.extracted_total}，窗内 {card.in_window_total}，"
            f"tier={card.live_primary_tier or '-'} {card.error or ''}"
        )

    manifest = {
        "generated_at": now.astimezone(CN_TZ).isoformat(timespec="seconds"),
        "window_days": args.window_days,
        "config_origin": origin,
        "methods": sorted(methods),
        "source_count": len(cards),
        "in_window_links": sum(c.in_window_total for c in cards),
        "sources": [c.source_id for c in cards],
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    index_path = OUT_DIR / "index.html"
    index_path.write_text(
        render_html(cards, window_days=args.window_days, origin=origin, now=now),
        encoding="utf-8",
    )
    print(f"\n已写出 {index_path.relative_to(ROOT)}")
    print(f"分源 JSON：{SOURCES_DIR.relative_to(ROOT)}/*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
