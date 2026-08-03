"""论文 PDF 全文与图表证据提取。

HF/PwC 只负责发现论文；分析依据必须回到论文 PDF。入库时保存经过限长的
分章节证据和图表页码，简报分析时再渲染少量图表页交给多模态模型。
"""
from __future__ import annotations

import base64
import logging
import re
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import fitz
import requests

from . import config, paper_enrich

log = logging.getLogger(__name__)

EVIDENCE_VERSION = 2
MAX_SECTION_CHARS = 4500
MAX_EVIDENCE_CHARS = 26000
MAX_VISUAL_PAGES = 4

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "AI-Signal/1.0 (paper-fulltext)"})
_PDF_CACHE: dict[str, bytes] = {}

_SECTION_ALIASES: list[tuple[str, re.Pattern[str]]] = [
    ("摘要", re.compile(r"^(?:abstract|摘要)$", re.I)),
    ("引言", re.compile(r"^(?:\d+(?:\.\d+)*[\s.]*)?(?:introduction|引言)$", re.I)),
    (
        "方法",
        re.compile(
            r"^(?:\d+(?:\.\d+)*[\s.]*)?(?:method(?:ology)?|approach|model|framework|"
            r"proposed method|方法|模型|框架)$",
            re.I,
        ),
    ),
    (
        "实验",
        re.compile(
            r"^(?:\d+(?:\.\d+)*[\s.]*)?(?:experiment(?:s|al setup)?|evaluation|"
            r"implementation details|实验|评测)$",
            re.I,
        ),
    ),
    (
        "结果",
        re.compile(
            r"^(?:\d+(?:\.\d+)*[\s.]*)?(?:result(?:s)?|analysis|discussion|"
            r"ablation(?: study)?|结果|分析|讨论|消融实验)$",
            re.I,
        ),
    ),
    (
        "局限",
        re.compile(r"^(?:\d+(?:\.\d+)*[\s.]*)?(?:limitations?|局限性?|局限)$", re.I),
    ),
    (
        "结论",
        re.compile(
            r"^(?:\d+(?:\.\d+)*[\s.]*)?(?:conclusion(?:s)?|concluding remarks|"
            r"结论|总结)$",
            re.I,
        ),
    ),
]
_CAPTION_RE = re.compile(
    r"^\s*(?:fig(?:ure)?|table|图|表)\s*[.\-:]?\s*(?:[A-Z]?\d+|[IVX]+)\b",
    re.I,
)
_PDF_META_RE = re.compile(
    r"<meta[^>]+name=[\"']citation_pdf_url[\"'][^>]+content=[\"']([^\"']+)",
    re.I,
)


