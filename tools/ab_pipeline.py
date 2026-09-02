"""两套正文抽取策略的 A/B 干跑，产出分源筛选报告。

同一批抓取结果喂给两条独立的清洗漏斗，抽取策略是唯一变量：

    B「现状」    正文保持采集时拿到的原样（RSS feed 正文 / Jina markdown）
    A「新抽取」  回源抓 HTML，用 trafilatura → readability-lxml 级联重建正文，
                 并在缺发布时间时用 trafilatura 元数据补上

两套都当 day1：不读飞书已存去重键，跨轮去重基准为空集，所以报告反映的是
「假如今天从零开始，各源能出多少条」。

只读飞书配置（一级参数表 + 二级参数表），不写任何飞书表，不写 health 记录，
因此参数表的采集统计不受这次干跑影响。

用法：
    python -m tools.ab_pipeline
    python -m tools.ab_pipeline --methods RSS,Scrape       # 只跑部分通道
    python -m tools.ab_pipeline --max-fetch 500            # 限制回源抓取条数
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output"

# 与 rss.fetch_article_content 保持一致：实测这个 UA 在华尔街见闻、mlcommons
# 等站点上比真实 Chrome UA 更少被拦。
FETCH_UA = "Mozilla/5.0 (compatible; AI-Signal/1.0)"
FETCH_TIMEOUT = 25

log = logging.getLogger("ab")

BODY_SOURCES = ("trafilatura", "readability", "original", "empty")


def load_dotenv() -> None:
    """把 .env 里尚未设置的键读进环境；已设置的以环境为准。"""
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


class HtmlCache:
    """按 URL 抓一次 HTML，两套策略共享，避免网络成为变量。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._lock = threading.Lock()
        self.errors: Counter[str] = Counter()
        self.fetched = 0
        self.total_ms = 0.0

    def get(self, url: str) -> str:
        with self._lock:
            if url in self._store:
                return self._store[url]
        t0 = time.perf_counter()
        html = ""
        try:
            response = requests.get(
                url, headers={"User-Agent": FETCH_UA}, timeout=FETCH_TIMEOUT
            )
            response.raise_for_status()
            html = response.content[:1_500_000].decode(
                response.encoding or "utf-8", errors="replace"
            )
        except requests.RequestException as exc:
            self.errors[type(exc).__name__] += 1
        elapsed = (time.perf_counter() - t0) * 1000
        with self._lock:
            self._store[url] = html
            self.fetched += 1
            self.total_ms += elapsed
        return html


def extract_new(html: str, url: str) -> tuple[str, str, str]:
    """A 方案抽取：trafilatura → readability-lxml。

    返回 (正文, 来源标签, 发布日期)。两级都拿不到正文时正文为空,由调用方决定
    是否回退原正文。
    """
    if not html:
        return "", "", ""

    import trafilatura

    date = ""
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        date = str(getattr(meta, "date", "") or "") if meta else ""
    except Exception:  # noqa: BLE001 - 元数据失败不影响正文
        pass

    try:
        text = (
            trafilatura.extract(
                html,
                url=url,
                output_format="markdown",
                include_tables=True,
                include_comments=False,
                include_images=False,
            )
            or ""
        )
        if text.strip():
            return text, "trafilatura", date
    except Exception as exc:  # noqa: BLE001
        log.debug("trafilatura 失败 %s: %s", url, exc)

    try:
        from readability import Document

        from src import scrape

        summary_html = Document(html).summary(html_partial=True)
        text = scrape.html_to_text(summary_html)
        if text.strip():
            return text, "readability", date
    except Exception as exc:  # noqa: BLE001
        log.debug("readability 失败 %s: %s", url, exc)

    return "", "", date


