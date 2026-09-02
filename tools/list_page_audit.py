"""列表页抽链覆盖度审计：暴露「规则漏看了页面上某整块区域」。

漏斗统计有个结构性盲区——它只能统计进了原料池的条目，列表页上从没被抽出来的
文章不会出现在任何淘汰原因里。这个工具补的就是那一层。

产出三样东西：

  output/list-audit/{源}.raw.html        原始页面落盘，供改完规则后复跑对比
  output/list-audit/{源}.annotated.html  彩色标注快照：每个链接按状态染色
  output/list-audit/index.html           汇总 + 各源区域清单表
  output/list-audit-worksheet.csv        五问判读表，判读列留空给人填

标注配色：
  绿  抽中，进了原料池
  红  同域但被过滤链挡掉，角标写明是哪条规则
  橙  通过了全部规则，但被 max_articles 截断（配置性截断，不是规则问题）
  灰  非同域或资源文件，规则正常排除

判定逻辑全部从 src.scrape 导入，并断言标绿集合 == scrape._extract_links_html()
的真实输出；不一致直接报错，避免给出一份抄错过滤链的报告。

用法：
    python -m tools.list_page_audit                      # 抓页面 + 标注
    python -m tools.list_page_audit --reannotate         # 复用落盘 HTML 重新标注
    python -m tools.list_page_audit --sources huxiu,caixin
    python -m tools.list_page_audit --max-articles 30    # 覆盖上限，验证截断问题
"""

from __future__ import annotations

import argparse
import csv
import html as html_mod
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output" / "list-audit"
WORKSHEET = ROOT / "output" / "list-audit-worksheet.csv"

REJECT_LABELS = {
    "kept": "抽中",
    "truncated": "被 max_articles 截断",
    "social_or_asset": "社媒/资源文件",
    "cross_host": "非同域",
    "empty_path_or_self": "空路径或列表页自身",
    "nav_title": "锚文本命中导航词",
    "depth": "路径深度检查",
    "path_filter": "路径白名单/黑名单",
    "duplicate": "同页重复",
}
# 灰色：规则正常排除，不参与漏看判断
NEUTRAL = {"social_or_asset", "cross_host", "empty_path_or_self"}
# 同一 URL 在页面上常被链三四次（图片、标题、「阅读更多」各一次），首次通过规则、
# 后几次记 duplicate。着色和统计都必须按 URL 的最终归宿，否则抽中的锚点会被后面的
# duplicate 覆盖成灰色，快照上一个绿的都看不到。
STATUS_PRIORITY = ("kept", "truncated", "nav_title", "depth", "path_filter")
# 判定漏看区域的门槛：区域内去重后的唯一 URL 数
SUSPECT_MIN_LINKS = 3


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


