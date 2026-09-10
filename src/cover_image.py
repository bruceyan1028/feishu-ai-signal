"""Generate a restrained editorial cover when an article has no usable source image."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)

_INELIGIBLE_REASONS = {"论文", "视频", "播客", "社交媒体"}
_DATA_IMAGE_RE = re.compile(
    r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)"
)


def _eligible(signal: dict[str, Any]) -> bool:
    return (
        config.IMAGE_GENERATION_ENABLED
        and bool(config.IMAGE_GENERATION_API_KEY)
        and not str(signal.get("imageUrl") or "").strip()
        and str(signal.get("contentType") or "") not in _INELIGIBLE_REASONS
    )


def _decode_data_image(value: str) -> bytes | None:
    match = _DATA_IMAGE_RE.search(value)
    if not match:
        return None
    try:
        return base64.b64decode(match.group(2), validate=True)
    except (ValueError, TypeError):
        return None


def _image_bytes(payload: dict[str, Any]) -> bytes | None:
    item = (payload.get("data") or [{}])[0]
    encoded = str(item.get("b64_json") or "")
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return None
    message = ((payload.get("choices") or [{}])[0].get("message") or {})
    content = message.get("content")
    if isinstance(content, str):
        decoded = _decode_data_image(content)
        if decoded:
            return decoded
    for image in message.get("images") or []:
        image_url = image.get("image_url") if isinstance(image, dict) else None
        value = image_url.get("url") if isinstance(image_url, dict) else image_url
        decoded = _decode_data_image(str(value or ""))
        if decoded:
            return decoded
    return None


def _generate_request(prompt: str) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {config.IMAGE_GENERATION_API_KEY}",
        "Content-Type": "application/json",
    }
    endpoint = config.IMAGE_GENERATION_ENDPOINT
    use_chat = endpoint == "chat" or (
        endpoint == "auto" and "gemini" in config.IMAGE_GENERATION_MODEL.lower()
    )
    if use_chat:
        return requests.post(
            f"{config.IMAGE_GENERATION_BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": config.IMAGE_GENERATION_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": ["text", "image"],
            },
            timeout=180,
        )
    return requests.post(
        f"{config.IMAGE_GENERATION_BASE_URL}/images/generations",
        headers=headers,
        json={
            "model": config.IMAGE_GENERATION_MODEL,
            "prompt": prompt,
            "size": "1536x1024",
        },
        timeout=180,
    )


def _validate_generated_cover(
    signal: dict[str, Any], content: bytes
) -> tuple[bool, str]:
    if not config.VISION_API_KEY:
        return True, ""
    from . import report

    title = str(signal.get("titleCn") or signal.get("title") or "").strip()
    image_url = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
    prompt = f"""审查这张 AI 新闻卡片封面，只输出 JSON：
{{"relevance":0到1,"quality":0到1,"text_heavy":true或false,"prominent_logo":true或false,"generic_abstract":true或false,"reason":"简短说明"}}
新闻标题：{title}
合格要求：主题相关；画面具体呈现新闻涉及的设备、工作场景或产品使用语境；构图和画质适合专业科技媒体。以标题、代码、社交帖子、网页界面或大段文字为主体时 text_heavy=true；背景中少量不可辨识的屏幕纹理、设备铭牌或自然环境字符不算。清晰品牌标志占据视觉焦点时 prominent_logo=true；微小且不显眼的设备标记不算。只有发光大脑、云朵、线路、抽象网络等通用 AI 意象应判为 generic_abstract。
"""
    try:
        result = report._llm_json(
            prompt,
            api_key=config.VISION_API_KEY,
            base_url=config.VISION_BASE_URL,
            model=config.VISION_MODEL,
            image_urls=[image_url],
            prefer_responses=True,
        )
        relevance = float(result.get("relevance", 0))
        quality = float(result.get("quality", 0))
        accepted = (
            relevance >= config.IMAGE_GENERATION_MIN_CONFIDENCE
            and quality >= config.IMAGE_GENERATION_MIN_CONFIDENCE
            and not bool(result.get("text_heavy"))
            and not bool(result.get("prominent_logo"))
            and not bool(result.get("generic_abstract"))
        )
        return accepted, str(result.get("reason") or "")
    except Exception as exc:  # noqa: BLE001 - 质检不可用时不阻断缺图兜底
        log.info("生成封面视觉质检不可用 %s: %s", title, exc)
        return True, ""


def generate_cover(signal: dict[str, Any], target_dir: Path) -> dict[str, str] | None:
    """Generate and cache a clearly synthetic, non-documentary news illustration."""
    if not _eligible(signal):
        return None
    title = str(signal.get("titleCn") or signal.get("title") or "").strip()
    summary = str(signal.get("summary") or "").strip()[:500]
    if not title:
        return None
    digest = hashlib.sha256(
        f"v3\n{config.IMAGE_GENERATION_MODEL}\n{title}\n{summary}".encode("utf-8")
    ).hexdigest()[:18]
    filename = f"generated-{digest}.png"
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / filename
    if destination.exists() and destination.stat().st_size > 0:
        return {"url": f"media/generated/{filename}", "kind": "generated-cover"}

    base_prompt = f"""Create a completely text-free landscape editorial image for an AI industry news card.
