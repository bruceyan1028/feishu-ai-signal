"""新源接入时的日期抽取路线判定。

日期是唯一一个「错了会让下游全部失效」的字段：日期错→排序错→截断随机→
后面的关键词和长度过滤只能在已经抓错的那批里挑。所以它值得单独一道验收关。

做三件事：

  1. 按优先级探测全部日期载体，报告每一级命中什么值
  2. 自动校验：多载体互证、dateModified 冲突、列表页日期单调性
  3. 给出推荐路线 + 置信度 + 需要人工/LLM 复核的具体理由

判定结论落成一张「日期路线卡」，存进 output/date-routes/{源}.json。站点改版后
重跑就能发现路线失效——这是回归检测，不是一次性探测。

用法：
    python -m tools.date_route_probe --url https://airisk.mit.edu/blog
    python -m tools.date_route_probe --source pingwest        # 用飞书里的现有配置
    python -m tools.date_route_probe --source pingwest --llm  # 有分歧时调 LLM 复核
    python -m tools.date_route_probe --all-scrape             # 全部 Scrape 源体检
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output" / "date-routes"
CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; AI-Signal/1.0)"

# 与 scrape._PUBLISHED_KEYS 保持一致，另外单列「更新时间」键用于冲突检测
MODIFIED_KEYS = ("dateModified", "date_modified", "updatedAt", "_updatedAt", "lastmod")

# 相对时间：中文列表页最常见，且对时间窗判断最直接
_REL_RE = re.compile(r"(\d+)\s*(分钟|小时|天|周)前|(昨天|今天|前天)")
_ABS_RE = re.compile(
    r"20\d{2}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+20\d{2}"
)
_URL_DATE_RE = re.compile(r"/(20\d{2})[-/_]?(\d{2})[-/_]?(\d{2})(?:/|$|\.)")


@dataclass
class TierHit:
    tier: str
    label: str
    value: str = ""
    detail: str = ""

    @property
    def hit(self) -> bool:
        return bool(self.value)


@dataclass
class Card:
    source_id: str
    list_url: str
    article_urls: list[str] = field(default_factory=list)
    article_tiers: dict[str, list[TierHit]] = field(default_factory=dict)
    list_pairs: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    route: str = ""
    confidence: str = ""
    llm_review: str = ""


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


def fetch(url: str, timeout: int = 25) -> str:
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
        raw = resp.content[:1_500_000]
        # 编码检测链：header charset → chardet 嗅探 → utf-8。财新那类 header 说
        # UTF-8 但实际另一套的站点，只信 header 会整页乱码。
        for enc in (resp.encoding, resp.apparent_encoding, "utf-8"):
            if not enc:
                continue
            try:
                text = raw.decode(enc, errors="strict")
                if text.count("\ufffd") == 0:
                    return text
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        return f"__FETCH_ERROR__{type(exc).__name__}: {exc}"


def norm_date(raw: str, *, now: datetime | None = None) -> str:
    """把各种写法归一成 YYYY-MM-DD，认不出返回空串。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    now = now or datetime.now(CN_TZ)

    rel = _REL_RE.search(text)
    if rel:
        if rel.group(3):
            delta = {"今天": 0, "昨天": 1, "前天": 2}[rel.group(3)]
            return (now - timedelta(days=delta)).strftime("%Y-%m-%d")
        n = int(rel.group(1))
        unit = rel.group(2)
        hours = {"分钟": n / 60, "小时": n, "天": n * 24, "周": n * 168}[unit]
        return (now - timedelta(hours=hours)).strftime("%Y-%m-%d")

    iso = re.match(r"(20\d{2})-(\d{2})-(\d{2})", text)
    if iso:
        return "-".join(iso.groups())

    from src import process

    ms = process.parse_date_ms(text)
    if ms:
        return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%Y-%m-%d")
    abs_m = _ABS_RE.search(text)
    if abs_m:
        ms = process.parse_date_ms(abs_m.group(0))
        if ms:
            return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%Y-%m-%d")
    return ""