def classify_links(html: str, feed: dict[str, Any]) -> list[dict[str, Any]]:
    """把页面上每个 href 逐条走一遍 _extract_links_html 的过滤链并记录归因。

    控制流是重写的，但每个判定都调 scrape 里的同一个函数，所以规则本身不会漂移；
    最后由 verify_against_pipeline 断言结果一致。
    """
    from src import config, scrape

    src_url = feed["url"]
    src_host = scrape._host(src_url)
    list_path = scrape._path_of(src_url)
    strict = len(list_path) > 1

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for match in scrape._HREF_RE.finditer(html or ""):
        raw = match.group(1).strip()
        url = urljoin(src_url, raw)
        url = re.sub(r"[).,]+$", "", url.split("#")[0])

        reason = ""
        if scrape._SOCIAL.search(url):
            reason = "social_or_asset"
        elif scrape._host(url) != src_host:
            reason = "cross_host"
        else:
            path = scrape._path_of(url)
            if not path or url.rstrip("/") == src_url.rstrip("/"):
                reason = "empty_path_or_self"
            elif not scrape._link_depth_ok(
                path, feed, strict=strict, list_path=list_path
            ):
                reason = "depth"
            elif not scrape._path_allowed(path, feed):
                reason = "path_filter"
            elif url in seen:
                reason = "duplicate"
        if not reason:
            seen.add(url)
        rows.append({"raw_href": raw, "url": url, "reason": reason})

    # 通过全部规则的按生产同一套 recency key（含列表邻近日期）排序后截断
    max_n = int(feed.get("max_articles") or config.DEFAULT_MAX_ARTICLES)
    passed = [r for r in rows if not r["reason"]]
    # 给候选补 published_raw，才能与 _extract_links_html 的 _cand_recency_key 对齐
    for row in passed:
        url = row["url"]
        url_date = scrape._published_date_from_url(url)
        if url_date:
            row["published_raw"] = url_date
        else:
            # 用该锚点在页面上的首次出现位置估邻近日期
            row["published_raw"] = ""
    # 重新扫一遍锚点位置以填邻近日期（仅对尚无 URL 日期的）
    hits = []
    for match in scrape._HREF_RE.finditer(html or ""):
        raw = match.group(1).strip()
        url = urljoin(src_url, raw)
        url = re.sub(r"[).,]+$", "", url.split("#")[0])
        hits.append((match, url))
    by_url_pub = {r["url"]: r for r in passed}
    for index, (match, url) in enumerate(hits):
        row = by_url_pub.get(url)
        if not row or row.get("published_raw"):
            continue
        next_start = hits[index + 1][0].start() if index + 1 < len(hits) else None
        near = scrape._date_near_anchor(html or "", match.start(), match.end(), next_start)
        if near:
            row["published_raw"] = near

    order = sorted(
        {r["url"]: r for r in passed}.values(),
        key=scrape._cand_recency_key,
        reverse=True,
    )
    kept_urls = {r["url"] for r in order[:max_n]}
    for row in rows:
        if not row["reason"]:
            row["reason"] = "kept" if row["url"] in kept_urls else "truncated"
    return rows


def unique_status(rows: list[dict[str, Any]]) -> dict[str, str]:
    """每个 URL 的最终归宿：多次出现时取优先级最高的那个状态。"""
    order = {name: i for i, name in enumerate(STATUS_PRIORITY)}
    best: dict[str, str] = {}
    for row in rows:
        url, reason = row["url"], row["reason"]
        if reason in NEUTRAL:
            best.setdefault(url, reason)
            continue
        rank = order.get(reason, len(order))
        current = best.get(url)
        if current is None or rank < order.get(current, len(order) + 1):
            best[url] = reason
    return best


def routing_note(feed: dict[str, Any]) -> str:
    """这个源到底走不走通用抽链？不走的话本审计对它没有意义，必须写明。"""
    from src import scrape

    if scrape._is_json_api_feed(feed):
        return "整源走 JSON API（ModelScope / Seed / GitHub Search），不做列表页抽链"
    if scrape._is_hf_pwc_paper_feed(feed):
        return "走 HF/PwC 论文榜专用解析器，不走通用抽链"
    if scrape._is_anthropic_news_feed(feed):
        return "走 Anthropic /news 专用解析器，不走通用抽链"
    if str(scrape._feed_extra(feed).get("list_parser") or "").strip() == "zhipu_news":
        return "走 zhipu_news 专用解析器，不走通用抽链"
    return ""


def verify_against_pipeline(
    rows: list[dict[str, Any]], html: str, feed: dict[str, Any]
) -> str:
    """断言标绿集合与生产代码一致；返回空串表示一致。

    比的是 _extract_links_for_feed(use_jina=False)——那才是 direct 引擎下的真实
    路由，直接比 _extract_links_html 会在专用解析器的源上假报不一致。
    """
    from src import scrape

    mine = {r["url"] for r in rows if r["reason"] == "kept"}
    theirs = {
        link["url"]
        for link in scrape._extract_links_for_feed(html, feed, use_jina=False)
    }
    if mine == theirs:
        return ""
    only_mine = sorted(mine - theirs)[:3]
    only_theirs = sorted(theirs - mine)[:3]
    return (
        f"自校验不一致：我多出 {len(mine - theirs)} 条 {only_mine}，"
        f"少了 {len(theirs - mine)} 条 {only_theirs}"
    )


