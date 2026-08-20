"""把每日简报渲染成一张摘要图，供飞书卡片当头图使用。

视觉沿用网页端：纸感白底、墨线直角边框、偏移投影、类别色标头。
只依赖 PyMuPDF 内置的简体中文字体，不引入额外字体或图形库。
"""
from __future__ import annotations

import logging
from typing import Any

import fitz

log = logging.getLogger(__name__)

# 与 index.html 的 :root 设计变量保持一致
INK = "#16130f"
ACCENT = "#c22f2a"
TEXT_2 = "#4a453d"
TEXT_3 = "#6f685e"
PAPER = "#ffffff"
BAR = "#f4f1ea"
HAIR = "#d7d3ca"
CAT_COLORS = [
    "#2b6cb0",
    "#1f7a45",
    "#a06600",
    "#5a4fb5",
    "#c22f2a",
    "#b23a6a",
    "#bf4a1e",
    "#2f6b2f",
]

CN = "china-s"
LATIN = "helv"
LATIN_BOLD = "hebo"
MONO = "cour"
WIDTH = 760
MARGIN = 26
SHADOW = 5
MAX_GROUPS = 4
MAX_ITEMS_PER_GROUP = 3
MAX_ITEMS = 8


def _rgb(value: str) -> tuple[float, float, float]:
    raw = value.lstrip("#")
    return tuple(int(raw[index : index + 2], 16) / 255 for index in (0, 2, 4))


def _runs(text: str, latin: str) -> list[tuple[str, str]]:
    """内置中文字体会把西文画成全角，故按字符切段：ASCII 走西文字体，其余走中文字体。"""
    segments: list[tuple[str, str]] = []
    buffer = ""
    current = ""
    for char in text:
        font = latin if char.isascii() else CN
        if current and font != current:
            segments.append((buffer, current))
            buffer = ""
        current = font
        buffer += char
    if buffer:
        segments.append((buffer, current))
    return segments


def _width(text: str, size: float, font: str = LATIN) -> float:
    return sum(
        fitz.get_text_length(segment, fontname=name, fontsize=size)
        for segment, name in _runs(text, font)
    )


def _wrap(text: str, size: float, limit: float, font: str = LATIN) -> list[str]:
    """按可用宽度断行；中文逐字、英文按词，避免把单词从中间劈开。"""
    lines: list[str] = []
    current = ""
    word = ""

    def flush_word() -> None:
        nonlocal current, word
        if not word:
            return
        if _width(current + word, size, font) <= limit:
            current += word
        else:
            if current:
                lines.append(current)
            current = word
        word = ""

    for char in str(text or "").strip():
        if char.isascii() and not char.isspace():
            word += char
            continue
        flush_word()
        if char.isspace():
            if current and _width(current + " ", size, font) <= limit:
                current += " "
            continue
        if _width(current + char, size, font) <= limit:
            current += char
        else:
            lines.append(current)
            current = char
    flush_word()
    if current:
        lines.append(current)
    return lines


def _clip(lines: list[str], keep: int) -> list[str]:
    if len(lines) <= keep:
        return lines
    trimmed = lines[:keep]
    trimmed[-1] = trimmed[-1].rstrip() + "…"
    return trimmed