def probe_article(html: str, url: str) -> list[TierHit]:
    """按优先级逐级探测文章页的日期载体。"""
    from html import unescape

    from src import scrape

    body = unescape(html)
    keys = "|".join(re.escape(k) for k in scrape._PUBLISHED_KEYS)
    tiers: list[TierHit] = []

    meta = re.search(
        rf"""<meta\b[^>]*(?:property|name)=["'](?:{keys})["'][^>]*content=["']([^"']+)["']""",
        body,
        re.I,
    )
    tiers.append(TierHit("meta", "<meta> 标签", meta.group(1).strip() if meta else ""))

    ld_value = ""
    ld_blocks = re.findall(
        r"(?is)<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html
    )
    for block in ld_blocks:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', block)
        if m:
            ld_value = m.group(1).strip()
            break
    tiers.append(
        TierHit("jsonld", "JSON-LD datePublished", ld_value, f"LD 块 {len(ld_blocks)} 个")
    )

    # 关键：冒号后必须允许空白。JSON-LD/SSR 多为格式化输出，写法是 "key": "value"，
    # 现网 src/scrape.py:326 的正则漏了这个 \s*，导致整级失效。
    ssr = re.search(
        rf"""(?:{keys})\\?["']?\s*:\s*\\?["']([^"'\\<]{{8,80}})""", body, re.I
    )
    tiers.append(TierHit("ssr", "SSR payload 键", ssr.group(1).strip() if ssr else ""))

    t = re.search(r"""(?is)<time\b[^>]*datetime=["']([^"']+)["']""", body)
    tiers.append(TierHit("time", "<time datetime>", t.group(1).strip() if t else ""))

    header = ""
    h1 = re.search(r"(?is)<h1\b[^>]*>.*?</h1\s*>", body)
    if h1:
        text = scrape._html_to_text(body[h1.start() : h1.end() + 5000])
        hm = scrape._HEADER_DATE_WITH_READ_TIME_RE.search(text)
        if hm:
            header = hm.group(1).strip()
    tiers.append(TierHit("h1_adjacent", "h1 邻近日期", header))

    vis = re.search(
        r"(?:published|posted|发布日期|发布时间)\s*(?:on|[:：])?\s*"
        r"([A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2}|20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2})",
        scrape._html_to_text(body),
        re.I,
    )
    tiers.append(TierHit("visible", "可见「发布时间」文本", vis.group(1).strip() if vis else ""))

    um = _URL_DATE_RE.search(url)
    tiers.append(
        TierHit("url", "URL 路径日期", "-".join(um.groups()) if um else "")
    )

    mkeys = "|".join(re.escape(k) for k in MODIFIED_KEYS)
    mod = re.search(rf"""(?:{mkeys})\\?["']?\s*:\s*\\?["']([^"'\\<]{{8,80}})""", body, re.I)
    tiers.append(
        TierHit("_modified", "（对照）更新时间", mod.group(1).strip() if mod else "")
    )
    return tiers


def probe_list_dates(html: str, list_url: str) -> list[tuple[str, str]]:
    """列表页上「链接 → 邻近日期」配对，按文档顺序返回。"""
    import lxml.html as LH

    tree = LH.fromstring(html)
    for bad in tree.xpath("//script|//noscript"):
        bad.getparent().remove(bad)

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in tree.xpath("//a[@href]"):
        url = urljoin(list_url, (anchor.get("href") or "").strip()).split("#")[0]
        if url in seen or url.rstrip("/") == list_url.rstrip("/"):
            continue
        node = anchor
        raw = ""
        for _ in range(3):
            node = node.getparent()
            if node is None:
                break
            text = re.sub(r"\s+", " ", node.text_content() or "")
            m = _REL_RE.search(text) or _ABS_RE.search(text)
            if m:
                raw = m.group(0)
                break
        if raw:
            seen.add(url)
            pairs.append((url, raw))
    return pairs