Topic: {title}
Context: {summary}
Use a concrete, story-specific real-world scene: recognizable hardware, workplace, laboratory,
data center, product-in-use environment, or human activity directly implied by this news. Favor
natural editorial photography or a sophisticated cinematic 3D scene with realistic materials,
lighting and depth. Use a clean composition with one strong focal subject, suitable for a
professional technology publication, 3:2 landscape.
Do not fabricate a documentary photograph of a named real event or identifiable real person. Do not include company
logos, trademarks, screens with writing, charts with labels, headlines, captions, letters, numbers,
watermarks, or borders. The final image must contain zero readable typography of any kind.
Avoid generic glowing brains, floating clouds, neon circuit webs, abstract AI networks, isometric
infographics, dashboard collages, and vague futuristic symbolism. The image must visually explain
this specific story even without a caption.
"""
    rejection = ""
    for attempt in range(max(1, config.IMAGE_GENERATION_MAX_ATTEMPTS)):
        prompt = base_prompt
        if rejection:
            prompt += f"\nA previous attempt was rejected because: {rejection}\nCorrect that issue."
        try:
            response = _generate_request(prompt)
            response.raise_for_status()
            payload = response.json()
            content = _image_bytes(payload)
            item = (payload.get("data") or [{}])[0]
            if not content and item.get("url"):
                image_response = requests.get(str(item["url"]), timeout=60)
                image_response.raise_for_status()
                content = image_response.content
            if not content:
                rejection = "the response contained no image"
                continue
            accepted, rejection = _validate_generated_cover(signal, content)
            if accepted:
                destination.write_bytes(content)
                break
            log.info("生成封面第 %s 次质检未通过 %s: %s", attempt + 1, title, rejection)
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.warning("生成封面失败 %s: %s", signal.get("url") or title, exc)
            return None
    if not destination.exists():
        log.warning("生成封面多次质检未通过 %s: %s", title, rejection)
        return None
    return {"url": f"media/generated/{filename}", "kind": "generated-cover"}


def fill_missing_covers(briefs: list[dict[str, Any]], target_dir: Path) -> None:
    """Fill eligible empty covers without replacing any source or PDF image."""
    for brief in briefs:
        for signal in brief.get("signals") or []:
            generated = generate_cover(signal, target_dir)
            if not generated:
                continue
            media = dict(signal.get("mediaAssets") or {})
            media["cover"] = generated["url"]
            media["coverKind"] = generated["kind"]
            media["curatedBy"] = "generated"
            signal["mediaAssets"] = media
            signal["imageUrl"] = generated["url"]
