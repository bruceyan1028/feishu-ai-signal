"""AI 信号情报报告生成器。

产出一份高度可视化、Perplexity 风格模块化的自包含 HTML 报告:
- 顶部「今日简报」答案块(带引用溯源)
- 主题聚类模块
- 逐条信号卡片(中文摘要 / 为何重要 / 影响·新颖·可行动评分 / 紧迫度 / 主题标签)
- 可交互筛选与排序(纯前端 JS,无需联网)

两种运行模式:
- demo 模式(默认):从 src/demo_report.json 读取「已分析」的真实数据,零依赖零密钥,直接出图。
    python -m src.report --demo
- 真实模式:从飞书「信号条目表」拉取 → 调用 LLM(OpenAI 兼容接口)分析 → 渲染。
    python -m src.report --real
"""
from __future__ import annotations

import argparse
import html
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("report")

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = Path("output")
DEMO_DATA = ROOT / "demo_report.json"
CACHE_FILE = OUTPUT_DIR / ".report_cache.json"

SCORE_META = [
    ("impact", "影响", "impact"),
    ("novelty", "新颖", "novelty"),
    ("actionability", "可行动", "action"),
]
URGENCY_CLASS = {"高": "u-high", "中": "u-mid", "低": "u-low"}


# --------------------------------------------------------------------------- #
# 数据加载与统计
# --------------------------------------------------------------------------- #
def load_demo() -> dict[str, Any]:
    return json.loads(DEMO_DATA.read_text(encoding="utf-8"))


def compute_stats(data: dict[str, Any]) -> dict[str, Any]:
    signals = data["signals"]
    sources = {s["source"] for s in signals}
    high_impact = [s for s in signals if s.get("impact", 0) >= 80]
    urgent = [s for s in signals if s.get("urgency") == "高"]
    topic_counter: Counter[str] = Counter()
    for s in signals:
        for t in s.get("topics", []):
            topic_counter[t] += 1
    cat_counter: Counter[str] = Counter(s.get("category", "其他") for s in signals)
    return {
        "total": len(signals),
        "sources": len(sources),
        "themes": len(data.get("themes", [])),
        "high_impact": len(high_impact),
        "urgent": len(urgent),
        "top_topics": topic_counter.most_common(8),
        "categories": cat_counter.most_common(),
    }


# --------------------------------------------------------------------------- #
# 真实模式:飞书读取 + LLM 分析
# --------------------------------------------------------------------------- #
def _cell(v: Any) -> str:
    if isinstance(v, list) and v:
        x = v[0]
        return x.get("text", "") if isinstance(x, dict) else str(x)
    if isinstance(v, dict):
        return v.get("text") or v.get("link") or ""
    return "" if v is None else str(v)


def _link(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("link", "")
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0].get("link", "")
    return ""


def _ms_to_date(v: Any) -> str:
    try:
        ms = int(float(v))
    except (ValueError, TypeError):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms > 0 else ""


ANALYSIS_PROMPT = """你是资深 AI 行业分析师。请阅读下面这条 AI 资讯,输出严格的 JSON(不要多余文字)。
字段:
- summary_cn: 用中文 1-2 句话客观概括核心内容。
- why: 用中文 1 句话说明「为何重要 / 对从业者的影响」。
- impact: 0-100 整数,行业影响力。
- novelty: 0-100 整数,新颖程度。
- actionability: 0-100 整数,对读者的可行动性。
- urgency: "高" / "中" / "低" 之一。
- topics: 2-4 个中文短标签的数组。

资讯:
标题: {title}
来源: {source}
分类: {category}
正文节选: {raw}
"""