def run_checks(card: Card) -> None:
    """三项自动校验：多载体互证、更新时间冲突、列表页单调性。"""
    # 1) 互证：各级归一后有几个不同的日期
    per_article: list[set[str]] = []
    for url, tiers in card.article_tiers.items():
        dates = {
            norm_date(t.value)
            for t in tiers
            if t.hit and not t.tier.startswith("_")
        }
        dates.discard("")
        per_article.append(dates)
        if len(dates) > 1:
            card.warnings.append(
                f"载体互相矛盾：{url[-42:]} 抽出多个不同日期 {sorted(dates)}"
            )
    agree = sum(1 for d in per_article if len(d) == 1)
    card.checks["互证一致的文章数"] = f"{agree}/{len(per_article)}"
    card.checks["多载体佐证数"] = (
        statistics.median(
            [
                sum(1 for t in tiers if t.hit and not t.tier.startswith("_"))
                for tiers in card.article_tiers.values()
            ]
        )
        if card.article_tiers
        else 0
    )

    # 2) 更新时间冲突：抽到的首发日期是不是其实等于更新时间
    for url, tiers in card.article_tiers.items():
        pub = next((norm_date(t.value) for t in tiers if t.hit and not t.tier.startswith("_")), "")
        mod = next((norm_date(t.value) for t in tiers if t.tier == "_modified" and t.hit), "")
        if pub and mod and pub == mod:
            card.warnings.append(
                f"首发时间与更新时间相同（{pub}），无法确认取到的是首发：{url[-42:]}"
            )
        elif pub and mod and mod < pub:
            card.warnings.append(f"更新时间早于首发时间，载体可疑：{url[-42:]}")

    # 3) 列表页单调性：列表页基本按时间倒序，日期不递减说明归属错了
    dates = [norm_date(raw) for _u, raw in card.list_pairs]
    dates = [d for d in dates if d]
    card.checks["列表页配到日期的链接数"] = len(card.list_pairs)
    if len(dates) >= 4:
        desc = sum(1 for a, b in zip(dates, dates[1:]) if a >= b)
        ratio = desc / (len(dates) - 1)
        card.checks["列表页日期单调递减比例"] = f"{ratio:.0%}"
        if ratio < 0.7:
            card.warnings.append(
                f"列表页日期不单调（仅 {ratio:.0%} 递减）：日期很可能被归属到了相邻链接上"
            )
    else:
        card.checks["列表页日期单调递减比例"] = "样本不足"

    # 4) 未来日期
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    for url, tiers in card.article_tiers.items():
        for t in tiers:
            d = norm_date(t.value)
            if d and d > today:
                card.warnings.append(f"抽到未来日期 {d}（{t.label}）：{url[-42:]}")


def decide_route(card: Card) -> None:
    """按命中率选推荐路线；置信度由自动校验结果决定。"""
    order = ["meta", "jsonld", "ssr", "time", "h1_adjacent", "visible", "url"]
    labels = {t.tier: t.label for tiers in card.article_tiers.values() for t in tiers}
    n = max(1, len(card.article_tiers))
    rates = {
        tier: sum(
            1
            for tiers in card.article_tiers.values()
            if any(t.tier == tier and t.hit for t in tiers)
        )
        / n
        for tier in order
    }
    winner = next((t for t in order if rates.get(t, 0) >= 0.99), "")
    if not winner:
        winner = max(order, key=lambda t: rates.get(t, 0))

    if not card.article_tiers:
        # 没探到样本 ≠ 没有可用路线，两者的处置完全不同
        card.route = "未探测"
        card.confidence = "无"
        card.warnings.append("没取到样本文章页，日期路线未探测——先解决抽链或反爬")
    elif rates.get(winner, 0) == 0:
        list_ok = len(card.list_pairs) >= 3
        card.route = "list_neighbor" if list_ok else "无可用路线"
        card.confidence = "低" if list_ok else "无"
        if not list_ok:
            card.warnings.append(
                "文章页与列表页都没抽到日期：需要逐源定制 date_selector，或该源不适合接入"
            )
    else:
        card.route = winner
        pct = rates[winner]
        blocking = [w for w in card.warnings if "不单调" in w or "矛盾" in w or "未来日期" in w]
        if blocking or pct < 0.6:
            card.confidence = "低"
        elif card.warnings:
            # 有疑点就不能给「高」，否则人会跳过复核——而复核正是这套东西的重点
            card.confidence = "中"
        elif pct >= 0.99:
            card.confidence = "高"
        else:
            card.confidence = "中"
    card.checks["各级命中率"] = {
        labels.get(t, t): f"{rates.get(t, 0):.0%}" for t in order
    }