def canonical_pdf_url(url: str, arxiv_id: str = "") -> str:
    """从论文落地页推导权威 PDF 地址；无法推导时返回空。"""
    pid = arxiv_id or paper_enrich.extract_arxiv_id(url)
    if pid:
        return f"https://arxiv.org/pdf/{pid}.pdf"
    clean = str(url or "").split("?", 1)[0]
    if clean.lower().endswith(".pdf"):
        return clean
    jmlr = re.search(r"(https?://(?:www\.)?jmlr\.org)/papers/v(\d+)/([^/]+)\.html?$", clean, re.I)
    if jmlr:
        host, volume, paper_id = jmlr.groups()
        return f"{host}/papers/volume{volume}/{paper_id}/{paper_id}.pdf"
    if not clean.startswith(("http://", "https://")):
        return ""
    try:
        response = _SESSION.get(clean, timeout=config.PAPER_ENRICH_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return ""
    match = _PDF_META_RE.search(response.text)
    return urljoin(response.url, unescape(match.group(1))) if match else ""


def fetch_pdf(pdf_url: str) -> bytes:
    if not pdf_url:
        return b""
    if pdf_url in _PDF_CACHE:
        return _PDF_CACHE[pdf_url]
    try:
        response = _SESSION.get(pdf_url, timeout=max(30, config.PAPER_ENRICH_TIMEOUT * 3))
        response.raise_for_status()
        content = response.content
        if not content.startswith(b"%PDF"):
            raise ValueError("response is not a PDF")
    except (requests.RequestException, ValueError) as exc:
        log.warning("论文 PDF 获取失败 %s: %s", pdf_url, exc)
        return b""
    _PDF_CACHE[pdf_url] = content
    return content


def _clean_page_text(text: str) -> str:
    text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _section_name(line: str) -> str:
    candidate = re.sub(r"\s+", " ", line).strip(" \t.:")
    if len(candidate) > 80:
        return ""
    for name, pattern in _SECTION_ALIASES:
        if pattern.fullmatch(candidate):
            return name
    return ""


def _extract_sections(page_texts: list[str]) -> list[dict[str, str]]:
    sections: dict[str, list[str]] = {}
    current = "正文"
    for page_text in page_texts:
        for line in page_text.splitlines():
            name = _section_name(line)
            if name:
                current = name
                sections.setdefault(current, [])
                continue
            if line.strip():
                sections.setdefault(current, []).append(line.strip())

    priority = ["摘要", "引言", "方法", "实验", "结果", "局限", "结论", "正文"]
    output: list[dict[str, str]] = []
    remaining = MAX_EVIDENCE_CHARS
    for name in priority:
        text = _clean_page_text("\n".join(sections.get(name) or []))
        if not text or remaining <= 0:
            continue
        excerpt = text[: min(MAX_SECTION_CHARS, remaining)].strip()
        if excerpt:
            output.append({"title": name, "text": excerpt})
            remaining -= len(excerpt)
    return output


def _extract_captions(page_texts: list[str]) -> tuple[list[dict[str, Any]], list[int]]:
    captions: list[dict[str, Any]] = []
    for page_index, page_text in enumerate(page_texts):
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not _CAPTION_RE.match(line):
                continue
            # 只有孤立的 “Figure 9.” 往往是正文交叉引用在 PDF 文本层里的断行，
            # 并不代表该页真的放着图；至少要在同一行出现说明文字。
            remainder = _CAPTION_RE.sub("", line, count=1).strip(" .|:—-")
            if len(remainder) < 8:
                continue
            caption = " ".join(lines[index : index + 3])[:1200]
            captions.append({"page": page_index + 1, "text": caption})
            if len(captions) >= 24:
                break
        if len(captions) >= 24:
            break

    def score(item: dict[str, Any]) -> tuple[int, int]:
        text = str(item.get("text") or "")
        value = 1
        if re.search(
            r"result|accuracy|benchmark|performance|ablation|comparison|"
            r"training curve|reward|evaluation|结果|准确率|基准|性能|消融",
            text,
            re.I,
        ):
            value += 4
        if re.match(r"\s*(?:table|表)\b", text, re.I):
            value += 2
        return value, -int(item.get("page") or 0)

    selected: list[int] = []
    for item in sorted(captions, key=score, reverse=True):
        page = int(item.get("page") or 0)
        if page > 0 and page not in selected:
            selected.append(page)
        if len(selected) >= MAX_VISUAL_PAGES:
            break
    return captions, sorted(selected)


def extract_pdf_evidence(pdf_bytes: bytes) -> dict[str, Any]:
    """从 PDF 提取结构化正文、图表说明和待视觉分析页码。"""
    if not pdf_bytes:
        return {}
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_texts = [_clean_page_text(page.get_text("text", sort=True)) for page in document]
    except (RuntimeError, ValueError) as exc:
        log.warning("论文 PDF 解析失败: %s", exc)
        return {}
    captions, visual_pages = _extract_captions(page_texts)
    sections = _extract_sections(page_texts)
    return {
        "version": EVIDENCE_VERSION,
        "source": "pdf",
        "pages": len(page_texts),
        "chars": sum(len(text) for text in page_texts),
        "sections": sections,
        "captions": captions,
        "visual_pages": visual_pages,
    }


def evidence_text(full_text: dict[str, Any]) -> str:
    parts = [
        f"【{section.get('title')}】\n{section.get('text')}"
        for section in full_text.get("sections") or []
        if section.get("text")
    ]
    captions = [
        f"[PDF 第{item.get('page')}页] {item.get('text')}"
        for item in full_text.get("captions") or []
        if item.get("text")
    ]
    if captions:
        parts.append("【图表说明】\n" + "\n".join(captions))
    return "\n\n".join(parts)[:MAX_EVIDENCE_CHARS]


def _page_caption_blocks(page: fitz.Page) -> list[tuple[fitz.Rect, str]]:
    captions: list[tuple[fitz.Rect, str]] = []
    for block in page.get_text("blocks", sort=True):
        text = " ".join(str(block[4]).split())
        match = _CAPTION_RE.match(text)
        if not match:
            continue
        remainder = text[match.end() :].strip(" .|:—-")
        if len(remainder) >= 8:
            captions.append((fitz.Rect(block[:4]), text))
    return captions


def _page_images(page: fitz.Page) -> list[fitz.Rect]:
    images: list[fitz.Rect] = []
    for image in page.get_image_info(xrefs=True):
        rect = fitz.Rect(image.get("bbox") or ())
        if (
            rect.width * rect.height >= 500
            and max(int(image.get("width") or 0), int(image.get("height") or 0)) >= 500
        ):
            images.append(rect)
    return images


def _page_graphics(page: fitz.Page) -> list[fitz.Rect]:
    graphics = _page_images(page)
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing.get("rect") or ())
        if (
            (rect.width >= 10 or rect.height >= 10)
            and rect.height <= page.rect.height * 0.3
            and rect.width * rect.height <= page.rect.width * page.rect.height * 0.2
        ):
            graphics.append(rect)
    try:
        graphics.extend(fitz.Rect(table.bbox) for table in page.find_tables().tables)
    except (AttributeError, RuntimeError, ValueError):
        pass
    return graphics


