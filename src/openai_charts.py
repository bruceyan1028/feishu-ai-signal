"""抓取并渲染 OpenAI 文章内嵌的 Vega-Lite 图表。

OpenAI 的评测图不是 <img>，而是藏在 Next.js RSC 数据中的 Vega-Lite spec，
浏览器在客户端把它画成 SVG。普通 HTML 图片提取器因此永远看不到这些图。
"""
from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from curl_cffi import requests as browser_requests
import vl_convert as vlc

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r"<script>self\.__next_f\.push\((.*?)\)</script>",
    re.S,
)
_SPEC_MARKER = '"vegaLiteSpec":'
_PALETTE = {
    "theme1": "#2C67C5",
    "theme2": "#B8A037",
    "theme3": "#FFA4A2",
    "theme4": "#FF6764",
}
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def is_openai_article(url: str) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").lower().removeprefix("www.")
    return host == "openai.com" and "/index/" in urlsplit(str(url or "")).path


def fetch_article_html(url: str, timeout: int = 45) -> str:
    """用浏览器 TLS 指纹通过 OpenAI 的 Cloudflare 页面校验。"""
    if not is_openai_article(url):
        return ""
    try:
        response = browser_requests.get(url, impersonate="chrome", timeout=timeout)
        response.raise_for_status()
        return response.text
    except Exception as exc:  # noqa: BLE001 - 单篇失败不阻断整站发布
        log.warning("OpenAI 图表页面读取失败 %s: %s", url, exc)
        return ""


def extract_vega_specs(html: str) -> list[dict[str, Any]]:
    """从 Next.js Flight 数据中还原全部 Vega-Lite spec。"""
    decoder = json.JSONDecoder()
    specs: list[dict[str, Any]] = []
    for raw in _NEXT_DATA_RE.findall(str(html or "")):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not (
            isinstance(payload, list)
            and len(payload) > 1
            and isinstance(payload[1], str)
        ):
            continue
        text = payload[1]
        start = 0
        while True:
            marker = text.find(_SPEC_MARKER, start)
            if marker < 0:
                break
            try:
                spec, end = decoder.raw_decode(text, marker + len(_SPEC_MARKER))
            except ValueError:
                start = marker + len(_SPEC_MARKER)
                continue
            if isinstance(spec, dict) and spec.get("data"):
                specs.append(spec)
            start = end
    return specs


def _sanitize_spec(value: Any) -> None:
    """替换 OpenAI Vega 宿主注入的主题 token 和容器宽度信号。"""
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if (
                isinstance(child, dict)
                and str(child.get("expr") or "").startswith("dotcomContainerWidth")
            ):
                value[key] = 2 if key == "columns" else 0
            else:
                _sanitize_spec(child)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str) and child in _PALETTE:
                value[index] = _PALETTE[child]
            else:
                _sanitize_spec(child)


def render_spec_png(spec: dict[str, Any], *, scale: float = 2) -> bytes:
    clean = copy.deepcopy(spec)
    _sanitize_spec(clean)
    clean["width"] = 760
    clean["height"] = 430
    return vlc.vegalite_to_png(clean, scale=scale)


def write_article_charts(
    url: str,
    output_dir: Path | str,
    prefix: str,
    *,
    limit: int = 6,
) -> list[dict[str, str]]:
    """抓取文章并把图表写成 PNG，返回文件名和图注。"""
    html = fetch_article_html(url)
    if not html:
        return []
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    safe_prefix = _SAFE_NAME_RE.sub("-", str(prefix or "openai")).strip("-") or "openai"
    written: list[dict[str, str]] = []
    for index, spec in enumerate(extract_vega_specs(html)[:limit], 1):
        title = str(spec.get("title") or f"OpenAI 原文图表 {index}").strip()
        filename = f"{safe_prefix}-chart-{index}.png"
        try:
            (destination / filename).write_bytes(render_spec_png(spec))
        except Exception as exc:  # noqa: BLE001 - 单张图失败继续渲染其它图
            log.warning("OpenAI 图表渲染失败 %s #%d: %s", url, index, exc)
            continue
        written.append({"filename": filename, "alt": title})
    return written