def _llm_json(prompt: str) -> dict[str, Any]:
    import requests

    urls = [f"{config.LLM_BASE_URL}/chat/completions"]
    if not config.LLM_BASE_URL.endswith("/v1"):
        urls.append(f"{config.LLM_BASE_URL}/v1/chat/completions")
    resp = None
    for url in urls:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {config.LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
        if resp.status_code not in {404, 405}:
            break
    assert resp is not None
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def analyze_entry(entry: dict[str, Any]) -> dict[str, Any]:
    prompt = ANALYSIS_PROMPT.format(
        title=entry["title"],
        source=entry["source"],
        category=entry.get("category", ""),
        raw=(entry.get("raw") or "")[:2000],
    )
    a = _llm_json(prompt)
    return {
        "summary_cn": str(a.get("summary_cn", "")).strip(),
        "why": str(a.get("why", "")).strip(),
        "impact": int(a.get("impact", 0)),
        "novelty": int(a.get("novelty", 0)),
        "actionability": int(a.get("actionability", 0)),
        "urgency": a.get("urgency", "中") if a.get("urgency") in URGENCY_CLASS else "中",
        "topics": [str(t) for t in (a.get("topics") or [])][:4],
    }


def synthesize_briefing_and_themes(signals: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [
        f'[{s["id"]}] ({s["category"]}) {s["title"]} — {s["summary_cn"]}' for s in signals
    ]
    prompt = (
        "你是 AI 情报主编。基于下面已编号的信号条目,输出严格 JSON:\n"
        "- briefing: {headline: 一句话中文主线概括, points: [{text: 中文要点, cites: [引用的条目编号数组]}]},3-6 条要点。\n"
        "- themes: [{id: 英文短id, name: 中文主题名, takeaway: 一句话中文洞察, signal_ids: [编号数组]}],4-6 个主题,覆盖尽量多条目。\n\n"
        "信号条目:\n" + "\n".join(lines)
    )
    return _llm_json(prompt)


def build_report_from_feishu() -> dict[str, Any]:
    if not config.LLM_API_KEY:
        raise config.ConfigError("真实模式需要 LLM_API_KEY(可指向 DeepSeek/通义/OpenAI 等 OpenAI 兼容接口)")
    from . import feishu

    token = feishu.get_tenant_access_token()
    records = feishu.read_all_records(
        token,
        config.FEISHU_ENTRY_TABLE_ID,
        field_names=["标题", "链接", "来源", "来源类型", "分类", "层级", "发布时间", "采集时间", "原文"],
    )
    records.sort(key=lambda f: int(float(_cell(f.get("采集时间")) or 0)), reverse=True)
    records = records[: config.REPORT_MAX_ENTRIES]

    cache: dict[str, Any] = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    signals: list[dict[str, Any]] = []
    for i, f in enumerate(records, 1):
        url = _link(f.get("链接")) or _cell(f.get("链接"))
        entry = {
            "title": _cell(f.get("标题")),
            "source": _cell(f.get("来源")),
            "category": _cell(f.get("分类")),
            "raw": _cell(f.get("原文")),
        }
        key = url or entry["title"]
        if key in cache:
            analysis = cache[key]
        else:
            log.info("LLM 分析 %d/%d: %s", i, len(records), entry["title"][:40])
            analysis = analyze_entry(entry)
            cache[key] = analysis
        signals.append(
            {
                "id": i,
                "title": entry["title"],
                "source": entry["source"],
                "url": url,
                "category": entry["category"],
                "tier": _cell(f.get("层级")),
                "published": _ms_to_date(f.get("发布时间")),
                **analysis,
            }
        )

    OUTPUT_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    log.info("综合简报与主题聚类中…")
    synth = synthesize_briefing_and_themes(signals)
    return {
        "date_label": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "period_label": "近 7 日 · AI 信号",
        "briefing": synth.get("briefing", {"headline": "", "points": []}),
        "themes": synth.get("themes", []),
        "signals": signals,
    }


# --------------------------------------------------------------------------- #
# HTML 渲染
# --------------------------------------------------------------------------- #
def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


CSS = """
:root{
  --bg:#faf9f6; --surface:#ffffff; --surface-2:#f4f2ec; --border:#e9e5dd;
  --text:#1c1c1a; --muted:#6c6a62; --faint:#9a978d;
  --accent:#20808d; --accent-soft:#e2f0f1; --accent-ink:#0f5560;
  --impact:#20808d; --novelty:#6a7fb0; --action:#5f8f5a;
  --hi:#b5462f; --hi-soft:#f6e6e1; --mid:#9a6b1f; --mid-soft:#f5ecd9; --lo:#6c6a62; --lo-soft:#eeece6;
  --radius:14px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#14140f; --surface:#1d1d18; --surface-2:#23231d; --border:#2f2f27;
    --text:#ecebe4; --muted:#a5a396; --faint:#77756a;
    --accent:#4bb3c1; --accent-soft:#123338; --accent-ink:#8fd6df;
    --impact:#4bb3c1; --novelty:#8ea4d6; --action:#8bc084;
    --hi:#e88a70; --hi-soft:#3a2019; --mid:#d6ab5c; --mid-soft:#332a15; --lo:#a5a396; --lo-soft:#26261f;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased;}
.serif{font-family:"Songti SC",Georgia,"Times New Roman",serif;}
a{color:inherit;text-decoration:none}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 96px}

/* top bar */
.topbar{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--border)}
.topbar .inner{max-width:1080px;margin:0 auto;padding:12px 24px;display:flex;align-items:center;gap:12px}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;letter-spacing:.2px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--accent)}
.topbar .meta{margin-left:auto;color:var(--muted);font-size:13px;display:flex;gap:16px;flex-wrap:wrap}

/* hero briefing */
.hero{padding:40px 0 8px}
.eyebrow{color:var(--accent);font-size:12.5px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase}
.hero h1{font-size:30px;line-height:1.32;margin:14px 0 22px;font-weight:600;max-width:60ch}
.points{display:flex;flex-direction:column;gap:14px;max-width:78ch}
.point{display:flex;gap:12px;align-items:flex-start}
.point .marker{margin-top:9px;width:6px;height:6px;border-radius:50%;background:var(--accent);flex:0 0 auto}
.cite{display:inline-flex;align-items:center;justify-content:center;min-width:19px;height:19px;padding:0 5px;
  margin:0 2px;border-radius:6px;background:var(--accent-soft);color:var(--accent-ink);
  font-size:11.5px;font-weight:700;vertical-align:1px;cursor:pointer;transition:.15s}
.cite:hover{background:var(--accent);color:#fff}

/* stat strip */
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:34px 0 8px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px}
.stat .v{font-size:26px;font-weight:750;font-variant-numeric:tabular-nums}
.stat .k{color:var(--muted);font-size:12.5px;margin-top:2px}
.stat.accent .v{color:var(--accent)}
.stat.warn .v{color:var(--hi)}

/* section */
.sec{margin-top:44px}
.sec-h{display:flex;align-items:baseline;gap:10px;margin-bottom:16px}
.sec-h h2{font-size:20px;margin:0;font-weight:650}
.sec-h .sub{color:var(--muted);font-size:13px}

/* topic bars */
.topics{display:grid;grid-template-columns:1fr 1fr;gap:10px 28px}
.trow{display:flex;align-items:center;gap:12px}
.trow .lbl{width:88px;font-size:13px;color:var(--muted);text-align:right;flex:0 0 auto}
.trow .track{flex:1;height:9px;background:var(--surface-2);border-radius:6px;overflow:hidden}
.trow .fill{height:100%;background:var(--accent);border-radius:6px}
.trow .n{font-size:12px;color:var(--faint);width:20px;font-variant-numeric:tabular-nums}

/* themes */
.theme{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px 22px;margin-bottom:14px}
.theme h3{margin:0 0 6px;font-size:17px;font-weight:640}
.theme .take{color:var(--muted);font-size:14px;margin:0 0 14px;max-width:80ch}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:flex;align-items:center;gap:8px;background:var(--surface-2);border:1px solid var(--border);
  border-radius:10px;padding:7px 11px;font-size:12.5px;cursor:pointer;transition:.15s;max-width:340px}
.chip:hover{border-color:var(--accent);background:var(--accent-soft)}
.chip .sc{font-weight:750;color:var(--accent);font-variant-numeric:tabular-nums;flex:0 0 auto}
.chip .ct{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* toolbar */
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:18px}
.filters{display:flex;flex-wrap:wrap;gap:7px}
.fbtn{border:1px solid var(--border);background:var(--surface);color:var(--muted);border-radius:999px;
  padding:6px 13px;font-size:13px;cursor:pointer;transition:.15s;font-family:inherit}
.fbtn:hover{border-color:var(--accent)}
.fbtn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.sort{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}
.sort select{font-family:inherit;font-size:13px;padding:6px 10px;border-radius:9px;
  border:1px solid var(--border);background:var(--surface);color:var(--text)}

/* signal cards */
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px 19px;display:flex;flex-direction:column;gap:11px;scroll-margin-top:80px;transition:.2s}
.card.flash{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.card .top{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.badge{font-size:11px;font-weight:750;padding:2px 8px;border-radius:7px;background:var(--accent-soft);color:var(--accent-ink)}
.badge.num{background:var(--surface-2);color:var(--faint);font-variant-numeric:tabular-nums}
.urg{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px}
.urg.u-high{background:var(--hi-soft);color:var(--hi)} .urg.u-mid{background:var(--mid-soft);color:var(--mid)} .urg.u-low{background:var(--lo-soft);color:var(--lo)}
.card h4{margin:0;font-size:16px;line-height:1.4;font-weight:620}
.card h4 a:hover{color:var(--accent)}
.src{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12.5px;flex-wrap:wrap}
.src .dotsep{width:3px;height:3px;border-radius:50%;background:var(--faint)}
.summary{margin:0;font-size:14px;color:var(--text)}
.why{margin:0;font-size:13px;color:var(--muted);background:var(--surface-2);border-left:3px solid var(--accent);
  padding:9px 12px;border-radius:0 9px 9px 0}
.why b{color:var(--accent-ink);font-weight:700}
.scores{display:flex;gap:16px;margin-top:2px}
.score{flex:1}
.score .sh{display:flex;justify-content:space-between;font-size:11.5px;color:var(--muted);margin-bottom:4px}
.score .sh b{color:var(--text);font-variant-numeric:tabular-nums}
.score .track{height:6px;background:var(--surface-2);border-radius:5px;overflow:hidden}
.score .fill{height:100%;border-radius:5px}
.fill.impact{background:var(--impact)} .fill.novelty{background:var(--novelty)} .fill.action{background:var(--action)}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.tag{font-size:11.5px;color:var(--muted);background:var(--surface-2);border-radius:6px;padding:2px 8px}

/* sources */
.sources{margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:8px 24px}
.sitem{display:flex;gap:10px;font-size:13px;color:var(--muted);align-items:baseline}
.sitem .n{color:var(--accent);font-weight:700;flex:0 0 auto}
.sitem a{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sitem a:hover{color:var(--accent)}
.foot{margin-top:56px;padding-top:20px;border-top:1px solid var(--border);color:var(--faint);font-size:12.5px}

@media(max-width:760px){
  .stats{grid-template-columns:repeat(2,1fr)} .grid{grid-template-columns:1fr}
  .topics{grid-template-columns:1fr} .sources{grid-template-columns:1fr} .hero h1{font-size:24px}
}
"""

JS = """
const cites=document.querySelectorAll('.cite');
cites.forEach(c=>c.addEventListener('click',()=>{
  const el=document.getElementById('sig-'+c.dataset.id);
  if(!el)return; el.scrollIntoView({behavior:'smooth',block:'center'});
  el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1500);
}));
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>{
  const el=document.getElementById('sig-'+c.dataset.id);
  if(!el)return; el.scrollIntoView({behavior:'smooth',block:'center'});
  el.classList.add('flash'); setTimeout(()=>el.classList.remove('flash'),1500);
}));
const grid=document.getElementById('grid');
const cards=[...grid.children];
let curFilter='all';
function apply(){
  cards.forEach(c=>{c.style.display=(curFilter==='all'||c.dataset.cat===curFilter)?'':'none';});
}
document.querySelectorAll('.fbtn').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.fbtn').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); curFilter=b.dataset.cat; apply();
}));
document.getElementById('sortsel').addEventListener('change',e=>{
  const k=e.target.value;
  const s=[...cards].sort((a,b)=> k==='date'
    ? (b.dataset.date>a.dataset.date?1:-1)
    : (Number(b.dataset[k])-Number(a.dataset[k])));
  s.forEach(c=>grid.appendChild(c));
});
"""


def _score_bar(sig: dict[str, Any]) -> str:
    parts = []
    for key, label, cls in SCORE_META:
        v = int(sig.get(key, 0))
        parts.append(
            f'<div class="score"><div class="sh"><span>{label}</span><b>{v}</b></div>'
            f'<div class="track"><div class="fill {cls}" style="width:{v}%"></div></div></div>'
        )
    return f'<div class="scores">{"".join(parts)}</div>'


def _card(sig: dict[str, Any]) -> str:
    urg = sig.get("urgency", "中")
    tags = "".join(f'<span class="tag">{_esc(t)}</span>' for t in sig.get("topics", []))
    title = _esc(sig["title"])
    url = _esc(sig.get("url", ""))
    title_html = f'<a href="{url}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f"""<article class="card" id="sig-{sig['id']}"
  data-cat="{_esc(sig.get('category',''))}" data-impact="{sig.get('impact',0)}"
  data-novelty="{sig.get('novelty',0)}" data-action="{sig.get('actionability',0)}"
  data-date="{_esc(sig.get('published',''))}">
  <div class="top">
    <span class="badge num">#{sig['id']}</span>
    <span class="badge">影响 {sig.get('impact',0)}</span>
    <span class="urg {URGENCY_CLASS.get(urg,'u-mid')}">紧迫 {_esc(urg)}</span>
  </div>
  <h4>{title_html}</h4>
  <div class="src"><span>{_esc(sig.get('source',''))}</span><span class="dotsep"></span>
    <span>{_esc(sig.get('tier',''))}</span><span class="dotsep"></span>
    <span>{_esc(sig.get('category',''))}</span><span class="dotsep"></span>
    <span>{_esc(sig.get('published',''))}</span></div>
  <p class="summary">{_esc(sig.get('summary_cn',''))}</p>
  <p class="why"><b>为何重要</b> · {_esc(sig.get('why',''))}</p>
  {_score_bar(sig)}
  <div class="tags">{tags}</div>
</article>"""


def render_html(data: dict[str, Any]) -> str:
    stats = compute_stats(data)
    by_id = {s["id"]: s for s in data["signals"]}

    # hero points
    points_html = []
    for p in data["briefing"].get("points", []):
        cites = "".join(f'<span class="cite" data-id="{c}">{c}</span>' for c in p.get("cites", []))
        points_html.append(
            f'<div class="point"><span class="marker"></span>'
            f'<span>{_esc(p["text"])} {cites}</span></div>'
        )

    # stats
    stat_html = "".join(
        f'<div class="stat {cls}"><div class="v">{v}</div><div class="k">{k}</div></div>'
        for v, k, cls in [
            (stats["total"], "信号条目", ""),
            (stats["sources"], "覆盖来源", ""),
            (stats["themes"], "主题聚类", "accent"),
            (stats["high_impact"], "高影响 (≥80)", "accent"),
            (stats["urgent"], "需即时关注", "warn"),
        ]
    )

    # topics
    max_t = max((n for _, n in stats["top_topics"]), default=1)
    topic_html = "".join(
        f'<div class="trow"><span class="lbl">{_esc(t)}</span>'
        f'<span class="track"><span class="fill" style="width:{n/max_t*100:.0f}%"></span></span>'
        f'<span class="n">{n}</span></div>'
        for t, n in stats["top_topics"]
    )

    # themes
    theme_html = []
    for th in data.get("themes", []):
        chips = []
        for sid in th.get("signal_ids", []):
            s = by_id.get(sid)
            if not s:
                continue
            chips.append(
                f'<div class="chip" data-id="{sid}"><span class="sc">{s.get("impact",0)}</span>'
                f'<span class="ct">{_esc(s["title"])}</span></div>'
            )
        theme_html.append(
            f'<div class="theme"><h3>{_esc(th["name"])}</h3>'
            f'<p class="take">{_esc(th.get("takeaway",""))}</p>'
            f'<div class="chips">{"".join(chips)}</div></div>'
        )

    # filters
    cats = [c for c, _ in stats["categories"]]
    fbtns = ['<button class="fbtn on" data-cat="all">全部</button>']
    fbtns += [f'<button class="fbtn" data-cat="{_esc(c)}">{_esc(c)}</button>' for c in cats]

    # signal grid, default sorted by impact desc
    ordered = sorted(data["signals"], key=lambda s: -s.get("impact", 0))
    cards_html = "".join(_card(s) for s in ordered)

    # sources
    src_html = "".join(
        f'<div class="sitem"><span class="n">{s["id"]}</span>'
        f'<a href="{_esc(s.get("url",""))}" target="_blank" rel="noopener">{_esc(s["source"])} — {_esc(s["title"])}</a></div>'
        for s in data["signals"]
    )

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 信号情报简报 · {_esc(data.get('date_label',''))}</title>
<style>{CSS}</style></head><body>
<div class="topbar"><div class="inner">
  <div class="brand"><span class="dot"></span>AI Signal</div>
  <div class="meta"><span>{_esc(data.get('period_label',''))}</span>
    <span>{stats['total']} 条 · {stats['sources']} 源</span>
    <span>生成于 {gen}</span></div>
</div></div>
<div class="wrap">
  <header class="hero">
    <div class="eyebrow">今日情报简报 · {_esc(data.get('date_label',''))}</div>
    <h1 class="serif">{_esc(data['briefing'].get('headline',''))}</h1>
    <div class="points">{''.join(points_html)}</div>
  </header>

  <div class="stats">{stat_html}</div>

  <section class="sec">
    <div class="sec-h"><h2>主题热度</h2><span class="sub">按条目数 · Top {len(stats['top_topics'])}</span></div>
    <div class="topics">{topic_html}</div>
  </section>

  <section class="sec">
    <div class="sec-h"><h2>主题聚类</h2><span class="sub">多模型分析 · 点击卡片跳转原信号</span></div>
    {''.join(theme_html)}
  </section>

  <section class="sec">
    <div class="sec-h"><h2>全部信号</h2><span class="sub">按影响分排序</span></div>
    <div class="toolbar">
      <div class="filters">{''.join(fbtns)}</div>
      <div class="sort">排序
        <select id="sortsel">
          <option value="impact">影响分</option>
          <option value="novelty">新颖度</option>
          <option value="action">可行动性</option>
          <option value="date">时间</option>
        </select>
      </div>
    </div>
    <div class="grid" id="grid">{cards_html}</div>
  </section>

  <section class="sec">
    <div class="sec-h"><h2>信息来源</h2><span class="sub">引用溯源</span></div>
    <div class="sources">{src_html}</div>
  </section>

  <div class="foot">本报告由 feishu-ai-signal 自动生成 · 数据源自飞书「信号条目表」· 分析与评分由多模型产出,仅供参考</div>
</div>
<script>{JS}</script>
</body></html>"""


# --------------------------------------------------------------------------- #
def run(mode: str = "demo") -> int:
    if mode == "real":
        log.info("真实模式:从飞书拉取并调用 LLM 分析")
        data = build_report_from_feishu()
    else:
        log.info("demo 模式:使用 src/demo_report.json 中的已分析真实数据")
        data = load_demo()

    OUTPUT_DIR.mkdir(exist_ok=True)
    html_path = OUTPUT_DIR / "report.html"
    json_path = OUTPUT_DIR / "report.json"
    html_path.write_text(render_html(data), encoding="utf-8")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("报告已生成:%s", html_path.resolve())
    log.info("数据已导出:%s", json_path.resolve())
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI 信号情报报告生成器")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--demo", action="store_const", dest="mode", const="demo", help="使用内置真实样本数据(默认)")
    g.add_argument("--real", action="store_const", dest="mode", const="real", help="从飞书拉取并调用 LLM 分析")
    ap.set_defaults(mode="demo")
    args = ap.parse_args()
    raise SystemExit(run(args.mode))