def eligible_for_refetch(item: dict[str, Any]) -> bool:
    """判断这条是否值得回源抓 HTML。

    抓取有成本，只对「抽取结果可能改变筛选结论」的条目动手：正文型通道、有
    标题和链接、且在时间窗内（或缺日期——A 方案可能用元数据把它救回来）。
    """
    from src import process

    feed = item.get("feed") or {}
    if feed.get("fetch_method") in {"Media", "Social", "Podcast"}:
        return False
    url = process.normalize_url(item.get("url"))
    title = process.strip_html(item.get("title"))
    if not url or not title or not url.startswith(("http://", "https://")):
        return False
    published_ms = process.parse_date_ms(item.get("published_raw"))
    if published_ms is None:
        return True
    lookback_ms = int(feed.get("lookback_hours") or 168) * 3600000
    return process.now_ms() - published_ms < lookback_ms


def build_plan_a(
    raw_items: list[dict[str, Any]], cache: HtmlCache, max_fetch: int, workers: int
) -> tuple[list[dict[str, Any]], Counter[str], dict[str, Counter[str]]]:
    """深拷贝一份条目，把正文换成新抽取结果。"""
    items = copy.deepcopy(raw_items)
    targets = [it for it in items if eligible_for_refetch(it)][:max_fetch]
    log.info("A 方案回源抓取 %d 条（候选 %d）", len(targets), len(items))

    body_sources: Counter[str] = Counter()
    by_source: dict[str, Counter[str]] = {}

    def work(item: dict[str, Any]) -> None:
        from src import process

        url = process.normalize_url(item.get("url"))
        html = cache.get(url)
        text, tag, date = extract_new(html, url)
        sid = str((item.get("feed") or {}).get("id") or "")
        if text:
            item["body"] = text
            item["is_html"] = False
        else:
            tag = "original" if str(item.get("body") or "").strip() else "empty"
        # 缺发布时间的用元数据补：这是 A 方案能救回 missing_or_invalid_date 的路径
        if date and process.parse_date_ms(item.get("published_raw")) is None:
            item["published_raw"] = date
            item["_date_from_meta"] = True
        body_sources[tag] += 1
        by_source.setdefault(sid, Counter())[tag] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, targets))
    return items, body_sources, by_source