def region_of(element: Any, depth: int = 6) -> str:
    """取链接最近的带 id/class 的祖先当区域标识。"""
    node = element
    for _ in range(depth):
        node = node.getparent()
        if node is None:
            break
        ident = node.get("id")
        if ident:
            return f"{node.tag}#{ident}"
        cls = (node.get("class") or "").split()
        if cls:
            return f"{node.tag}.{cls[0]}"
    return "(无标识容器)"


def annotate(html: str, feed: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """生成标注快照 + 区域清单。"""
    import lxml.html as LH
    from src import scrape

    src_url = feed["url"]
    by_url = unique_status(rows)

    tree = LH.fromstring(html)
    # 去掉脚本：站点 JS 会重渲染 DOM 把标注抹掉，静态快照更可靠
    for bad in tree.xpath("//script|//noscript"):
        bad.getparent().remove(bad)

    head = tree.find("head")
    if head is None:
        head = LH.Element("head")
        tree.insert(0, head)
    base = LH.Element("base")
    base.set("href", src_url)
    head.insert(0, base)

    region_rows: dict[str, Counter[str]] = defaultdict(Counter)
    region_samples: dict[str, list[str]] = defaultdict(list)
    # 被挡链接的路径首段：/member/ ×19 一眼就是作者页，不用人工逐条看
    region_prefix: dict[str, Counter[str]] = defaultdict(Counter)
    # 区域清单按去重后的唯一 URL 统计：同一篇文章在一张卡片里常被链三四次，
    # 按 href 计数会把卡片内层包装（div.card-top__bottom 之类）撑成一堆假区域。
    counted_urls: set[str] = set()

    for anchor in tree.xpath("//a[@href]"):
        raw = (anchor.get("href") or "").strip()
        url = re.sub(r"[).,]+$", "", urljoin(src_url, raw).split("#")[0])
        reason = by_url.get(url)
        if reason is None:
            continue
        anchor.set("data-fa", REJECT_LABELS.get(reason, reason))
        anchor.set(
            "class",
            f"{anchor.get('class') or ''} fa-{'neutral' if reason in NEUTRAL else reason}".strip(),
        )
        if reason in NEUTRAL or url in counted_urls:
            continue
        counted_urls.add(url)
        key = region_of(anchor)
        region_rows[key][reason] += 1
        if reason not in {"kept", "truncated"}:
            segs = [s for s in scrape._path_of(url).split("/") if s]
            region_prefix[key][f"/{segs[0]}/" if segs else "/"] += 1
        text = re.sub(r"\s+", " ", anchor.text_content() or "").strip()
        if len(region_samples[key]) < 3 and text:
            region_samples[key].append(f"{text[:60]} → {url}")

    counts = Counter(by_url.values())
    style = LH.Element("style")
    style.text = _CSS
    head.append(style)

    body = tree.find("body")
    if body is not None:
        bar = LH.fromstring(_bar_html(feed, counts))
        body.insert(0, bar)

    regions = []
    for key, c in sorted(region_rows.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        kept = c.get("kept", 0)
        blocked = total - kept - c.get("truncated", 0)
        top = [
            (REJECT_LABELS.get(k, k), v)
            for k, v in c.most_common()
            if k not in {"kept", "truncated"}
        ]
        regions.append(
            {
                "region": key,
                "links": total,
                "kept": kept,
                "truncated": c.get("truncated", 0),
                "blocked": blocked,
                "top_reason": f"{top[0][0]}×{top[0][1]}" if top else "",
                "path_pattern": "  ".join(
                    f"{p}×{n}" for p, n in region_prefix.get(key, Counter()).most_common(2)
                ),
                "samples": region_samples.get(key, []),
                "suspect": kept == 0 and blocked >= SUSPECT_MIN_LINKS,
            }
        )
    return LH.tostring(tree, encoding="unicode", method="html"), regions


_CSS = """
a[data-fa] { position:relative !important; outline-offset:1px; }
a[data-fa]::after {
  content: attr(data-fa); position:absolute; left:0; top:100%;
  font:10px/1.3 -apple-system,sans-serif; white-space:nowrap;
  padding:1px 4px; border-radius:3px; z-index:99998; pointer-events:none;
  color:#fff; opacity:.92;
}
a.fa-kept { outline:3px solid #16a34a !important; background:#dcfce7 !important; }
a.fa-kept::after { background:#16a34a; }
a.fa-truncated { outline:3px solid #f59e0b !important; background:#fef3c7 !important; }
a.fa-truncated::after { background:#f59e0b; }
a.fa-nav_title, a.fa-depth, a.fa-path_filter {
  outline:3px solid #dc2626 !important; background:#fee2e2 !important; }
a.fa-nav_title::after, a.fa-depth::after,
a.fa-path_filter::after { background:#dc2626; }
a.fa-neutral { outline:1px dotted #bbb !important; }
a.fa-neutral::after { display:none; }
#fa-bar {
  position:fixed; left:0; right:0; top:0; z-index:99999;
  background:#111; color:#fff; padding:8px 14px;
  font:13px/1.5 -apple-system,"PingFang SC",sans-serif;
}
#fa-bar b { color:#fff; }
#fa-bar .sw { display:inline-block; width:10px; height:10px; border-radius:2px;
              margin:0 4px 0 12px; vertical-align:middle; }
"""


def _bar_html(feed: dict[str, Any], counts: Counter[str]) -> str:
    return (
        '<div id="fa-bar">'
        f'<b>{html_mod.escape(str(feed.get("id")))}</b> · '
        f'{html_mod.escape(str(feed.get("url")))} · '
        f'max_articles={feed.get("max_articles") or "默认"}'
        f'<span class="sw" style="background:#16a34a"></span>抽中 {counts.get("kept", 0)}'
        f'<span class="sw" style="background:#f59e0b"></span>截断 {counts.get("truncated", 0)}'
        f'<span class="sw" style="background:#dc2626"></span>规则挡掉 '
        f'{sum(v for k, v in counts.items() if k not in NEUTRAL | {"kept", "truncated"})}'
        f'<span class="sw" style="background:#bbb"></span>正常排除 '
        f'{sum(counts.get(k, 0) for k in NEUTRAL)}'
        "</div>"
    )


def write_index(results: list[dict[str, Any]]) -> None:
    blocks = []
    for res in sorted(results, key=lambda r: -len(r["suspects"])):
        sid = res["sid"]
        rows = "".join(
            f"<tr class=\"{'suspect' if r['suspect'] else ''}\">"
            f"<td><code>{html_mod.escape(r['region'])}</code></td>"
            f"<td>{r['links']}</td><td>{r['kept']}</td>"
            f"<td>{r['truncated']}</td><td>{r['blocked']}</td>"
            f"<td>{html_mod.escape(r['top_reason'])}</td>"
            f"<td><code>{html_mod.escape(r['path_pattern'])}</code></td>"
            f"<td class=\"s\">{html_mod.escape(' ｜ '.join(r['samples']))}</td></tr>"
            for r in res["regions"]
        )
        note = res.get("verify") or ""
        cls = "warn" if "自校验" in note or "抓不到" in note else "skip"
        warn = f'<p class="{cls}">{html_mod.escape(note)}</p>' if note else ""
        blocks.append(
            f"""<section>
<h2>{html_mod.escape(sid)}
  <small>{html_mod.escape(res["url"])} · 流水线引擎 {html_mod.escape(res["engine"])}
  · 疑似漏看区域 {len(res["suspects"])} 块</small></h2>
{warn}
<p><a href="{html_mod.escape(sid)}.annotated.html" target="_blank">打开标注快照 →</a>
   ｜ <a href="{html_mod.escape(res["url"])}" target="_blank">打开真实页面 →</a></p>
<table><thead><tr><th>区域</th><th>唯一URL</th><th>抽中</th><th>截断</th>
<th>被挡</th><th>主要原因</th><th>被挡路径模式</th><th>样例</th>
</tr></thead><tbody>{rows}</tbody></table>
</section>"""
        )

    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>列表页抽链覆盖度审计</title><style>
body {{ font:14px/1.6 -apple-system,"PingFang SC",sans-serif; max-width:1180px;
       margin:28px auto; padding:0 20px; }}
h1 {{ font-size:21px; }}
h2 {{ font-size:17px; margin:32px 0 6px; border-bottom:2px solid #111; padding-bottom:5px; }}
h2 small {{ font-weight:400; color:#888; font-size:13px; }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
th,td {{ border:1px solid #e5e5e5; padding:4px 7px; text-align:left; vertical-align:top; }}
th {{ background:#fafafa; }}
tr.suspect {{ background:#fee2e2; font-weight:600; }}
td.s {{ color:#666; font-weight:400; max-width:420px; }}
.warn {{ background:#fff1f0; border:1px solid #ffa39e; padding:8px 12px; border-radius:5px; }}
.skip {{ background:#f0f0f0; border:1px solid #d9d9d9; padding:8px 12px;
         border-radius:5px; color:#555; }}
.note {{ background:#fffbe6; border:1px solid #ffe58f; padding:10px 14px;
         border-radius:6px; margin:14px 0 24px; }}
code {{ background:#f5f5f5; padding:1px 4px; border-radius:3px; }}
</style></head><body>
<h1>列表页抽链覆盖度审计</h1>
<div class="note">
<b>判读五问</b>（每个源按顺序回答，填进 <code>output/list-audit-worksheet.csv</code>）：<br>
1. 标注快照能正常渲染吗？渲染不出来说明是 SPA，抽链规则无关，跳过后面四问。<br>
2. <b>先不看标注</b>，你在真实页面上数到几块文章列表区域？填数字。<br>
3. 下表里标红的（抽中=0 且链接≥3）是哪几块？逐块回答：它是文章列表吗？<br>
4. 快照里绿色的链接，有没有明显不是文章的（栏目页/标签页/作者页/分页）？<br>
5. 页面上最新一篇文章是哪篇？它在快照里是绿色的吗？<br><br>
<b>橙色是配置性截断，不是规则漏看</b>：那些链接通过了全部规则，只是超出
<code>max_articles</code>。想验证就用 <code>--max-articles 30</code> 重跑，别改规则。
</div>
{"".join(blocks)}
</body></html>"""
    (OUT_DIR / "index.html").write_text(doc, encoding="utf-8")


def write_worksheet(results: list[dict[str, Any]]) -> None:
    fields = [
        "源ID",
        "名称",
        "列表页URL",
        "流水线引擎",
        "同域链接数",
        "抽中",
        "被规则挡",
        "被截断",
        "疑似漏看区域数",
        "本审计是否适用",
        "Q1快照能渲染吗(能/SPA空白)",
        "Q2你数到几块文章列表区域",
        "Q3全红区域标识及是否文章列表",
        "Q4绿色里有非文章吗",
        "Q5最新文章标题",
        "Q5最新文章是否抽中(是/否)",
        "结论(规则漏看/配置截断/反爬/SPA/无问题)",
        "备注",
    ]
    with WORKSHEET.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for res in sorted(results, key=lambda r: -len(r["suspects"])):
            counts = res["counts"]
            writer.writerow(
                {
                    "源ID": res["sid"],
                    "名称": res["name"],
                    "列表页URL": res["url"],
                    "流水线引擎": res["engine"],
                    "同域链接数": sum(
                        v for k, v in counts.items() if k not in {"cross_host", "social_or_asset"}
                    ),
                    "抽中": counts.get("kept", 0),
                    "被规则挡": sum(
                        v
                        for k, v in counts.items()
                        if k not in NEUTRAL | {"kept", "truncated"}
                    ),
                    "被截断": counts.get("truncated", 0),
                    "疑似漏看区域数": len(res["suspects"]),
                    "本审计是否适用": res.get("verify") or "适用",
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default="", help="逗号分隔的源 ID，默认全部 Scrape 源")
    parser.add_argument("--reannotate", action="store_true", help="复用落盘 HTML，不重新抓")
    parser.add_argument("--max-articles", type=int, default=0, help="覆盖 max_articles")
    args = parser.parse_args()

    load_dotenv()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from src import config, feishu, main as main_mod, scrape, typed_config

    config.validate()
    token = feishu.get_tenant_access_token()
    records = feishu.read_param_records(token)
    type_configs = typed_config.load_typed_configs(token)
    feeds = main_mod._prepare_scrape_sources(
        feishu_records=records, type_configs=type_configs
    )
    wanted = {s.strip() for s in args.sources.split(",") if s.strip()}
    if wanted:
        feeds = [f for f in feeds if str(f.get("id")) in wanted]
    if not feeds:
        print("没有匹配的 Scrape 源")
        return 1

    engine = "jina" if scrape.probe_jina() else "direct"
    print(f"Scrape 源 {len(feeds)} 个；流水线当前引擎探测结果：{engine}")
    print("（标注快照需要 HTML，所以审计走 direct 抓页面，归因的是 direct 抽链链路）\n")

    results: list[dict[str, Any]] = []
    for i, feed in enumerate(feeds, 1):
        sid = str(feed.get("id") or "")
        if args.max_articles:
            feed = {**feed, "max_articles": args.max_articles}
        raw_path = OUT_DIR / f"{sid}.raw.html"

        if args.reannotate and raw_path.exists():
            html = raw_path.read_text(encoding="utf-8")
            note = "复用落盘"
        else:
            t0 = time.perf_counter()
            html = scrape._safe_direct_get(feed["url"])
            note = f"抓取 {time.perf_counter() - t0:.1f}s"
            if html:
                raw_path.write_text(html, encoding="utf-8")

        if not html:
            print(f"[{i}/{len(feeds)}] {sid:<26} 抓不到页面（反爬或超时），跳过")
            results.append(
                {
                    "sid": sid,
                    "name": str(feed.get("name") or ""),
                    "url": feed["url"],
                    "engine": engine,
                    "counts": Counter(),
                    "regions": [],
                    "suspects": [],
                    "verify": "页面抓不到：反爬或超时，不是抽链规则问题",
                }
            )
            continue

        rows = classify_links(html, feed)
        special = routing_note(feed)
        # 专用解析器的源不比对：通用抽链的输出跟它的实际取链天然不同
        verify = special or verify_against_pipeline(rows, html, feed)
        annotated, regions = annotate(html, feed, rows)
        (OUT_DIR / f"{sid}.annotated.html").write_text(annotated, encoding="utf-8")

        counts = Counter(unique_status(rows).values())
        # 不走通用抽链的源，★ 没有意义，不要让它污染判读清单
        suspects = [] if special else [r for r in regions if r["suspect"]]
        results.append(
            {
                "sid": sid,
                "name": str(feed.get("name") or ""),
                "url": feed["url"],
                "engine": engine,
                "counts": counts,
                "regions": regions,
                "suspects": suspects,
                "verify": verify,
            }
        )
        flag = "  ⚠ " + verify if verify else ""
        print(
            f"[{i}/{len(feeds)}] {sid:<26} {note:<14} 唯一URL {sum(counts.values()):4d} "
            f"抽中 {counts.get('kept', 0):3d} 截断 {counts.get('truncated', 0):3d} "
            f"规则挡 {sum(v for k, v in counts.items() if k not in NEUTRAL | {'kept', 'truncated'}):4d} "
            f"疑似漏看区域 {len(suspects)}{flag}"
        )

    write_index(results)
    write_worksheet(results)

    bad = [r for r in results if r.get("verify") and "自校验" in r["verify"]]
    print(f"\n自校验不一致的源：{len(bad)}")
    total_suspect = sum(len(r["suspects"]) for r in results)
    print(f"疑似漏看区域合计 {total_suspect} 块，分布在 "
          f"{sum(1 for r in results if r['suspects'])} 个源")
    print(f"\n{(OUT_DIR / 'index.html').relative_to(ROOT)}   ← 从这里开始看")
    print(f"{WORKSHEET.relative_to(ROOT)}   ← 判读填这里")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            [
                {k: v for k, v in r.items() if k not in {"regions", "suspects"}}
                | {"regions": r["regions"]}
                for r in results
            ],
            ensure_ascii=False,
            indent=2,
            default=dict,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
