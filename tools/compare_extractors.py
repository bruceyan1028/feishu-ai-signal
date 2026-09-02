"""离线对比正文抽取方案，不改动采集代码。

样本取自 site/data/brief-*.json 里已入库条目的真实 URL。每个 URL 只抓一次 HTML，
再把同一份 HTML 分别交给三种抽取方案，这样抽取器是唯一变量：

    current-asis  现状全流程 rss.fetch_article_content（自带 UA/12s 超时，可选 Jina 兜底）
    current       现状抽取器 rss.parse_article_html，但换 Chrome UA + 长超时
    trafilatura   同一份 HTML 交给 trafilatura

用法：
    python -m tools.compare_extractors                 # 每站 3 条
    python -m tools.compare_extractors --per-host 5
    python -m tools.compare_extractors --jina          # 真的调 Jina 兜底（慢、耗配额）
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
BRIEF_GLOB = "brief-*.json"
BRIEF_DIR = ROOT / "site" / "data"
OUT_PATH = ROOT / "output" / "extractor-compare.json"

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

# 样板文案残留探针：命中越多说明正文里混进了导航/推荐位/互动条
BOILERPLATE_PROBES = (
    r"read\s+more",
    r"subscribe",
    r"sign\s*(?:in|up)",
    r"log\s*in",
    r"share\s+this",
    r"follow\s+us",
    r"cookie",
    r"privacy\s+policy",
    r"terms\s+of\s+(?:use|service)",
    r"all\s+rights\s+reserved",
    r"related\s+(?:articles?|posts?|reading)",
    r"\d+\s*min(?:ute)?s?\s*read",
    r"相关阅读",
    r"推荐阅读",
    r"热门文章",
    r"扫码关注",
    r"关注我们",
    r"版权所有",
    r"未经授权",
    r"分钟阅读",
    r"点击下载",
    r"登录后",
)
_PROBE_RE = re.compile("|".join(BOILERPLATE_PROBES), re.I)

# 页面 SSR/JSON payload 被当成正文抽走的痕迹。这类污染比文案样板更隐蔽：
# 长度门槛照样能过，但送进 LLM 的就是一坨序列化数据。
_JUNK_RE = re.compile(
    r"\"_id\"|avatarUrl|\"updatedAt\"|\\n\",\"|<\\/|\\u00[0-9a-f]{2}"
    r"|__NEXT_DATA__|window\.__|\"@type\"|\"props\":|\"pageProps\"",
    re.I,
)


def load_samples(per_host: int, skip_hosts: set[str]) -> list[dict[str, str]]:
    by_host: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in sorted(BRIEF_DIR.glob(BRIEF_GLOB)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for signal in data.get("signals") or []:
            url = str(signal.get("url") or signal.get("link") or "")
            if not url.startswith("http"):
                continue
            host = urlparse(url).netloc
            if host in skip_hosts:
                continue
            bucket = by_host[host]
            if any(row["url"] == url for row in bucket) or len(bucket) >= per_host:
                continue
            bucket.append(
                {
                    "url": url,
                    "host": host,
                    "title": str(signal.get("title") or signal.get("title_cn") or ""),
                    "source": str(signal.get("source") or ""),
                }
            )
    return [row for bucket in by_host.values() for row in bucket]


def fetch_html(url: str, timeout: int) -> tuple[str, str, float, str]:
    """返回 (html, final_url, 耗时秒, error)。解码方式与 rss.fetch_article_content 一致。"""
    t0 = time.perf_counter()
    try:
        response = requests.get(url, headers={"User-Agent": CHROME_UA}, timeout=timeout)
        response.raise_for_status()
        html = response.content[:1_500_000].decode(
            response.encoding or "utf-8", errors="replace"
        )
        return html, response.url or url, time.perf_counter() - t0, ""
    except requests.RequestException as exc:
        return "", url, time.perf_counter() - t0, f"{type(exc).__name__}"


def probe_boilerplate(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _PROBE_RE.finditer(text or "")})


def probe_junk(text: str) -> int:
    return len(_JUNK_RE.findall(text or ""))


def run_one(row: dict[str, str], *, timeout: int, use_jina: bool) -> dict[str, Any]:
    from src import rss

    url, title = row["url"], row["title"]
    out: dict[str, Any] = dict(row)

    # ---- arm 1: 现状全流程（自带 UA + 12s 超时 + Jina 兜底）
    t0 = time.perf_counter()
    try:
        asis = rss.fetch_article_content(url, title=title)
        asis_text = str(asis.get("text") or "")
    except Exception as exc:  # noqa: BLE001
        asis_text = ""
        out["asis_error"] = f"{type(exc).__name__}: {exc}"
    out["asis"] = {
        "chars": len(asis_text),
        "ms": round((time.perf_counter() - t0) * 1000),
        "boilerplate": probe_boilerplate(asis_text),
        "junk": probe_junk(asis_text),
    }

    # ---- 共享抓取：Chrome UA + 长超时
    html, final_url, fetch_s, fetch_err = fetch_html(url, timeout)
    out["fetch"] = {
        "ms": round(fetch_s * 1000),
        "bytes": len(html),
        "error": fetch_err,
    }
    if not html:
        out["current"] = {
            "chars": 0,
            "images": 0,
            "ms": 0,
            "boilerplate": [],
            "junk": 0,
        }
        out["trafilatura"] = {
            "chars": 0,
            "ms": 0,
            "boilerplate": [],
            "junk": 0,
            "date": "",
            "returned_none": True,
        }
        out["_texts"] = {"current": "", "trafilatura": ""}
        return out

    # ---- arm 2: 现状抽取器 + 好 UA
    t0 = time.perf_counter()
    try:
        cur = rss.parse_article_html(html, final_url, title, 15000)
        cur_text = str(cur.get("text") or "")
        cur_imgs = len(cur.get("images") or [])
    except Exception as exc:  # noqa: BLE001
        cur_text, cur_imgs = "", 0
        out["current_error"] = f"{type(exc).__name__}: {exc}"
    out["current"] = {
        "chars": len(cur_text),
        "images": cur_imgs,
        "ms": round((time.perf_counter() - t0) * 1000),
        "boilerplate": probe_boilerplate(cur_text),
        "junk": probe_junk(cur_text),
    }

    # ---- arm 3: trafilatura（同一份 HTML）
    import trafilatura

    t0 = time.perf_counter()
    tra_text = ""
    tra_date = ""
    try:
        tra_text = (
            trafilatura.extract(
                html,
                url=final_url,
                output_format="markdown",
                include_tables=True,
                include_comments=False,
                include_images=False,
            )
            or ""
        )
        meta = trafilatura.extract_metadata(html, default_url=final_url)
        tra_date = str(getattr(meta, "date", "") or "") if meta else ""
    except Exception as exc:  # noqa: BLE001
        out["trafilatura_error"] = f"{type(exc).__name__}: {exc}"
    out["trafilatura"] = {
        "chars": len(tra_text),
        "ms": round((time.perf_counter() - t0) * 1000),
        "boilerplate": probe_boilerplate(tra_text),
        "junk": probe_junk(tra_text),
        "date": tra_date,
        "returned_none": not tra_text,
    }
    out["_texts"] = {"current": cur_text[:1200], "trafilatura": tra_text[:1200]}
    return out


def summarize(rows: list[dict[str, Any]], threshold: int) -> None:
    arms = ("asis", "current", "trafilatura")
    print(f"\n样本 {len(rows)} 条，达标门槛 = {threshold} 字（rss.FULLTEXT_MIN_CHARS）\n")

    print(
        f"{'方案':<14}{'达标':>7}{'达标率':>8}{'中位字数':>10}"
        f"{'样板命中':>10}{'JSON污染':>10}{'撞上限':>8}{'中位耗时':>10}"
    )
    print("-" * 77)
    for arm in arms:
        chars = [r[arm]["chars"] for r in rows]
        ok = [c for c in chars if c >= threshold]
        probes = sum(len(r[arm]["boilerplate"]) for r in rows)
        junk = sum(1 for r in rows if r[arm]["junk"])
        capped = sum(1 for c in chars if c >= 15000)
        mss = sorted(r[arm]["ms"] for r in rows)
        med = sorted(chars)[len(chars) // 2] if chars else 0
        print(
            f"{arm:<14}{len(ok):>7}{len(ok) / max(1, len(rows)) * 100:>7.0f}%"
            f"{med:>10}{probes:>10}{junk:>10}{capped:>8}"
            f"{mss[len(mss) // 2] if mss else 0:>9}ms"
        )

    print("\n按站点（达标数 / 样本数，括号为中位字数）")
    print(f"{'站点':<26}{'现状全流程':>14}{'现状+好UA':>14}{'trafilatura':>14}")
    print("-" * 68)
    by_host: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_host[r["host"]].append(r)
    for host in sorted(by_host, key=lambda h: -len(by_host[h])):
        hrows = by_host[host]
        cells = []
        for arm in arms:
            chars = sorted(r[arm]["chars"] for r in hrows)
            ok = sum(1 for c in chars if c >= threshold)
            cells.append(f"{ok}/{len(hrows)} ({chars[len(chars) // 2]})")
        print(f"{host:<26}{cells[0]:>14}{cells[1]:>14}{cells[2]:>14}")

    fetch_err = Counter(r["fetch"]["error"] for r in rows if r["fetch"]["error"])
    if fetch_err:
        print("\n共享抓取失败：", dict(fetch_err))

    # 现状全流程达标、但共享抓取那两路都归零的行，说明差异来自抓取而非抽取
    suspects = [
        r
        for r in rows
        if r["asis"]["chars"] >= threshold
        and r["current"]["chars"] < threshold
        and r["trafilatura"]["chars"] < threshold
    ]
    if suspects:
        print("\n现状达标但共享抓取归零（差异来自抓取层，不是抽取器）：")
        for r in suspects:
            print(
                f"  {r['host']:<22} asis={r['asis']['chars']:<6} "
                f"共享抓取 bytes={r['fetch']['bytes']:<8} err={r['fetch']['error'] or '-'}"
            )

    none_cnt = sum(1 for r in rows if r["trafilatura"]["returned_none"])
    date_cnt = sum(1 for r in rows if r["trafilatura"]["date"])
    print(f"\ntrafilatura 明确返回空（宁缺毋滥）：{none_cnt}/{len(rows)}")
    print(f"trafilatura 顺带抽到发布日期：{date_cnt}/{len(rows)}")

    wins = sum(
        1
        for r in rows
        if r["trafilatura"]["chars"] >= threshold and r["current"]["chars"] < threshold
    )
    losses = sum(
        1
        for r in rows
        if r["current"]["chars"] >= threshold and r["trafilatura"]["chars"] < threshold
    )
    print(f"\ntrafilatura 救回（现状不达标→它达标）：{wins} 条")
    print(f"trafilatura 变差（现状达标→它不达标）：{losses} 条")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-host", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--skip-hosts",
        default="www.youtube.com",
        help="逗号分隔；默认跳过 YouTube（没有正文可抽）",
    )
    parser.add_argument(
        "--jina",
        action="store_true",
        help="现状全流程真的调 Jina 兜底；默认打桩，只统计触发次数",
    )
    args = parser.parse_args()

    from src import scrape

    jina_calls = Counter()
    if not args.jina:
        original = scrape._safe_jina_get

        def stub(url: str, list_mode: bool) -> str:
            jina_calls[url] += 1
            return ""

        scrape._safe_jina_get = stub  # type: ignore[assignment]
        del original

    samples = load_samples(args.per_host, set(filter(None, args.skip_hosts.split(","))))
    if not samples:
        print("没找到样本，检查 site/data/brief-*.json")
        return 1
    print(f"取样 {len(samples)} 条，来自 {len({s['host'] for s in samples})} 个站点")

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(run_one, s, timeout=args.timeout, use_jina=args.jina): s
            for s in samples
        }
        for i, future in enumerate(as_completed(futures), 1):
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}] 异常 {futures[future]['url']}: {exc}")
            if i % 10 == 0:
                print(f"  ...{i}/{len(samples)}")

    from src import rss

    summarize(rows, rss.FULLTEXT_MIN_CHARS)
    if not args.jina:
        print(f"\n现状全流程触发 Jina 兜底：{sum(jina_calls.values())}/{len(rows)} 条")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"明细已写入 {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