def llm_review(card: Card) -> str:
    """只在自动校验有分歧时调用；确定的情况不花这个钱。"""
    from src import report

    tiers_dump = {
        url: {t.label: t.value for t in tiers if t.hit}
        for url, tiers in card.article_tiers.items()
    }
    prompt = (
        "你在审核一个新闻源的「发布时间抽取」是否可靠。下面是从该源若干文章页上，"
        "用不同方法抽到的候选日期，以及自动校验发现的疑点。\n\n"
        f"源：{card.source_id}\n列表页：{card.list_url}\n"
        f"各文章页抽到的候选：{json.dumps(tiers_dump, ensure_ascii=False)}\n"
        f"自动校验疑点：{json.dumps(card.warnings, ensure_ascii=False)}\n"
        f"推荐路线：{card.route}\n\n"
        "请判断：这条推荐路线抽到的是文章的首发时间，还是可能是更新时间/抓取时间/"
        "相邻文章的时间？返回 JSON："
        '{"verdict":"可用|存疑|不可用","reason":"一句话","suggest":"若不可用，建议改用哪一级"}'
    )
    try:
        out = report._llm_json(prompt)
        return json.dumps(out, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - LLM 不可用不阻断探测
        return f"LLM 复核失败：{type(exc).__name__}: {exc}"


def pick_samples(list_html: str, list_url: str, card: Card) -> list[str]:
    """挑样本文章页。

    不能只从「邻近有日期的链接」里挑：列表页没日期的源会一篇样本都取不到，于是
    把「没取到样本」误报成「无可用路线」。优先用生产代码的抽链结果，它才是这个源
    真正会去抓的那批。
    """
    from src import scrape

    feed = {"id": card.source_id, "url": list_url}
    try:
        links = [
            str(link.get("url") or "")
            for link in scrape._extract_links_for_feed(list_html, feed, use_jina=False)
        ]
    except Exception:  # noqa: BLE001 - 抽链失败就退回启发式
        links = []
    if not links:
        host = scrape._host(list_url)
        links = [
            url
            for url, _raw in card.list_pairs
            if scrape._host(url) == host
        ]
    if not links:
        # 最后兜底：同域、路径至少两段的链接
        import lxml.html as LH

        host = scrape._host(list_url)
        seen: set[str] = set()
        tree = LH.fromstring(list_html)
        for anchor in tree.xpath("//a[@href]"):
            url = urljoin(list_url, (anchor.get("href") or "").strip()).split("#")[0]
            if scrape._host(url) != host or url in seen:
                continue
            if len([s for s in scrape._path_of(url).split("/") if s]) >= 2:
                seen.add(url)
                links.append(url)
    if not links:
        card.warnings.append("列表页上取不到任何同域文章链接：先查抽链规则，日期探测无从谈起")
    return links[:3]


def api_route_note(source_id: str, list_url: str) -> str:
    """走 JSON API 的源，日期由接口字段直接给出，探 HTML 没有意义。"""
    from src import scrape

    feed = {"id": source_id, "url": list_url}
    if scrape._is_json_api_feed(feed):
        return "整源走 JSON API（ModelScope / Seed / GitHub Search），日期取接口字段，不探 HTML"
    return ""


def probe(source_id: str, list_url: str, article_urls: list[str], use_llm: bool) -> Card:
    card = Card(source_id=source_id, list_url=list_url)

    api_note = api_route_note(source_id, list_url)
    if api_note:
        card.route = "api_field"
        card.confidence = "不适用"
        card.checks["说明"] = api_note
        return card

    list_html = fetch(list_url)
    if list_html.startswith("__FETCH_ERROR__"):
        card.warnings.append(f"列表页抓不到：{list_html[15:]}")
        list_html = ""
    else:
        card.list_pairs = probe_list_dates(list_html, list_url)

    if not article_urls and list_html:
        article_urls = pick_samples(list_html, list_url, card)

    card.article_urls = article_urls
    for url in article_urls:
        html = fetch(url)
        if html.startswith("__FETCH_ERROR__"):
            card.warnings.append(f"文章页抓不到：{url[-42:]} {html[15:]}")
            continue
        card.article_tiers[url] = probe_article(html, url)

    run_checks(card)
    decide_route(card)
    if use_llm and (card.confidence != "高" or card.warnings):
        card.llm_review = llm_review(card)
    return card


def print_card(card: Card) -> None:
    print(f"\n{'=' * 84}")
    print(f"日期路线卡  {card.source_id}")
    print(f"{'=' * 84}")
    print(f"列表页  {card.list_url}")
    print(f"样本文章 {len(card.article_urls)} 篇\n")

    if card.article_tiers:
        print(f"{'载体':<24}{'命中率':>8}   样例值")
        print("-" * 84)
        order = ["meta", "jsonld", "ssr", "time", "h1_adjacent", "visible", "url", "_modified"]
        for tier in order:
            hits = [
                t
                for tiers in card.article_tiers.values()
                for t in tiers
                if t.tier == tier and t.hit
            ]
            label = next(
                (
                    t.label
                    for tiers in card.article_tiers.values()
                    for t in tiers
                    if t.tier == tier
                ),
                tier,
            )
            n = len(card.article_tiers)
            rate = f"{len(hits)}/{n}"
            sample = hits[0].value[:34] if hits else "—"
            mark = "  ←推荐" if tier == card.route else ""
            print(f"{label:<24}{rate:>8}   {sample}{mark}")

    print(f"\n推荐路线：{card.route}    置信度：{card.confidence}")
    print("\n自动校验：")
    for key, value in card.checks.items():
        if isinstance(value, dict):
            print(f"  {key}：")
            for k, v in value.items():
                print(f"     {k:<24} {v}")
        else:
            print(f"  {key}：{value}")

    if card.warnings:
        print("\n需要复核的疑点：")
        for w in card.warnings:
            print(f"  ⚠ {w}")
    else:
        print("\n自动校验未发现疑点。")

    if card.llm_review:
        print(f"\nLLM 复核：{card.llm_review}")

    print("\n人工验收（填进路线卡 JSON 的 manual_review 字段）：")
    print("  1. 推荐路线抽到的值，和文章页上肉眼看到的发布时间一致吗？")
    print("  2. 那个值是首发时间，还是「最后更新」？")
    print("  3. 列表页上第一条（最新）的日期，和它文章页里的一致吗？")
    print("  4. 结论：可用 / 存疑 / 不可用（不可用则需配 date_selector）")


def save_card(card: Card) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{card.source_id}.json"
    payload = {
        "source_id": card.source_id,
        "list_url": card.list_url,
        "probed_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "route": card.route,
        "confidence": card.confidence,
        "checks": card.checks,
        "warnings": card.warnings,
        "llm_review": card.llm_review,
        "article_samples": {
            url: {t.label: t.value for t in tiers if t.hit}
            for url, tiers in card.article_tiers.items()
        },
        "list_pairs_sample": card.list_pairs[:10],
        "manual_review": {"verdict": "", "note": "", "reviewer": "", "date": ""},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="", help="列表页 URL（新源接入时用）")
    parser.add_argument("--source", default="", help="已有源 ID，从飞书读端点")
    parser.add_argument("--article", action="append", default=[], help="指定样本文章 URL")
    parser.add_argument("--all-scrape", action="store_true", help="全部 Scrape 源体检")
    parser.add_argument("--llm", action="store_true", help="有疑点时调 LLM 复核")
    args = parser.parse_args()

    load_dotenv()
    targets: list[tuple[str, str]] = []

    if args.url:
        targets.append((args.url.split("//")[-1].split("/")[0].replace("www.", ""), args.url))
    if args.source or args.all_scrape:
        from src import config, feishu, main as main_mod, typed_config

        config.validate()
        token = feishu.get_tenant_access_token()
        feeds = main_mod._prepare_scrape_sources(
            feishu_records=feishu.read_param_records(token),
            type_configs=typed_config.load_typed_configs(token),
        )
        for feed in feeds:
            sid = str(feed.get("id") or "")
            if args.all_scrape or sid == args.source:
                targets.append((sid, str(feed.get("url") or "")))
    if not targets:
        print("给一个 --url 或 --source，或用 --all-scrape")
        return 1

    cards = []
    for sid, url in targets:
        card = probe(sid, url, list(args.article), args.llm)
        print_card(card)
        path = save_card(card)
        print(f"\n路线卡已存 {path.relative_to(ROOT)}")
        cards.append(card)

    if len(cards) > 1:
        print(f"\n{'=' * 84}\n汇总\n{'=' * 84}")
        print(f"{'源':<28}{'推荐路线':<16}{'置信度':<8}疑点数")
        print("-" * 66)
        for c in sorted(cards, key=lambda x: (x.confidence != "低", x.source_id)):
            print(f"{c.source_id:<28}{c.route:<16}{c.confidence:<8}{len(c.warnings)}")
        print("\n验收约定：置信度非「高」或有疑点的源，先在参数表标 status=experimental，")
        print("人工复核填进路线卡的 manual_review 后再改 active。")
        print("改完飞书配置记得跑 python -m tools.export_seed 回写仓库快照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
