"""本地预览飞书每日卡片：把 notify.build_card 的真实 JSON 渲染成近似飞书样式的网页。

预览直接读生产代码的卡片结构，改 notify.py 后刷新即可，不会与线上样式漂移。
只写 output/，不连飞书、不发消息。

    python -m tools.preview_card
    python -m tools.preview_card --input site/data/brief-2026-08-22.json
"""
from __future__ import annotations

import argparse
import json
import re
import webbrowser
from html import escape
from pathlib import Path
from typing import Any

from src import notify

OUT_DIR = Path("output/card-preview")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_FONT_RE = re.compile(r"&lt;font color=&#x27;([\w-]+)&#x27;&gt;(.*?)&lt;/font&gt;")

# 飞书颜色枚举的浅色主题编码，取自开放平台「颜色枚举值」文档。
FEISHU_COLORS = {
    "blue": "#1456F0",
    "blue-100": "#E0E9FF",
    "turquoise": "#067062",
    "turquoise-100": "#C4F2EC",
    "orange": "#A44904",
    "orange-100": "#FEE7CD",
    "red": "#C02A26",
    "red-100": "#FEE3E2",
    "purple": "#7A35F0",
    "purple-100": "#EFE6FE",
    "indigo": "#4752E6",
    "indigo-100": "#E9EAFB",
    "lime": "#5C6D08",
    "lime-100": "#E3F0A3",
    "wathet": "#076A94",
    "wathet-100": "#CAEFFC",
    "grey": "#646a73",
    "grey-100": "#f2f3f5",
    "red-100": "#FEE3E2",
    "default": "transparent",
}

# 富文本组件 text_size 对应的字号，与飞书文档一致。
TEXT_SIZES = {
    "heading-2": "20px",
    "heading-3": "18px",
    "heading": "16px",
    "normal": "14px",
    "notation": "12px",
}