def group_signals(signals: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """按分类聚合，保留原有排序（已按质量与影响分排过）。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        name = str(signal.get("category") or "其他").strip() or "其他"
        grouped.setdefault(name, []).append(signal)
    ordered = sorted(grouped.items(), key=lambda item: -len(item[1]))[:MAX_GROUPS]
    result: list[tuple[str, list[dict[str, Any]]]] = []
    used = 0
    for name, items in ordered:
        if used >= MAX_ITEMS:
            break
        take = items[: min(MAX_ITEMS_PER_GROUP, MAX_ITEMS - used)]
        used += len(take)
        result.append((name, take))
    return result


class _Canvas:
    """先记录绘制指令，算出真实高度后再一次成页，避免估高误差留白。"""

    def __init__(self) -> None:
        self.ops: list[tuple] = []
        self.y = 0.0

    def rect(self, x0: float, y0: float, x1: float, y1: float, *, fill: str | None, stroke: str | None, width: float = 1.5) -> None:
        self.ops.append(("rect", x0, y0, x1, y1, fill, stroke, width))

    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: float,
        color: str,
        font: str = LATIN,
        bold: bool = False,
    ) -> None:
        self.ops.append(("text", x, y, content, size, color, font, bold))

    def flush(self, page: fitz.Page) -> None:
        for op in self.ops:
            if op[0] == "rect":
                _, x0, y0, x1, y1, fill, stroke, width = op
                page.draw_rect(
                    fitz.Rect(x0, y0, x1, y1),
                    color=_rgb(stroke) if stroke else None,
                    fill=_rgb(fill) if fill else None,
                    width=width if stroke else 0,
                )
            else:
                _, x, y, content, size, color, font, bold = op
                cursor = x
                for segment, name in _runs(content, font):
                    # 中文内置字体没有粗体，用极小偏移重绘一次模拟加粗。
                    offsets = (0.0, 0.32) if bold else (0.0,)
                    for shift in offsets:
                        page.insert_text(
                            (cursor + shift, y),
                            segment,
                            fontname=name,
                            fontsize=size,
                            color=_rgb(color),
                        )
                    cursor += fitz.get_text_length(segment, fontname=name, fontsize=size)


def _draw_header(canvas: _Canvas, brief: dict[str, Any], signal_count: int) -> None:
    top = MARGIN
    height = 66
    canvas.rect(MARGIN, top, WIDTH - MARGIN, top + height, fill=BAR, stroke=INK)
    canvas.rect(MARGIN, top, MARGIN + 8, top + height, fill=ACCENT, stroke=None)
    canvas.text(MARGIN + 22, top + 30, "AI SIGNAL 每日情报", size=17, color=INK, font=LATIN_BOLD, bold=True)
    canvas.text(
        MARGIN + 22,
        top + 50,
        f"{brief.get('date', '')} · 精选 {signal_count} 条",
        size=10.5,
        color=TEXT_3,
        font=MONO,
    )
    canvas.y = top + height + 16


def _draw_intro(canvas: _Canvas, intro: str) -> None:
    if not intro:
        return
    inner = WIDTH - MARGIN * 2
    lines = _clip(_wrap(intro, 11.5, inner - 32), 3)
    height = 18 + len(lines) * 18
    top = canvas.y
    canvas.rect(MARGIN + SHADOW, top + SHADOW, WIDTH - MARGIN + SHADOW, top + height + SHADOW, fill=HAIR, stroke=None)
    canvas.rect(MARGIN, top, WIDTH - MARGIN, top + height, fill=PAPER, stroke=INK)
    for index, line in enumerate(lines):
        canvas.text(MARGIN + 16, top + 22 + index * 18, line, size=11.5, color=TEXT_2)
    canvas.y = top + height + 18


def _draw_group(canvas: _Canvas, name: str, items: list[dict[str, Any]], color: str, start_index: int) -> int:
    inner = WIDTH - MARGIN * 2
    strip = 26
    rows: list[tuple[list[str], str]] = []
    for offset, signal in enumerate(items):
        title = str(signal.get("titleCn") or signal.get("title") or "").strip()
        lines = _clip(_wrap(title, 12.5, inner - 74), 2)
        meta = " · ".join(
            part
            for part in (
                str(signal.get("source") or "").strip(),
                f"影响分 {int(signal.get('impact') or 0)}",
                str(signal.get("contentType") or "").strip(),
            )
            if part
        )
        rows.append((lines, meta))
    body = sum(len(lines) * 17 + 16 + 12 for lines, _ in rows) + 6
    height = strip + body
    top = canvas.y
    canvas.rect(MARGIN + SHADOW, top + SHADOW, WIDTH - MARGIN + SHADOW, top + height + SHADOW, fill=HAIR, stroke=None)
    canvas.rect(MARGIN, top, WIDTH - MARGIN, top + height, fill=PAPER, stroke=INK)
    canvas.rect(MARGIN, top, WIDTH - MARGIN, top + strip, fill=color, stroke=None)
    canvas.text(MARGIN + 12, top + 18, name, size=12, color="#ffffff", bold=True)
    label = f"{len(items):02d} 条"
    canvas.text(WIDTH - MARGIN - 14 - _width(label, 10, MONO), top + 18, label, size=10, color="#ffffff", font=MONO)

    cursor = top + strip + 10
    for offset, (lines, meta) in enumerate(rows):
        number = f"{start_index + offset:02d}"
        canvas.text(MARGIN + 14, cursor + 12, number, size=13, color=color, font=MONO)
        for line_index, line in enumerate(lines):
            canvas.text(MARGIN + 52, cursor + 12 + line_index * 17, line, size=12.5, color=INK, bold=True)
        canvas.text(MARGIN + 52, cursor + 12 + len(lines) * 17 + 2, meta, size=9.5, color=TEXT_3, font=MONO)
        cursor += len(lines) * 17 + 16 + 12
        if offset < len(rows) - 1:
            canvas.rect(MARGIN + 14, cursor - 8, WIDTH - MARGIN - 14, cursor - 7.2, fill=HAIR, stroke=None)
    canvas.y = top + height + 14
    return start_index + len(items)


def _draw_footer(canvas: _Canvas) -> None:
    top = canvas.y
    canvas.text(MARGIN, top + 10, "完整解读与原文链接见网页简报", size=10, color=TEXT_3)
    canvas.y = top + 20


def render_daily_cover(brief: dict[str, Any]) -> bytes:
    """渲染每日简报摘要图，返回 PNG 字节。"""
    signals = [item for item in (brief.get("signals") or []) if isinstance(item, dict)]
    groups = group_signals(signals)
    canvas = _Canvas()
    _draw_header(canvas, brief, len(signals))
    _draw_intro(canvas, str(brief.get("intro") or ""))
    index = 1
    for position, (name, items) in enumerate(groups):
        index = _draw_group(canvas, name, items, CAT_COLORS[position % len(CAT_COLORS)], index)
    _draw_footer(canvas)

    height = canvas.y + MARGIN
    document = fitz.open()
    page = document.new_page(width=WIDTH, height=height)
    page.draw_rect(fitz.Rect(0, 0, WIDTH, height), fill=_rgb(PAPER), width=0)
    canvas.flush(page)
    data = page.get_pixmap(dpi=144).tobytes("png")
    document.close()
    return data
