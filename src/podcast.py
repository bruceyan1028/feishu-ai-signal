"""播客 RSS、公开文字稿、托管 ASR 与分层中文摘要。"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import feedparser
import requests

from . import config, report

log = logging.getLogger(__name__)
_UA = "Mozilla/5.0 (compatible; AI-Signal/1.0)"
_TAG_RE = re.compile(r"<[^>]+>")
_TIMECODE_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})(?:[.,]\d{1,3})?"
)


def parse_duration(value: Any) -> int:
    """解析 RSS 常见的秒数、MM:SS 或 HH:MM:SS。"""
    raw = str(value or "").strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return 0
    values = [int(part) for part in parts]
    if len(values) == 2:
        return values[0] * 60 + values[1]
    return values[0] * 3600 + values[1] * 60 + values[2]


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _clean_text(value: Any) -> str:
    text = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return re.sub(r"[ \t]+", " ", re.sub(r"\r\n?", "\n", text)).strip()


def _entry_image(entry: dict[str, Any], parsed: Any) -> str:
    image = entry.get("itunes_image") or {}
    if isinstance(image, dict) and image.get("href"):
        return str(image["href"])
    for value in entry.get("media_thumbnail") or []:
        if isinstance(value, dict) and value.get("url"):
            return str(value["url"])
    feed_image = (getattr(parsed, "feed", {}) or {}).get("image") or {}
    return str(feed_image.get("href") or feed_image.get("url") or "")


def _audio_enclosure(entry: dict[str, Any]) -> tuple[str, str, int]:
    for enclosure in entry.get("enclosures") or entry.get("links") or []:
        if not isinstance(enclosure, dict):
            continue
        media_type = str(enclosure.get("type") or "")
        href = str(enclosure.get("href") or enclosure.get("url") or "")
        rel = str(enclosure.get("rel") or "")
        if href and (media_type.startswith("audio/") or rel == "enclosure"):
            try:
                length = int(enclosure.get("length") or 0)
            except (TypeError, ValueError):
                length = 0
            return href, media_type, length
    return "", "", 0


def _raw_xml_transcripts(content: bytes) -> dict[str, list[dict[str, str]]]:
    """按 guid/link/title 建立 Podcasting 2.0 transcript 索引。"""
    index: dict[str, list[dict[str, str]]] = {}
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return index
    for item in (element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "item"):
        keys: list[str] = []
        transcripts: list[dict[str, str]] = []
        for child in item:
            local = child.tag.rsplit("}", 1)[-1]
            if local in {"guid", "link", "title"} and child.text:
                keys.append(child.text.strip())
            if local == "transcript":
                url = str(child.attrib.get("url") or "").strip()
                if url:
                    transcripts.append(
                        {
                            "url": url,
                            "type": str(child.attrib.get("type") or "text/plain"),
                            "language": str(child.attrib.get("language") or ""),
                        }
                    )
        for key in keys:
            index[key] = transcripts
    return index


def _feedparser_transcripts(entry: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for key in ("podcast_transcript", "podcast_transcripts", "transcript"):
        raw = entry.get(key)
        values = raw if isinstance(raw, list) else [raw] if raw else []
        for value in values:
            if isinstance(value, dict):
                url = str(value.get("url") or value.get("href") or "").strip()
                if url:
                    result.append(
                        {
                            "url": url,
                            "type": str(value.get("type") or "text/plain"),
                            "language": str(value.get("language") or ""),
                        }
                    )
    return result


def fetch_source(feed: dict[str, Any]) -> list[dict[str, Any]]:
    urls = [str(feed["url"])] + [
        str(url)
        for url in (feed.get("extra_config") or {}).get("fallback_urls") or []
        if str(url).strip()
    ]
    response = None
    parsed = None
    errors = []
    for url in dict.fromkeys(urls):
        try:
            candidate = requests.get(url, headers={"User-Agent": _UA}, timeout=(15, 90))
            candidate.raise_for_status()
            candidate_parsed = feedparser.parse(candidate.content)
            if getattr(candidate_parsed, "bozo", False) and not candidate_parsed.entries:
                raise RuntimeError("RSS 无法解析或为空")
            response, parsed = candidate, candidate_parsed
            break
        except (requests.RequestException, RuntimeError) as exc:
            errors.append(f"{url}: {exc}")
    if response is None or parsed is None:
        raise RuntimeError("；".join(errors) or f"播客 RSS 获取失败：{feed['url']}")
    xml_transcripts = _raw_xml_transcripts(response.content)
    max_items = max(1, min(50, int((feed.get("extra_config") or {}).get("max_items") or 10)))
    results: list[dict[str, Any]] = []
    for entry in list(parsed.entries)[:max_items]:
        title = str(entry.get("title") or "").strip()
        page_url = str(entry.get("link") or "").strip()
        audio_url, audio_type, audio_bytes = _audio_enclosure(entry)
        guid = str(entry.get("id") or entry.get("guid") or page_url or audio_url).strip()
        if not title or not guid or not audio_url:
            continue
        duration = parse_duration(entry.get("itunes_duration") or entry.get("duration"))
        candidates = _feedparser_transcripts(entry)
        for key in (guid, page_url, title):
            candidates.extend(xml_transcripts.get(key) or [])
        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            url = urljoin(page_url or feed["url"], candidate["url"])
            if url and url not in seen:
                seen.add(url)
                unique.append({**candidate, "url": url})
        episode_feed = dict(feed)
        episode_feed["podcast_guid"] = guid
        episode_feed["dedup_key"] = "podcast_guid"
        image = _entry_image(entry, parsed)
        body = str(entry.get("summary") or entry.get("description") or title)
        results.append(
            {
                "title": title,
                "url": page_url or audio_url,
                "body": body,
                "published_raw": entry.get("published") or entry.get("updated"),
                "image_url": image,
                "is_html": True,
                "podcast": {
                    "guid": guid,
                    "audio_url": audio_url,
                    "audio_type": audio_type,
                    "audio_bytes": audio_bytes,
                    "duration_sec": duration,
                    "transcripts": unique,
                },
                "metrics": {"duration_sec": duration, "show": feed.get("name") or ""},
                "media_assets": {
                    "images": [{"url": image, "alt": title}] if image else [],
                    "videos": [],
                    "audio": {
                        "url": audio_url,
                        "type": audio_type,
                        "durationSec": duration,
                    },
                },
                "feed": episode_feed,
            }
        )
    return results


def fetch_podcast_sources(feeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for feed in feeds:
        try:
            items = fetch_source(feed)
            results.extend(items)
            log.info("播客源 %s 获取 %d 期", feed.get("id"), len(items))
        except Exception as exc:  # noqa: BLE001 - 单节目失败不影响其它白名单
            log.warning("播客源 %s 获取失败：%s", feed.get("id"), exc)
    return results


def _timestamp_seconds(raw: str) -> int:
    match = _TIMECODE_RE.search(raw)
    if not match:
        return 0
    return int(match["h"]) * 3600 + int(match["m"]) * 60 + int(match["s"])


def parse_transcript(content: str, media_type: str = "text/plain") -> str:
    """把 VTT/SRT/PodcastIndex JSON/HTML/plain 统一成带时间戳的文本。"""
    media_type = str(media_type or "").lower()
    if "json" in media_type:
        try:
            payload = json.loads(content)
            segments = payload.get("segments") if isinstance(payload, dict) else payload
            lines = []
            for segment in segments or []:
                if not isinstance(segment, dict):
                    continue
                start = segment.get("startTime") or segment.get("start_time") or segment.get("start") or 0
                try:
                    # PodcastIndex JSON 的 startTime 是毫秒；常见 ASR JSON 的 start 是秒。
                    seconds = float(start) / 1000 if "startTime" in segment else float(start)
                except (TypeError, ValueError):
                    seconds = 0
                text = _clean_text(segment.get("body") or segment.get("text"))
                if text:
                    lines.append(f"[{format_timestamp(seconds)}] {text}")
            if lines:
                return "\n".join(lines)
        except (TypeError, ValueError):
            pass
    if "html" in media_type:
        cleaned = _clean_text(content)
        return f"[00:00:00] {cleaned}" if cleaned else ""
    lines: list[str] = []
    current_time = 0
    buffer: list[str] = []
    for raw in content.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line in {"WEBVTT"} or line.isdigit():
            if buffer:
                text = _clean_text(" ".join(buffer))
                if text:
                    lines.append(f"[{format_timestamp(current_time)}] {text}")
                buffer = []
            continue
        if "-->" in line:
            if buffer:
                text = _clean_text(" ".join(buffer))
                if text:
                    lines.append(f"[{format_timestamp(current_time)}] {text}")
                buffer = []
            current_time = _timestamp_seconds(line.split("-->", 1)[0])
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        buffer.append(line)
    if buffer:
        text = _clean_text(" ".join(buffer))
        if text:
            lines.append(f"[{format_timestamp(current_time)}] {text}")
    if lines:
        return "\n".join(lines)
    plain = _clean_text(content)
    return f"[00:00:00] {plain}" if plain else ""


def fetch_public_transcript(candidates: list[dict[str, str]]) -> tuple[str, str]:
    preferred = sorted(
        candidates,
        key=lambda item: (
            not any(token in str(item.get("type") or "") for token in ("vtt", "subrip", "json")),
            str(item.get("language") or "") not in {"", "en", "zh", "zh-CN", "zh-Hans"},
        ),
    )
    for candidate in preferred:
        try:
            response = requests.get(candidate["url"], headers={"User-Agent": _UA}, timeout=45)
            response.raise_for_status()
            transcript = parse_transcript(response.text, candidate.get("type") or response.headers.get("content-type", ""))
            if len(transcript) >= 200:
                return transcript, "rss_transcript"
        except requests.RequestException as exc:
            log.info("公开 transcript 获取失败 %s：%s", candidate.get("url"), exc)
    return "", ""


def _youtube_captions(url: str) -> str:
    with tempfile.TemporaryDirectory(prefix="podcast-subs-") as raw_dir:
        output = str(Path(raw_dir) / "subtitle")
        command = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "en.*,zh.*,zh-Hans,zh-Hant",
            "--sub-format",
            "vtt",
            "--output",
            output,
            url,
        ]
        try:
            subprocess.run(command, check=True, timeout=180, capture_output=True)
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""
        files = sorted(Path(raw_dir).glob("subtitle*.vtt"))
        if not files:
            return ""
        return parse_transcript(files[0].read_text(encoding="utf-8", errors="replace"), "text/vtt")


def fetch_page_transcript(page_url: str) -> tuple[str, str]:
    """从节目页 transcript 链接、JSON-LD 或关联 YouTube 字幕读取公开文本。"""
    if not page_url.startswith(("http://", "https://")):
        return "", ""
    try:
        response = requests.get(page_url, headers={"User-Agent": _UA}, timeout=(15, 60))
        response.raise_for_status()
    except requests.RequestException:
        return "", ""
    page = response.text[:3_000_000]
    candidates: list[dict[str, str]] = []
    for href in re.findall(r"""(?is)<a\b[^>]+href=["']([^"']+)["'][^>]*>""", page):
        absolute = urljoin(response.url, html.unescape(href))
        path = urlsplit(absolute).path.lower()
        if path.endswith((".vtt", ".srt", ".json", ".txt")) and (
            "transcript" in absolute.lower() or path.endswith((".vtt", ".srt"))
        ):
            media_type = (
                "text/vtt"
                if path.endswith(".vtt")
                else "application/x-subrip"
                if path.endswith(".srt")
                else "application/json"
                if path.endswith(".json")
                else "text/plain"
            )
            candidates.append({"url": absolute, "type": media_type, "language": ""})
    transcript, _ = fetch_public_transcript(candidates)
    if transcript:
        return transcript, "page_transcript"
    for block in re.findall(
        r"""(?is)<script\b[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
        page,
    ):
        try:
            payload = json.loads(html.unescape(block))
        except (TypeError, ValueError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict):
                raw = _clean_text(value.get("transcript"))
                if len(raw) >= 500:
                    return f"[00:00:00] {raw}", "page_transcript"
    youtube = re.search(
        r"""https?://(?:www\.)?(?:youtube\.com/watch\?[^"' ]*v=|youtu\.be/)[\w-]{11}[^"' <]*""",
        page,
        re.IGNORECASE,
    )
    if youtube:
        transcript = _youtube_captions(html.unescape(youtube.group(0)))
        if len(transcript) >= 200:
            return transcript, "youtube_captions"
    return "", ""