def _md(text: str) -> str:
    """卡片用到的 lark_md 子集：字色、粗体、行内代码、链接、换行。"""
    out = escape(str(text or ""))
    out = _FONT_RE.sub(
        lambda m: '<span style="color:{color}">{body}</span>'.format(
            color=FEISHU_COLORS.get(m.group(1), "inherit"), body=m.group(2)
        ),
        out,
    )
    out = _LINK_RE.sub(r'<a href="\2">\1</a>', out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _CODE_RE.sub(r"<code>\1</code>", out)
    return out.replace("\n", "<br>")


def _render(element: dict[str, Any]) -> str:
    tag = element.get("tag")
    if tag == "hr":
        return '<hr class="fs-hr">'
    if tag == "div":
        return f'<div class="fs-div">{_md((element.get("text") or {}).get("content"))}</div>'
    if tag == "markdown":
        align = str(element.get("text_align") or "left")
        size = TEXT_SIZES.get(str(element.get("text_size") or "normal"), "14px")
        return (
            f'<div class="fs-div" style="text-align:{align};font-size:{size}">'
            f'{_md(element.get("content"))}</div>'
        )
    if tag == "note":
        inner = " ".join(
            _md(item.get("content")) for item in element.get("elements") or []
        )
        return f'<div class="fs-note">{inner}</div>'
    if tag == "action":
        buttons = "".join(
            f'<a class="fs-btn {action.get("type", "default")}" href="{escape(str(action.get("url") or "#"))}">'
            f'{escape(str((action.get("text") or {}).get("content") or ""))}</a>'
            for action in element.get("actions") or []
        )
        return f'<div class="fs-action">{buttons}</div>'
    if tag == "column_set":
        columns = element.get("columns") or []
        total = sum(int(column.get("weight") or 1) for column in columns) or 1
        cells = "".join(
            '<div class="fs-col" style="flex:{grow};background:{bg}">{body}</div>'.format(
                grow=int(column.get("weight") or 1) / total,
                bg=FEISHU_COLORS.get(str(column.get("background_style") or "default"), "transparent"),
                body="".join(_render(child) for child in column.get("elements") or []),
            )
            for column in columns
        )
        band = FEISHU_COLORS.get(str(element.get("background_style") or "default"), "transparent")
        return f'<div class="fs-cols" style="background:{band}">{cells}</div>'
    if tag == "img":
        return '<div class="fs-note">[图片元素]</div>'
    return f'<div class="fs-note">[未预览的元素 {escape(str(tag))}]</div>'


def build_html(card: dict[str, Any], brief: dict[str, Any]) -> str:
    header = card.get("header") or {}
    title = str((header.get("title") or {}).get("content") or "")
    head_html = f'<div class="card-head">{escape(title)}</div>' if title else ""
    body = "\n".join(_render(element) for element in card.get("elements") or [])
    raw = escape(json.dumps(card, ensure_ascii=False, indent=2))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>飞书每日卡片预览 · {escape(str(brief.get('date') or ''))}</title>
<style>
:root {{ --ink:#1f2329; --meta:#8f959e; --line:#dee0e3; --brand:#245bdb; --red:#d83931; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font:14px/1.6 "PingFang SC","Microsoft YaHei",sans-serif; color:var(--ink);
       background:#f5f6f7; padding:32px; }}
h1 {{ font-size:18px; }}
.lead {{ color:var(--meta); margin:8px 0 24px; }}
.stage {{ display:flex; gap:32px; align-items:flex-start; flex-wrap:wrap; }}
.chat {{ background:#ebedf0; padding:20px; border-radius:12px; width:420px; }}
.chat.wide {{ width:640px; }}
.chat h2 {{ font-size:12px; color:var(--meta); font-weight:500; margin-bottom:10px; }}
.card {{ background:#fff; border-radius:8px; overflow:hidden;
         box-shadow:0 2px 8px rgba(31,35,41,.1); }}
.card-head {{ background:var(--red); color:#fff; font-size:15px; font-weight:600; padding:12px 16px; }}
.card-body {{ padding:14px 16px 16px; }}
.fs-div {{ font-size:14px; margin:8px 0; }}
.fs-div strong {{ font-weight:600; }}
.fs-note {{ font-size:12px; color:var(--meta); margin:6px 0; }}
.fs-hr {{ border:0; border-top:1px solid var(--line); margin:12px 0; }}
.fs-cols {{ display:flex; gap:8px; align-items:center; margin:14px 0 6px; border-radius:4px; }}
.fs-col {{ padding:7px 10px; border-radius:4px; }}
.fs-col > .fs-div {{ margin:0; font-size:13px; }}
.fs-col code {{ font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--meta);
                background:#f2f3f5; border-radius:3px; padding:1px 5px; }}
a {{ color:var(--brand); text-decoration:none; }}
.fs-action {{ margin-top:14px; }}
.fs-btn {{ display:block; text-align:center; padding:9px; border-radius:4px;
           font-size:14px; background:#f2f3f5; color:var(--ink); }}
.fs-btn.primary {{ background:var(--brand); color:#fff; }}
pre {{ background:#1f2329; color:#c9d1d9; padding:16px; border-radius:8px; width:420px;
       font:11px/1.6 ui-monospace,Menlo,monospace; max-height:760px; overflow:auto; }}
</style></head><body>
<h1>飞书每日卡片 · 纯文字版</h1>
<p class="lead">由 <code>notify.build_card</code> 的真实 JSON 渲染，改代码后重跑即可刷新。
窄卡模拟手机宽度，宽卡模拟电脑端 <code>wide_screen_mode</code>。</p>
<div class="stage">
  <div class="chat"><h2>手机</h2>
    <div class="card">{head_html}<div class="card-body">{body}</div></div></div>
  <div class="chat wide"><h2>电脑</h2>
    <div class="card">{head_html}<div class="card-body">{body}</div></div></div>
  <pre>{raw}</pre>
</div>
</body></html>
"""


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="site/data/brief-latest.json")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    brief = json.loads(Path(args.input).read_text(encoding="utf-8"))
    card = notify.build_card(brief, "https://example.github.io/feishu-ai-signal/?date=" + str(brief.get("date") or ""))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    page = OUT_DIR / "index.html"
    page.write_text(build_html(card, brief), encoding="utf-8")
    print(f"预览页 → {page.resolve()}")
    if not args.no_open:
        webbrowser.open(page.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