def _nearest_graphic_cluster(
    graphics: list[fitz.Rect],
    caption: fitz.Rect,
    *,
    below: bool,
    max_gap: float = 18,
    min_y: float = 0,
    max_y: float = float("inf"),
) -> list[fitz.Rect]:
    if below:
        candidates = [
            rect
            for rect in graphics
            if rect.y0 >= max(caption.y1 - 4, min_y)
            and rect.y1 <= max_y
            and rect.y0 - caption.y1 <= 420
        ]
        candidates.sort(key=lambda rect: (rect.y0, rect.y1))
        if not candidates:
            return []
        cluster = [candidates[0]]
        edge = candidates[0].y1
        for rect in candidates[1:]:
            if rect.y0 - edge > max_gap:
                break
            cluster.append(rect)
            edge = max(edge, rect.y1)
        return cluster

    candidates = [
        rect
        for rect in graphics
        if rect.y0 >= min_y
        and rect.y1 <= min(caption.y0 + 4, max_y)
        and caption.y0 - rect.y1 <= 420
    ]
    candidates.sort(key=lambda rect: (rect.y1, rect.y0), reverse=True)
    if not candidates:
        return []
    cluster = [candidates[0]]
    edge = candidates[0].y0
    for rect in candidates[1:]:
        if edge - rect.y1 > max_gap:
            break
        cluster.append(rect)
        edge = min(edge, rect.y0)
    return cluster


def _visual_regions(page: fitz.Page) -> list[tuple[fitz.Rect, str]]:
    """定位单个 Figure/Table，而不是把整页当成一张正文配图。"""
    regions: list[tuple[fitz.Rect, str]] = []
    captions = _page_caption_blocks(page)
    graphics = _page_graphics(page)
    for index, (caption_rect, caption) in enumerate(captions):
        is_table = bool(re.match(r"^\s*(?:table|表)\b", caption, re.I))
        previous = captions[index - 1] if index else None
        following = captions[index + 1] if index + 1 < len(captions) else None
        min_y = page.rect.y0
        max_y = page.rect.y1
        if not is_table and previous:
            previous_is_table = bool(
                re.match(r"^\s*(?:table|表)\b", previous[1], re.I)
            )
            min_y = (
                (previous[0].y1 + caption_rect.y0) / 2
                if previous_is_table
                else previous[0].y1
            )
        if is_table and following:
            max_y = (caption_rect.y1 + following[0].y0) / 2
        cluster = _nearest_graphic_cluster(
            graphics,
            caption_rect,
            below=is_table,
            max_gap=40 if not is_table else 18,
            min_y=min_y,
            max_y=max_y,
        )
        if cluster:
            visual = fitz.Rect(cluster[0])
            for rect in cluster[1:]:
                visual |= rect
            x0 = min(caption_rect.x0, visual.x0) - 8
            x1 = max(caption_rect.x1, visual.x1) + 8
            y0 = min(caption_rect.y0, visual.y0) - 8
            y1 = min(
                max_y,
                max(caption_rect.y1, visual.y1) + (24 if is_table else 8),
            )
        else:
            # 少数纯文本表格没有边框/绘图对象；按图注相邻区域做受限回退，
            # 仍只截半页以内，不再输出整页。
            x0, x1 = caption_rect.x0 - 8, caption_rect.x1 + 8
            if is_table:
                y0, y1 = caption_rect.y0 - 8, min(page.rect.y1, caption_rect.y1 + 260)
            else:
                y0, y1 = max(0, caption_rect.y0 - 260), caption_rect.y1 + 8
        clip = fitz.Rect(
            max(page.rect.x0, x0),
            max(page.rect.y0, y0),
            min(page.rect.x1, x1),
            min(page.rect.y1, y1),
        )
        if clip.width >= 80 and clip.height >= 45:
            regions.append((clip, caption))
    return regions