def _download_audio(url: str, target: Path) -> None:
    max_bytes = config.PODCAST_MAX_AUDIO_MB * 1024 * 1024
    with requests.get(url, headers={"User-Agent": _UA}, stream=True, timeout=(30, 120)) as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length") or 0)
        if declared > max_bytes:
            raise RuntimeError(f"音频 {declared / 1024 / 1024:.1f}MB 超过上限")
        written = 0
        with target.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise RuntimeError("音频下载超过大小上限")
                handle.write(chunk)


def _segment_audio(source: Path, directory: Path) -> list[Path]:
    pattern = directory / "chunk-%04d.mp3"
    executable = shutil.which("ffmpeg")
    if not executable:
        try:
            import imageio_ffmpeg  # type: ignore[import-not-found]

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            executable = "ffmpeg"
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        "-f",
        "segment",
        "-segment_time",
        str(config.ASR_CHUNK_SECONDS),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    try:
        subprocess.run(command, check=True, timeout=1800)
    except FileNotFoundError as exc:
        raise RuntimeError("缺少 ffmpeg，无法切分播客音频") from exc
    chunks = sorted(directory.glob("chunk-*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg 未生成音频分片")
    return chunks


def _asr_endpoint() -> str:
    base = config.ASR_BASE_URL.rstrip("/")
    if base.endswith("/audio/transcriptions"):
        return base
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/audio/transcriptions"


def _transcribe_chunk(path: Path, offset_sec: int) -> str:
    if not config.ASR_API_KEY:
        raise RuntimeError("没有公开 transcript，且未配置 ASR_API_KEY")
    response = None
    for response_format in ("verbose_json", "json"):
        with path.open("rb") as audio:
            response = requests.post(
                _asr_endpoint(),
                headers={"Authorization": f"Bearer {config.ASR_API_KEY}"},
                data={"model": config.ASR_MODEL, "response_format": response_format},
                files={"file": (path.name, audio, "audio/mpeg")},
                timeout=900,
            )
        if response.status_code not in {400, 422} or response_format == "json":
            break
    assert response is not None
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        payload = {"text": response.text}
    lines = []
    for segment in payload.get("segments") or []:
        text = _clean_text(segment.get("text"))
        if text:
            lines.append(
                f"[{format_timestamp(offset_sec + float(segment.get('start') or 0))}] {text}"
            )
    text = _clean_text(payload.get("text"))
    return "\n".join(lines) or (f"[{format_timestamp(offset_sec)}] {text}" if text else "")


def transcribe_audio(audio_url: str) -> str:
    suffix = Path(urlsplit(audio_url).path).suffix or ".audio"
    with tempfile.TemporaryDirectory(prefix="podcast-") as raw_dir:
        directory = Path(raw_dir)
        source = directory / f"episode{suffix[:8]}"
        _download_audio(audio_url, source)
        chunks = _segment_audio(source, directory)
        transcripts = [
            _transcribe_chunk(chunk, index * config.ASR_CHUNK_SECONDS)
            for index, chunk in enumerate(chunks)
        ]
    return "\n".join(text for text in transcripts if text)


def _text_chunks(text: str, limit: int) -> list[str]:
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 1 > limit:
            chunks.append(current)
            current = ""
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(paragraph[i : i + limit] for i in range(0, len(paragraph), limit))
        else:
            current = f"{current}\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks


def summarize_transcript(title: str, show: str, transcript: str) -> tuple[str, dict[str, Any]]:
    if not config.LLM_API_KEY:
        raise RuntimeError("播客完整摘要需要 LLM_API_KEY")
    notes: list[str] = []
    chunks = _text_chunks(transcript, max(2000, config.PODCAST_TRANSCRIPT_CHUNK_CHARS))
    for index, chunk in enumerate(chunks, 1):
        raw = report._llm_json(
            f"""你在整理播客《{show}》的《{title}》，这是第 {index}/{len(chunks)} 段逐字稿。
仅依据本段输出 JSON：{{"notes_cn":"..."}}。
notes_cn 用中文按时间顺序列出有信息量的观点、事实、数字、承诺、争议和限制；每条必须保留原时间戳。
删去寒暄、广告和重复表达，不得补写逐字稿未提及的事实。

逐字稿：
{chunk}"""
        )
        note = str(raw.get("notes_cn") or "").strip()
        if note:
            notes.append(note[:5000])
    if not notes:
        raise RuntimeError("分段归纳没有产生有效笔记")
    combined = "\n\n".join(notes)[:50000]
    final = report._llm_json(
        f"""你是资深 AI 行业分析师。根据播客分段笔记输出严格 JSON，不得虚构：
- title_cn：准确简洁的中文标题
- summary_cn：300-600字完整中文摘要，覆盖全期主线、关键论据和结论
- deep_analysis_cn：1000-1800字，必须按以下顺序排版：
  1. 【嘉宾介绍】：先说明嘉宾姓名、当前身份/机构、专业背景，以及为何有资格讨论本期主题；只写逐字稿可证实的信息
  2. 【核心观点一：具体标题】起，按嘉宾在节目中的论述顺序展开 3-6 个核心观点；每个观点单独成节，写清主张、论据、例子、数字及限定条件
  3. 【分歧与边界】：明确主持人的追问、嘉宾保留意见、尚未解决的问题
  4. 【行业影响】：说明这些观点对 AI 技术、产品、人才或产业的具体影响
  不要再使用“核心议题”“关键观点与证据”“跟进行动”等泛化模板标题
- evidence_cn：2000-6000字时间戳证据稿，按节目顺序保留关键观点、数字、主体和限定条件
- why：中文1句说明为何重要
- impact/novelty/actionability：0-100整数
- urgency：高/中/低
- topics：从 AI、LLM、Agent、RAG、推理、多模态、开源、硬件、监管、融资、产品、其他中选2-4个

节目：{show}
标题：{title}
分段笔记：
{combined}"""
    )
    evidence = str(final.get("evidence_cn") or combined).strip()[:15000]
    analysis = {
        "title_cn": str(final.get("title_cn") or title).strip(),
        "summary_cn": str(final.get("summary_cn") or "").strip(),
        "deep_analysis_cn": str(final.get("deep_analysis_cn") or "").strip(),
        "why": str(final.get("why") or "").strip(),
        "impact": max(0, min(100, int(final.get("impact") or 0))),
        "novelty": max(0, min(100, int(final.get("novelty") or 0))),
        "actionability": max(0, min(100, int(final.get("actionability") or 0))),
        "urgency": str(final.get("urgency") or "中"),
        "topics": [str(value) for value in final.get("topics") or []][:4] or ["其他"],
    }
    if not analysis["summary_cn"]:
        raise RuntimeError("最终播客摘要为空")
    return evidence, analysis


def summarize_official_description(
    title: str, show: str, description: str
) -> dict[str, Any]:
    """在无法取得逐字稿时整理官方简介，并明确限制证据等级。"""
    raw = report._llm_json(
        f"""你是播客内容编辑。只依据节目标题和官方简介输出严格 JSON，不得补充外部知识：
{{
  "title_cn": "准确、克制的中文标题",
  "summary_cn": "120-240字的本期主题概览",
  "guest_intro_cn": "嘉宾姓名、简介明确披露的身份及其与主题的关系；未披露则返回空字符串",
  "core_points": [{{"title": "12字以内的具体小标题", "text": "1-3句，说明简介明确预告的议题、判断或案例"}}],
  "why": "一句话说明本期对AI行业观察的价值",
  "impact": 0到100整数,
  "novelty": 0到100整数,
  "actionability": 0到100整数,
  "urgency": "高/中/低",
  "topics": ["从AI、LLM、Agent、RAG、推理、多模态、开源、硬件、端侧、监管、融资、产品、其他中选2-4项"]
}}

整理规则：
1. core_points 给出2-4项，按简介叙述顺序排列，标题必须具体；
2. 删除关注公众号、视频号、平台分发、时间戳、emoji和营销口号；
3. 不把“将讨论、尝试回答”改写成嘉宾已经证明的结论；
4. 不生成逐字稿中才可能确认的分歧、论证过程或最终结论；
5. 评分只衡量简介明确披露的内容，证据不足时从严；
6. 中文自然简洁，不整段照抄官方简介。

节目：{show}
标题：{title}
官方简介：
{description[:5000]}"""
    )
    points = [
        {
            "title": str(point.get("title") or "简介要点").strip(),
            "text": str(point.get("text") or "").strip(),
        }
        for point in (raw.get("core_points") or [])
        if isinstance(point, dict) and str(point.get("text") or "").strip()
    ][:4]
    guest = str(raw.get("guest_intro_cn") or "").strip()
    sections = []
    if guest:
        sections.append("【嘉宾与背景】\n" + guest)
    sections.extend(
        f"【简介要点{index}：{point['title']}】\n{point['text']}"
        for index, point in enumerate(points, 1)
    )
    boundary = (
        "【信息边界】\n本卡片只依据节目官方简介提炼，尚未取得完整逐字稿；"
        "因此不对简介之外的论证过程、分歧和结论作推断。"
    )
    if not guest:
        boundary += " 官方简介未披露可核验的嘉宾身份。"
    sections.append(boundary)
    urgency = str(raw.get("urgency") or "中")
    topics = [
        str(topic)
        for topic in (raw.get("topics") or [])
        if str(topic)
        in {
            "AI",
            "LLM",
            "Agent",
            "RAG",
            "推理",
            "多模态",
            "开源",
            "硬件",
            "端侧",
            "监管",
            "融资",
            "产品",
            "其他",
        }
    ][:4]
    return {
        "title_cn": str(raw.get("title_cn") or title).strip(),
        "summary_cn": str(raw.get("summary_cn") or "").strip(),
        "deep_analysis_cn": "\n\n".join(sections),
        "why": str(raw.get("why") or "").strip(),
        "impact": max(0, min(100, int(raw.get("impact") or 0))),
        "novelty": max(0, min(100, int(raw.get("novelty") or 0))),
        "actionability": max(0, min(100, int(raw.get("actionability") or 0))),
        "urgency": urgency if urgency in {"高", "中", "低"} else "中",
        "topics": topics or ["AI", "其他"],
        "guest_intro_cn": guest,
        "core_points": points,
    }


def enrich_podcast_item(item: dict[str, Any]) -> str:
    metadata = item.get("podcast") or {}
    duration = int(metadata.get("duration_sec") or 0)
    max_duration = int(
        ((item.get("feed") or {}).get("extra_config") or {}).get("max_duration_sec")
        or config.PODCAST_MAX_DURATION_SECONDS
    )
    if duration and duration > max_duration:
        raise RuntimeError(f"节目时长 {duration} 秒超过上限 {max_duration}")
    transcript, transcript_source = fetch_public_transcript(metadata.get("transcripts") or [])
    if not transcript:
        transcript, transcript_source = fetch_page_transcript(str(item.get("url") or ""))
    if not transcript:
        if config.ASR_API_KEY:
            transcript = transcribe_audio(str(metadata.get("audio_url") or ""))
            transcript_source = "hosted_asr"
        else:
            description = _clean_text(
                item.get("raw_content") or item.get("body") or ""
            )
            if len(description) < 80:
                raise RuntimeError("没有公开 transcript，且官方简介过短")
            analysis = summarize_official_description(
                str(item.get("title") or ""),
                str((item.get("feed") or {}).get("name") or ""),
                description,
            )
            item["raw_content"] = description
            item["podcast_analysis"] = analysis
            metrics = dict(item.get("metrics") or {})
            metrics.update(
                {
                    "transcript_source": "official_description",
                    "transcript_chars": 0,
                    "duration_sec": duration,
                }
            )
            item["metrics"] = metrics
            item["podcast_metrics_json"] = metrics
            item["quality_score"] = round(
                (
                    float(analysis["impact"])
                    + float(analysis["novelty"])
                    + float(analysis["actionability"])
                )
                / 3,
                1,
            )
            return "official_description"
    if len(transcript) < 200:
        raise RuntimeError("逐字稿过短")
    evidence, analysis = summarize_transcript(
        str(item.get("title") or ""),
        str((item.get("feed") or {}).get("name") or ""),
        transcript,
    )
    item["raw_content"] = evidence
    item["podcast_analysis"] = analysis
    metrics = dict(item.get("metrics") or {})
    metrics.update(
        {
            "transcript_source": transcript_source,
            "transcript_chars": len(transcript),
            "duration_sec": duration,
        }
    )
    item["metrics"] = metrics
    item["podcast_metrics_json"] = metrics
    item["quality_score"] = round(
        (
            float(analysis["impact"])
            + float(analysis["novelty"])
            + float(analysis["actionability"])
        )
        / 3,
        1,
    )
    return transcript_source


def enrich_podcast_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for item in items:
        if (item.get("feed") or {}).get("fetch_method") != "Podcast":
            kept.append(item)
            continue
        try:
            source = enrich_podcast_item(item)
            stats[source] += 1
            stats["kept"] += 1
            kept.append(item)
        except Exception as exc:  # noqa: BLE001 - 单期失败不影响其它节目
            stats["failed"] += 1
            log.warning("播客处理失败 %s：%s", item.get("url"), exc)
    return kept, dict(stats)
