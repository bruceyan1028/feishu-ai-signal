"""本地视频源诊断：抓取 YouTube 频道最新视频并生成前端预览站。

用法：
  python -m src.diag_video
  python -m src.diag_video --items-per-source 2 --serve
  python -m src.diag_video --source-id youtube-openai --source-id youtube-anthropic

脚本只读飞书配置，不写入飞书，也不下载视频或执行 ASR。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from . import config, feishu, sources, typed_config

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover - 由 CLI 给出可操作提示
    YoutubeDL = None  # type: ignore[assignment,misc]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("diag_video")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "video-preview"
PREVIEW_SEED = Path(__file__).with_name("video_preview_seed.json")
YOUTUBE_ID_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|embed/))([\w-]{11})")

_CONFIG_ENV = {
    "FEISHU_APP_ID": "FEISHU_APP_ID",
    "FEISHU_APP_SECRET": "FEISHU_APP_SECRET",
    "FEISHU_BASE_ID": "FEISHU_BASE_ID",
    "FEISHU_PARAM_TABLE_ID": "FEISHU_PARAM_TABLE_ID",
    "FEISHU_ENTRY_TABLE_ID": "FEISHU_ENTRY_TABLE_ID",
    "FEISHU_BRIEF_TABLE_ID": "FEISHU_BRIEF_TABLE_ID",
    "FEISHU_PAPER_CONFIG_TABLE_ID": "FEISHU_PAPER_CONFIG_TABLE_ID",
    "FEISHU_WECHAT_CONFIG_TABLE_ID": "FEISHU_WECHAT_CONFIG_TABLE_ID",
    "FEISHU_VIDEO_CONFIG_TABLE_ID": "FEISHU_VIDEO_CONFIG_TABLE_ID",
    "FEISHU_SOCIAL_CONFIG_TABLE_ID": "FEISHU_SOCIAL_CONFIG_TABLE_ID",
    "FEISHU_GITHUB_CONFIG_TABLE_ID": "FEISHU_GITHUB_CONFIG_TABLE_ID",
}

_TABLE_ATTRS = {
    "一级参数": "FEISHU_PARAM_TABLE_ID",
    "条目表": "FEISHU_ENTRY_TABLE_ID",
    "简报表": "FEISHU_BRIEF_TABLE_ID",
    "二级参数-论文": "FEISHU_PAPER_CONFIG_TABLE_ID",
    "二级参数-公众号": "FEISHU_WECHAT_CONFIG_TABLE_ID",
    "二级参数-视频": "FEISHU_VIDEO_CONFIG_TABLE_ID",
    "二级参数-社媒": "FEISHU_SOCIAL_CONFIG_TABLE_ID",
    "二级参数-GitHub": "FEISHU_GITHUB_CONFIG_TABLE_ID",
}


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    for attr, env_name in _CONFIG_ENV.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            setattr(config, attr, value)


def _resolve_table_ids(token: str) -> None:
    """按表名刷新 table_id，避免 .env 沿用旧 Base 的表 ID。"""
    tables = {str(item.get("name") or ""): str(item.get("table_id") or "") for item in feishu.list_tables(token)}
    for table_name, attr in _TABLE_ATTRS.items():
        if tables.get(table_name):
            setattr(config, attr, tables[table_name])


def _parse_extra(fields: dict[str, Any]) -> dict[str, Any]:
    raw = sources.cell(fields.get("extra_config"))
    try:
        return json.loads(str(raw)) if raw else {}
    except (TypeError, ValueError):
        return {}


def load_video_sources(
    records: list[dict[str, Any]],
    type_configs: dict[str, dict[str, Any]],
    *,
    source_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    wanted = {value.strip() for value in source_ids or [] if value.strip()}
    result: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        source_id = str(sources.cell(fields.get("source_id")) or "").strip()
        status = str(sources.cell(fields.get("status")) or "active").strip()
        method = str(sources.cell(fields.get("fetch_method")) or "").strip()
        if method != "Media" or status not in {"active", "experimental"}:
            continue
        if wanted and source_id not in wanted:
            continue
        extra = _parse_extra(fields)
        typed = type_configs.get(source_id) or {}
        if typed.get("entity_type") not in {None, "video"}:
            continue
        endpoint = sources.normalize_endpoint(sources.cell(fields.get("endpoint")))
        if not endpoint:
            continue
        result.append(
            {
                "id": source_id,
                "name": str(sources.cell(fields.get("name")) or source_id),
                "url": endpoint,
                "category": sources.normalize_category(
                    fields.get("dimension"), default="其他"
                ),
                "tier": str(sources.cell(fields.get("tier")) or "L2"),
                "priority": str(sources.cell(fields.get("priority")) or "P1"),
                "lookback": str(sources.cell(fields.get("lookback_window")) or ""),
                "status": status,
                "extra_config": extra,
                "video_config": typed.get("params") or {},
            }
        )
    result.sort(key=lambda item: (item["priority"], item["id"]))
    return result[:limit] if limit and limit > 0 else result


def load_video_type_configs(token: str) -> dict[str, dict[str, Any]]:
    """只读视频二级参数，避免其它类型配置表故障影响本诊断。"""
    result: dict[str, dict[str, Any]] = {}
    for fields in feishu.read_all_records(token, config.FEISHU_VIDEO_CONFIG_TABLE_ID):
        source_id = str(feishu._read_cell_key(fields.get("source_id")) or "").strip()
        if not source_id:
            continue
        result[source_id] = {
            "entity_type": "video",
            "params": typed_config._parse_row("video", fields),
        }
    return result


def _channel_videos_url(source: dict[str, Any]) -> str:
    channel_id = str((source.get("extra_config") or {}).get("channel_id") or "").strip()
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}/videos"
    url = str(source.get("url") or "").rstrip("/")
    return url if url.endswith("/videos") else f"{url}/videos"


def fetch_channel(source: dict[str, Any], items_per_source: int) -> tuple[list[dict[str, Any]], str]:
    if YoutubeDL is None:
        raise RuntimeError("缺少 yt-dlp，请先运行 pip install -r requirements.txt")
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": True,
        "playlistend": max(1, items_per_source),
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
    }
    error = ""
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(_channel_videos_url(source), download=False) or {}
    except Exception as exc:  # noqa: BLE001 - 诊断必须让其它频道继续
        info = {}
        error = str(exc)
    entries = [entry for entry in info.get("entries") or [] if entry and entry.get("id")]
    if entries:
        return entries[:items_per_source], ""
    fallback = _preview_seed_entries(source["id"], items_per_source)
    if fallback:
        reason = error or "YouTube 实时列表未返回条目"
        return fallback, f"{reason}；已使用最近核验样本"
    return [], error


def _preview_seed_entries(source_id: str, limit: int) -> list[dict[str, Any]]:
    if not PREVIEW_SEED.is_file():
        return []
    try:
        data = json.loads(PREVIEW_SEED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = (data.get("sources") or {}).get(source_id) or []
    return [{**entry, "_preview_fallback": True} for entry in entries[:limit]]


def _video_id(entry: dict[str, Any]) -> str:
    direct = str(entry.get("id") or "").strip()
    if re.fullmatch(r"[\w-]{11}", direct):
        return direct
    match = YOUTUBE_ID_RE.search(str(entry.get("url") or entry.get("webpage_url") or ""))
    return match.group(1) if match else direct


def _thumbnail(entry: dict[str, Any], video_id: str) -> str:
    direct = str(entry.get("thumbnail") or "").strip()
    if direct:
        return direct
    thumbnails = [item for item in entry.get("thumbnails") or [] if item.get("url")]
    if thumbnails:
        return str(thumbnails[-1]["url"])
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def _published_date(entry: dict[str, Any], fallback: str) -> str:
    raw = str(entry.get("upload_date") or entry.get("release_date") or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    timestamp = entry.get("timestamp") or entry.get("release_timestamp")
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return fallback


def _duration_label(value: Any) -> str:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return "列表接口未返回"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def fetch_top_comments(
    video_id: str,
    limit: int,
    *,
    cookies_from_browser: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """优先用 YouTube Data API；未配置 API key 时回落到 yt-dlp。"""
    if not video_id or limit <= 0:
        return [], ""
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if api_key:
        try:
            response = requests.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={
                    "part": "snippet",
                    "videoId": video_id,
                    "order": "relevance",
                    "maxResults": min(100, max(limit * 3, limit)),
                    "textFormat": "plainText",
                    "key": api_key,
                },
                timeout=20,
            )
            response.raise_for_status()
            comments = []
            for item in response.json().get("items") or []:
                snippet = ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
                comments.append(
                    {
                        "id": str(item.get("id") or ""),
                        "author": str(snippet.get("authorDisplayName") or "YouTube 用户"),
                        "authorAvatarUrl": str(snippet.get("authorProfileImageUrl") or ""),
                        "text": str(snippet.get("textDisplay") or ""),
                        "likes": int(snippet.get("likeCount") or 0),
                        "replies": int((item.get("snippet") or {}).get("totalReplyCount") or 0),
                        "publishedAt": str(snippet.get("publishedAt") or ""),
                    }
                )
            comments.sort(key=lambda item: item["likes"], reverse=True)
            return comments[:limit], ""
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return [], f"YouTube Data API 评论获取失败：HTTP {status}" if status else "YouTube Data API 评论获取失败：网络请求异常"
        except (ValueError, TypeError):
            return [], "YouTube Data API 评论获取失败：返回数据格式异常"

    if YoutubeDL is None:
        return [], "缺少 yt-dlp"
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "getcomments": True,
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
        "extractor_args": {"youtube": {"max_comments": [str(max(limit * 3, limit))]}},
    }
    if cookies_from_browser:
        options["cookiesfrombrowser"] = (cookies_from_browser,)
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False) or {}
    except Exception as exc:  # noqa: BLE001 - 评论失败不能阻塞视频预览
        return [], f"yt-dlp 评论获取失败：{exc}"
    comments = []
    for item in info.get("comments") or []:
        if item.get("parent") not in (None, "root"):
            continue
        comments.append(
            {
                "id": str(item.get("id") or ""),
                "author": str(item.get("author") or "YouTube 用户"),
                "authorAvatarUrl": str(item.get("author_thumbnail") or ""),
                "text": str(item.get("text") or ""),
                "likes": int(item.get("like_count") or 0),
                "replies": int(item.get("replies") or 0),
                "publishedAt": str(item.get("timestamp") or ""),
            }
        )
    comments.sort(key=lambda item: item["likes"], reverse=True)
    return comments[:limit], ""


def build_signal(
    source: dict[str, Any],
    entry: dict[str, Any],
    *,
    rank: int,
    preview_date: str,
) -> dict[str, Any]:
    video_id = _video_id(entry)
    url = f"https://www.youtube.com/watch?v={video_id}" if video_id else str(entry.get("url") or "")
    title = str(entry.get("title") or "未命名视频").strip()
    title_cn = str(entry.get("title_cn") or title).strip()
    thumbnail = _thumbnail(entry, video_id)
    views = entry.get("view_count")
    views_text = f"{int(views):,}" if isinstance(views, (int, float)) else "列表接口未返回"
    duration_text = _duration_label(entry.get("duration"))
    description = re.sub(r"\s+", " ", str(entry.get("description") or "")).strip()
    summary = description[:280] if description else (
        f"从 {source['name']} 获取到的最新视频。当前快速预览已取得标题、缩略图和可嵌入播放地址；"
        "字幕与 ASR 将在正式 Media 采集链路中补齐。"
    )
    return {
        "recordId": f"video-preview-{rank}",
        "title": title,
        "titleCn": title_cn,
        "source": source["name"],
        "url": url,
        "category": source["category"],
        "contentType": "视频",
        "tier": source["tier"],
        "priority": source["priority"],
        "publishedDate": _published_date(entry, preview_date),
        "summary": summary,
        "deepAnalysis": (
            f"【视频元数据】\n频道：{source['name']}；时长：{duration_text}；播放量：{views_text}；"
            f"视频 ID：{video_id or '未知'}。\n\n"
            "【采集状态】\n本地快速预览使用频道列表元数据，不下载视频，也不执行字幕提取或语音转写。"
            "因此这里主要用于确认视频卡片、缩略图、来源类型标签和嵌入播放器的前端效果。"
        ),
        "why": "验证该视频源的频道发现、元数据获取和前端视频展示链路。",
        "impact": 85 if source["priority"] == "P0" else 70,
        "novelty": 60,
        "actionability": 65,
        "urgency": "中",
        "tags": ["视频", "YouTube", source["priority"]],
        "imageUrl": thumbnail,
        "mediaAssets": {
            "images": [],
            "videos": [
                {
                    "url": url,
                    "embedUrl": f"https://www.youtube.com/embed/{video_id}?rel=0&playsinline=1",
                    "provider": "youtube",
                    "videoId": video_id,
                    "title": title,
                }
            ]
            if video_id
            else [],
        },
        "topComments": entry.get("top_comments") or [],
        "commentFetchError": str(entry.get("_comment_error") or ""),
    }


def build_preview(
    sources_to_fetch: list[dict[str, Any]],
    *,
    items_per_source: int,
    offline: bool = False,
    comments_limit: int = 5,
    cookies_from_browser: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    today = datetime.now().date().isoformat()
    signals: list[dict[str, Any]] = []
    source_reports: list[dict[str, Any]] = []
    fetched: dict[str, tuple[list[dict[str, Any]], str]] = {}
    if offline:
        for source in sources_to_fetch:
            entries = _preview_seed_entries(source["id"], items_per_source)
            for entry in entries:
                entry["_comment_error"] = "--offline 模式未请求 YouTube 评论"
            fetched[source["id"]] = (entries, "已按 --offline 使用最近核验样本")
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(sources_to_fetch))) as executor:
            futures = {
                executor.submit(fetch_channel, source, items_per_source): source
                for source in sources_to_fetch
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    fetched[source["id"]] = future.result()
                except Exception as exc:  # noqa: BLE001 - 单源失败不阻塞预览
                    fetched[source["id"]] = ([], str(exc))

    # 元数据可使用离线核验样本；配置 API Key 后仍实时获取公开评论。
    if comments_limit > 0 and (not offline or bool(os.environ.get("YOUTUBE_API_KEY", "").strip())):
        comment_jobs: dict[Any, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            for entries, _ in fetched.values():
                for entry in entries:
                    video_id = _video_id(entry)
                    if video_id:
                        future = executor.submit(
                            fetch_top_comments,
                            video_id,
                            comments_limit,
                            cookies_from_browser=cookies_from_browser,
                        )
                        comment_jobs[future] = entry
            for future in as_completed(comment_jobs):
                entry = comment_jobs[future]
                try:
                    comments, comment_error = future.result()
                except Exception as exc:  # noqa: BLE001
                    comments, comment_error = [], str(exc)
                entry["top_comments"] = comments
                entry["_comment_error"] = comment_error

    for source in sources_to_fetch:
        entries, error = fetched.get(source["id"], ([], "未返回抓取结果"))
        cached = bool(entries and entries[0].get("_preview_fallback"))
        source_reports.append(
            {
                "source_id": source["id"],
                "name": source["name"],
                "status": "cached" if cached else ("ok" if entries else "failed"),
                "items": len(entries),
                "comments": sum(len(entry.get("top_comments") or []) for entry in entries),
                "error": error,
            }
        )
        log.info("%s: 获取 %d 条%s", source["id"], len(entries), f"（{error}）" if error else "")
        for entry in entries:
            signals.append(build_signal(source, entry, rank=len(signals) + 1, preview_date=today))

    live = sum(1 for item in source_reports if item["status"] == "ok")
    cached = sum(1 for item in source_reports if item["status"] == "cached")
    failed = len(source_reports) - live - cached
    refs = list(range(1, min(len(signals), 10) + 1))
    brief = {
        "date": today,
        "title": f"YouTube 视频源本地预览 · {today}",
        "intro": (
            f"本次读取 {len(sources_to_fetch)} 个 experimental/active Media 源，"
            f"{live} 个实时成功、{cached} 个使用最近核验样本、{failed} 个失败，"
            f"共展示 {len(signals)} 条视频。"
        ),
        "bullets": [
            {
                "title": f"成功发现 {len(signals)} 条视频",
                "text": "以下均为当前频道列表的真实标题、缩略图和 YouTube 嵌入地址。",
                "refs": refs,
            },
            {
                "title": "本轮仅验证发现与展示",
                "text": "未下载视频、未执行字幕提取或 ASR，正式采集链路仍保持 experimental。",
                "refs": refs[: min(4, len(refs))],
            },
        ]
        if signals
        else [],
        "signals": signals,
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": source_reports,
        "signals": len(signals),
    }
    return brief, report


def write_preview(brief: dict[str, Any], report: dict[str, Any], out_dir: Path) -> Path:
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "index.html", out_dir / "index.html")
    content = json.dumps(brief, ensure_ascii=False, indent=2)
    (data_dir / "brief-latest.json").write_text(content, encoding="utf-8")
    (data_dir / f"brief-{brief['date']}.json").write_text(content, encoding="utf-8")
    (out_dir / "video-fetch-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_dir / "index.html"


def run() -> int:
    parser = argparse.ArgumentParser(description="抓取 YouTube Media 源并生成本地前端预览")
    parser.add_argument("--source-id", action="append", help="只测试指定 source_id，可重复传入")
    parser.add_argument("--limit-sources", type=int, help="最多测试多少个频道")
    parser.add_argument("--items-per-source", type=int, default=1, help="每频道取最近多少条，默认 1")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT), help="预览输出目录")
    parser.add_argument("--serve", action="store_true", help="生成后启动本地 HTTP 服务")
    parser.add_argument("--offline", action="store_true", help="跳过 YouTube 实时请求，使用最近核验样本")
    parser.add_argument("--comments", type=int, default=5, help="每条视频最多抓取多少条热门评论，默认 5")
    parser.add_argument(
        "--cookies-from-browser",
        help="可选：让 yt-dlp 读取指定浏览器 Cookie，例如 chrome 或 safari",
    )
    parser.add_argument("--port", type=int, default=4174, help="HTTP 服务端口，默认 4174")
    args = parser.parse_args()

    _load_dotenv()
    if YoutubeDL is None:
        parser.error("缺少 yt-dlp，请先运行 pip install -r requirements.txt")
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        parser.error("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，请检查 .env")

    token = feishu.get_tenant_access_token()
    _resolve_table_ids(token)
    records = feishu.read_param_records(token)
    type_configs = load_video_type_configs(token)
    video_sources = load_video_sources(
        records,
        type_configs,
        source_ids=args.source_id,
        limit=args.limit_sources,
    )
    if not video_sources:
        parser.error("没有找到可测试的 Media 视频源")

    brief, report = build_preview(
        video_sources,
        items_per_source=max(1, args.items_per_source),
        offline=args.offline,
        comments_limit=max(0, args.comments),
        cookies_from_browser=args.cookies_from_browser,
    )
    preview = write_preview(brief, report, Path(args.out_dir))
    print(f"预览已生成：{preview}")
    print(f"抓取报告：{preview.parent / 'video-fetch-report.json'}")
    if args.serve:
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        os.chdir(preview.parent)
        url = f"http://127.0.0.1:{args.port}"
        print(f"本地预览：{url}")
        ThreadingHTTPServer(("127.0.0.1", args.port), SimpleHTTPRequestHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
