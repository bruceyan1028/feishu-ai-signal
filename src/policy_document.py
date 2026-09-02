"""官方政策条目的 PDF 附件提取与正文证据增强。"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any

import fitz
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import paper_fulltext

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 24 * 1024 * 1024
MAX_DOCUMENT_CHARS = 8_000
MAX_VISUALS_PER_DOCUMENT = 4
EVIDENCE_VERSION = 2
USER_AGENT = "Mozilla/5.0 (compatible; AI-Signal/1.0; +policy-document)"
_PDF_CACHE: dict[str, bytes] = {}


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=6),
    reraise=True,
)
def _download_pdf(url: str) -> requests.Response:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"},
        timeout=45,
    )
    response.raise_for_status()
    return response


def fetch_pdf(url: str) -> bytes:
    if url in _PDF_CACHE:
        return _PDF_CACHE[url]
    response = _download_pdf(url)
    payload = response.content
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if len(payload) > MAX_PDF_BYTES:
        raise ValueError(f"PDF too large: {len(payload)} bytes")
    if not payload.startswith(b"%PDF") and "application/pdf" not in content_type:
        raise ValueError(f"not a PDF: {content_type or 'unknown content type'}")
    _PDF_CACHE[url] = payload
    return payload


def _clean_page_text(text: str) -> str:
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", str(text or ""))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_evidence(payload: bytes, max_chars: int = MAX_DOCUMENT_CHARS) -> dict[str, Any]:
    """从政策 PDF 直接提取正文、图表说明与视觉页，不依赖发布页摘要。"""
    document = fitz.open(stream=payload, filetype="pdf")
    try:
        page_texts = [_clean_page_text(page.get_text("text", sort=True)) for page in document]
    finally:
        page_count = document.page_count
        document.close()
    captions, visual_pages = paper_fulltext._extract_captions(page_texts)

    def score(page_number: int) -> tuple[int, int]:
        text = page_texts[page_number - 1]
        value = 0
        value += 5 * len(
            re.findall(
                r"\b(?:recommend|should|shall|required?|direct(?:s|ed)?|deadline|"
                r"budget|funding|implementation|action plan)\b",
                text,
                re.I,
            )
        )
        value += 3 * len(
            re.findall(
                r"\b(?:artificial intelligence|AI|Genesis Mission|R&D|research|"
                r"compute|dataset|infrastructure|autonomous laborator)\b",
                text,
                re.I,
            )
        )
        value += 10 if page_number in visual_pages else 0
        return value, -page_number

    # 开头用于交代文件性质，随后优先抽取政策条款与图表所在页。
    ordered_pages: list[int] = []
    for page_number in [1, 2, 3, *visual_pages, *range(4, page_count + 1)]:
        if (
            1 <= page_number <= page_count
            and page_number not in ordered_pages
            and page_texts[page_number - 1]
        ):
            ordered_pages.append(page_number)
    head = ordered_pages[:3]
    remainder = sorted(ordered_pages[3:], key=score, reverse=True)
    selected_pages: list[dict[str, Any]] = []
    used = 0
    for page_number in [*head, *remainder]:
        text = page_texts[page_number - 1]
        chunk = f"[PDF 第 {page_number} 页]\n{text}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        excerpt = chunk[:remaining]
        selected_pages.append({"page": page_number, "text": excerpt})
        used += len(excerpt)
    caption_text = "\n".join(
        f"[PDF 第 {item['page']} 页] {item['text']}" for item in captions
    )
    text = "\n\n".join(page["text"] for page in selected_pages)
    if caption_text:
        text = f"{text}\n\n【PDF 图表说明】\n{caption_text}"[: max_chars + 4000]
    return {
        "version": EVIDENCE_VERSION,
        "source": "pdf",
        "pages": page_count,
        "text": text.strip(),
        "extracted_chars": used,
        "selected_pages": [page["page"] for page in selected_pages],
        "captions": captions,
        "visual_pages": visual_pages,
    }


def render_visual_pages(
    pdf_url: str,
    page_numbers: list[Any],
    *,
    limit: int = MAX_VISUALS_PER_DOCUMENT,
) -> list[str]:
    """裁切政策 PDF 内单个 Figure/Table，供多模态模型读取。"""
    return [
        f"data:image/png;base64,{base64.b64encode(content).decode('ascii')}"
        for _, _, content in paper_fulltext._render_visual_crops(
            fetch_pdf(pdf_url),
            page_numbers,
            limit=limit,
        )
    ]


def write_visual_images(
    pdf_url: str,
    page_numbers: list[Any],
    target_dir: Path,
    file_prefix: str,
    *,
    limit: int = MAX_VISUALS_PER_DOCUMENT,
) -> list[dict[str, str]]:
    """把政策 PDF 图表裁切写入静态站。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_prefix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", file_prefix).strip("-") or "policy"
    images: list[dict[str, str]] = []
    for index, (page_number, caption, content) in enumerate(
        paper_fulltext._render_visual_crops(
            fetch_pdf(pdf_url),
            page_numbers,
            limit=limit,
        ),
        1,
    ):
        filename = f"{safe_prefix}-p{page_number}-f{index}.png"
        (target_dir / filename).write_bytes(content)
        images.append(
            {
                "filename": filename,
                "alt": f"政策 PDF 第 {page_number} 页图表：{caption[:600]}",
            }
        )
    return images