def run_plan(
    name: str, items: list[dict[str, Any]], type_configs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """跑一条完整漏斗：清洗 → day1 去重 → 补全复判。"""
    from src import health, main as main_mod, process, rss

    drop_stats: dict[str, int] = {}
    funnel = health.Funnel()
    cleaned = process.process_and_clean(items, type_configs, drop_stats, funnel)
    # day1：跨轮去重基准为空集
    new_items = main_mod.filter_new_items(cleaned, set())
    before = len(new_items)
    rss.backfill_full_text(new_items)
    final, _ = process.drop_too_short(new_items, funnel)
    log.info(
        "[%s] 清洗 %d → day1 去重 %d → 补全复判 %d",
        name,
        len(cleaned),
        before,
        len(final),
    )
    return {
        "name": name,
        "funnel": funnel,
        "drop_stats": drop_stats,
        "cleaned": len(cleaned),
        "after_dedup": before,
        "final": final,
    }


def all_stages(*funnels: Any) -> list[str]:
    """按 health.FUNNEL_STAGES 的顺序排，白名单外的实际淘汰原因追加在后面。"""
    from src import health

    seen: list[str] = []
    present = {s for f in funnels for s in f.totals}
    for stage in health.FUNNEL_STAGES:
        if stage in present and stage not in {"raw", "kept"}:
            seen.append(stage)
    for stage in sorted(present - set(health.FUNNEL_STAGES)):
        seen.append(stage)
    return seen


def _lookback_label(hours: int) -> str:
    if hours % 24 == 0:
        return f"{hours // 24}d"
    return f"{hours}h"


def print_plan_report(
    plan: dict[str, Any],
    feed_by_id: dict[str, dict[str, Any]],
    body_by_source: dict[str, Counter[str]] | None,
) -> None:
    from src import health

    funnel: health.Funnel = plan["funnel"]
    final_by_source = Counter(
        str(it.get("source_id") or "") for it in plan["final"]
    )
    # 不能只按 health.FUNNEL_STAGES 展示：二级参数表的分支过滤会返回
    # min_quality_score / keyword_exclude / min_chars 等不在那份白名单里的原因，
    # 只认白名单会整类淘汰不显示。
    stages = all_stages(funnel)

    print(f"\n{'=' * 108}")
    print(f"方案 {plan['name']}：清洗保留 {plan['cleaned']} 条 → "
          f"day1 去重后 {plan['after_dedup']} 条 → 最终 {len(plan['final'])} 条")
    print("=" * 108)
    header = f"{'源 ID':<26}{'方式':<8}{'时间窗':>7}{'raw':>6}{'入库':>6}   淘汰明细"
    print(header)
    print("-" * 108)

    source_ids = sorted(
        set(funnel.by_source) | set(final_by_source),
        key=lambda s: (-funnel.for_source(s).get("raw", 0), s),
    )
    for sid in source_ids:
        if not sid:
            continue
        per = funnel.for_source(sid)
        feed = feed_by_id.get(sid) or {}
        hours = int(feed.get("lookback_hours") or 168)
        drops = {s: per[s] for s in stages if per.get(s)}
        detail = "  ".join(f"{s}={n}" for s, n in drops.items()) or "-"
        if body_by_source and body_by_source.get(sid):
            comp = body_by_source[sid]
            tags = "/".join(f"{t}:{comp[t]}" for t in BODY_SOURCES if comp.get(t))
            detail = f"{detail}   [正文来源 {tags}]"
        print(
            f"{sid[:25]:<26}{str(feed.get('fetch_method') or '?'):<8}"
            f"{_lookback_label(hours):>7}{per.get('raw', 0):>6}"
            f"{final_by_source.get(sid, 0):>6}   {detail}"
        )

    print("-" * 108)
    print("全局淘汰合计：", json.dumps(funnel.drops(), ensure_ascii=False))
    if plan["drop_stats"]:
        top = sorted(plan["drop_stats"].items(), key=lambda kv: -kv[1])[:8]
        print("本可通过其余过滤、仅因时间窗被丢：", dict(top))


def print_diff(plan_b: dict[str, Any], plan_a: dict[str, Any]) -> None:
    from src import health

    fb: health.Funnel = plan_b["funnel"]
    fa: health.Funnel = plan_a["funnel"]
    final_b = Counter(str(i.get("source_id") or "") for i in plan_b["final"])
    final_a = Counter(str(i.get("source_id") or "") for i in plan_a["final"])

    print(f"\n{'=' * 78}")
    print("两套方案差异（A 新抽取 − B 现状）")
    print("=" * 78)
    print(f"{'指标':<34}{'B 现状':>12}{'A 新抽取':>12}{'差':>10}")
    print("-" * 78)
    rows = [
        ("清洗保留", plan_b["cleaned"], plan_a["cleaned"]),
        ("day1 去重后", plan_b["after_dedup"], plan_a["after_dedup"]),
        ("最终入库", len(plan_b["final"]), len(plan_a["final"])),
    ]
    for stage in all_stages(fb, fa):
        b, a = fb.totals.get(stage, 0), fa.totals.get(stage, 0)
        if b or a:
            rows.append((f"淘汰·{stage}", b, a))
    for label, b, a in rows:
        mark = "" if a == b else ("↑" if a > b else "↓")
        print(f"{label:<34}{b:>12}{a:>12}{a - b:>9}{mark}")

    changed = sorted(
        (sid for sid in set(final_a) | set(final_b) if final_a[sid] != final_b[sid]),
        key=lambda s: -(final_a[s] - final_b[s]),
    )
    if changed:
        print("\n入库条数发生变化的源：")
        for sid in changed:
            print(f"  {sid:<28} B={final_b[sid]:<4} A={final_a[sid]:<4} "
                  f"差={final_a[sid] - final_b[sid]:+d}")
    else:
        print("\n没有任何源的入库条数发生变化。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--methods", default="RSS,Scrape,Media")
    parser.add_argument("--max-fetch", type=int, default=600)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    load_dotenv()

    from src import config, feishu

    config.validate()
    token = feishu.get_tenant_access_token()
    log.info("飞书 token 就绪（本次只读，不写任何表）")

    from src import typed_config

    records = feishu.read_param_records(token)
    type_configs = typed_config.load_typed_configs(token)
    log.info("源配置 %d 条，类型化配置命中 %d 个源", len(records), len(type_configs))

    enabled = {m.strip() for m in args.methods.split(",") if m.strip()}
    raw_items, feed_by_id = fetch_raw(records, type_configs, enabled)
    log.info("抓取到原始条目 %d 条", len(raw_items))
    if not raw_items:
        print("没抓到任何条目，无法对比")
        return 1

    cache = HtmlCache()
    items_a, body_sources, body_by_source = build_plan_a(
        raw_items, cache, args.max_fetch, args.workers
    )
    log.info(
        "回源抓取 %d 次，平均 %.0fms，失败 %s",
        cache.fetched,
        cache.total_ms / max(1, cache.fetched),
        dict(cache.errors) or "无",
    )
    log.info("A 方案正文来源构成：%s", dict(body_sources))

    plan_b = run_plan("B 现状", copy.deepcopy(raw_items), type_configs)
    plan_a = run_plan("A 新抽取", items_a, type_configs)

    print_plan_report(plan_b, feed_by_id, None)
    print_plan_report(plan_a, feed_by_id, body_by_source)
    print_diff(plan_b, plan_a)

    print(f"\nA 方案正文来源构成：{dict(body_sources)}")
    print(f"回源抓取 {cache.fetched} 次，失败 {dict(cache.errors) or '无'}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dump = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "methods": sorted(enabled),
        "raw_total": len(raw_items),
        "body_sources": dict(body_sources),
        "fetch_errors": dict(cache.errors),
        "plans": {
            p["name"]: {
                "cleaned": p["cleaned"],
                "after_dedup": p["after_dedup"],
                "final": len(p["final"]),
                "drops": p["funnel"].drops(),
                "by_source": p["funnel"].by_source,
                "final_by_source": dict(
                    Counter(str(i.get("source_id") or "") for i in p["final"])
                ),
                "titles": [
                    {
                        "source_id": i.get("source_id"),
                        "title": i.get("title"),
                        "url": i.get("url"),
                        "chars": len(str(i.get("raw_content") or "")),
                    }
                    for i in p["final"]
                ],
            }
            for p in (plan_b, plan_a)
        },
    }
    path = OUT_DIR / "ab-pipeline.json"
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已写入 {path.relative_to(ROOT)}")
    return 0


def fetch_raw(
    records: list[dict[str, Any]],
    type_configs: dict[str, dict[str, Any]],
    enabled: set[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """镜像 main.run() 的抓取段，去掉所有飞书写操作。"""
    from src import main as main_mod, rss, scrape, sources, video

    raw_items: list[dict[str, Any]] = []
    feed_by_id: dict[str, dict[str, Any]] = {}

    if "RSS" in enabled:
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
            feed_by_id[str(feed.get("id") or "")] = feed
        log.info("RSS 源 %d 个", len(feed_sources))
        items, _stats = rss.fetch_feed_sources_with_stats(feed_sources)
        raw_items += items

    if "Scrape" in enabled:
        scrape_sources = main_mod._prepare_scrape_sources(
            feishu_records=records, type_configs=type_configs
        )
        for feed in scrape_sources:
            feed_by_id[str(feed.get("id") or "")] = feed
        log.info("Scrape 源 %d 个", len(scrape_sources))
        try:
            items, _stats = scrape.fetch_scrape_sources_with_stats(
                scrape_sources, engine="auto"
            )
            raw_items += items
        except Exception as exc:  # noqa: BLE001
            log.warning("Scrape 抓取失败，跳过：%s", exc)

    if "Media" in enabled:
        media = sources.map_media_sources(records)
        for feed in media:
            feed_by_id[str(feed.get("id") or "")] = feed
        log.info("Media 源 %d 个", len(media))
        try:
            raw_items += video.fetch_video_sources(media)
        except Exception as exc:  # noqa: BLE001
            log.warning("Media 抓取失败，跳过：%s", exc)

    return raw_items, feed_by_id


if __name__ == "__main__":
    raise SystemExit(main())