def _render_visual_crops(
    pdf_bytes: bytes, page_numbers: list[Any], *, limit: int
) -> list[tuple[int, str, bytes]]:
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except (RuntimeError, ValueError):
        return []
    crops: list[tuple[int, str, bytes]] = []
    seen: set[int] = set()
    for value in page_numbers:
        try:
            page_number = int(value)
            index = page_number - 1
        except (TypeError, ValueError):
            continue
        if index in seen or index < 0 or index >= len(document):
            continue
        seen.add(index)
        page = document[index]
        for clip, caption in _visual_regions(page):
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(1.8, 1.8),
                clip=clip,
                alpha=False,
            )
            crops.append((page_number, caption, pixmap.tobytes("png")))
            if len(crops) >= limit:
                return crops
    return crops


def render_visual_pages(
    pdf_url: str, page_numbers: list[Any], *, limit: int = MAX_VISUAL_PAGES
) -> list[str]:
    """把单个 Figure/Table 裁切为 data URL，供全模态模型直接阅读。"""
    pdf_bytes = fetch_pdf(pdf_url)
    if not pdf_bytes:
        return []
    return [
        f"data:image/png;base64,{base64.b64encode(content).decode('ascii')}"
        for _, _, content in _render_visual_crops(pdf_bytes, page_numbers, limit=limit)
    ]


def write_visual_page_images(
    pdf_url: str,
    page_numbers: list[Any],
    target_dir: Path,
    file_prefix: str,
    captions: list[dict[str, Any]] | None = None,
    *,
    limit: int = MAX_VISUAL_PAGES,
) -> list[dict[str, str]]:
    """把单个 PDF Figure/Table 写成静态站图片。"""
    pdf_bytes = fetch_pdf(pdf_url)
    if not pdf_bytes:
        return []
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_prefix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", file_prefix).strip("-") or "paper"
    images: list[dict[str, str]] = []
    for index, (page_number, caption, content) in enumerate(
        _render_visual_crops(pdf_bytes, page_numbers, limit=limit),
        1,
    ):
        filename = f"{safe_prefix}-p{page_number}-f{index}.png"
        (target_dir / filename).write_bytes(content)
        images.append(
            {
                "filename": filename,
                "alt": f"PDF 第 {page_number} 页图表：{caption[:600]}",
            }
        )
    return images


def enrich_item(item: dict[str, Any]) -> bool:
    """给最终入库论文补齐 PDF 证据；成功读取 PDF 才返回 True。"""
    metrics = dict(item.get("paper_metrics_json") or {})
    arxiv_id = str(metrics.get("arxiv_id") or paper_enrich.extract_arxiv_id(item.get("url") or ""))
    pdf_url = canonical_pdf_url(str(item.get("url") or ""), arxiv_id)
    full_text = extract_pdf_evidence(fetch_pdf(pdf_url))
    if not full_text:
        metrics["full_text"] = {
            "version": EVIDENCE_VERSION,
            "source": "abstract",
            "pdf_url": pdf_url,
            "chars": len(str(item.get("raw_content") or "")),
            "sections": [],
            "captions": [],
            "visual_pages": [],
        }
        item["paper_metrics_json"] = metrics
        return False
    full_text["pdf_url"] = pdf_url
    metrics["arxiv_id"] = arxiv_id or metrics.get("arxiv_id")
    metrics["full_text"] = full_text
    item["paper_metrics_json"] = metrics

    if arxiv_id:
        from . import rss

        figures = rss.fetch_arxiv_figures(f"https://arxiv.org/abs/{arxiv_id}", limit=4)
        if figures:
            media = dict(item.get("media_assets") or {})
            existing = list(media.get("images") or [])
            known = {str(image.get("url") or "") for image in existing}
            media["images"] = existing + [figure for figure in figures if figure["url"] not in known]
            item["media_assets"] = media
            if not item.get("image_url"):
                item["image_url"] = figures[0]["url"]
    return True