def is_policy_document_item(item: dict[str, Any]) -> bool:
    feed = item.get("feed") or {}
    extra = feed.get("extra_config") or {}
    return bool(extra.get("document_pdf_enrich"))


def enrich_item(item: dict[str, Any]) -> int:
    """读取条目 PDF 附件并将证据置于正文前部；返回成功读取的附件数。"""
    if not is_policy_document_item(item):
        return 0
    media = item.get("media_assets")
    if not isinstance(media, dict):
        return 0
    documents = [doc for doc in media.get("documents") or [] if isinstance(doc, dict)]
    if not documents:
        return 0
    extra = (item.get("feed") or {}).get("extra_config") or {}
    limit = max(1, min(4, int(extra.get("max_document_pdfs") or 2)))
    evidence_blocks: list[str] = []
    successful = 0
    enriched_documents: list[dict[str, Any]] = []
    for raw in documents[:limit]:
        doc = dict(raw)
        url = str(doc.get("url") or "").strip()
        if not url:
            continue
        try:
            evidence = extract_pdf_evidence(fetch_pdf(url))
            doc.pop("error", None)
            doc.update(
                {
                    "fullTextSource": "pdf",
                    "pages": int(evidence.get("pages") or 0),
                    "extractedChars": int(evidence.get("extracted_chars") or 0),
                    "evidenceVersion": int(evidence.get("version") or 0),
                    "selectedPages": list(evidence.get("selected_pages") or []),
                    "captions": list(evidence.get("captions") or [])[:24],
                    "visualPages": list(evidence.get("visual_pages") or [])[
                        :MAX_VISUALS_PER_DOCUMENT
                    ],
                }
            )
            text = str(evidence.get("text") or "").strip()
            if text:
                evidence_blocks.append(
                    f"【官方 PDF 附件：{doc.get('title') or '未命名文件'}】\n{text}"
                )
                successful += 1
        except (requests.RequestException, ValueError, RuntimeError, fitz.FileDataError) as exc:
            doc["fullTextSource"] = "unavailable"
            doc["error"] = str(exc)[:240]
            log.warning("政策 PDF 读取失败 %s: %s", url, exc)
        enriched_documents.append(doc)
    enriched_documents.extend(documents[limit:])
    media["documents"] = enriched_documents
    if evidence_blocks:
        original = str(item.get("raw_content") or "").strip()
        release_marker = "【White House 发布页正文】"
        if release_marker in original:
            original = original.rsplit(release_marker, 1)[-1].strip()
        item["raw_content"] = "\n\n".join(
            evidence_blocks + ([f"【White House 发布页正文】\n{original}"] if original else [])
        )
    return successful


def enrich_items(items: list[dict[str, Any]]) -> dict[str, int]:
    attempted = 0
    documents = 0
    for item in items:
        if not is_policy_document_item(item):
            continue
        attempted += 1
        documents += enrich_item(item)
    return {"items_attempted": attempted, "documents_read": documents}
