"""YouTube 视频源采集：通过 Data API 获取频道视频、指标与热门评论。"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

log = logging.getLogger(__name__)

_API = "https://www.googleapis.com/youtube/v3"
_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def _api_get(resource: str, *, key: str, params: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(
        f"{_API}/{resource}",
        params={**params, "key": key},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def parse_duration(value: str) -> int:
    """把 YouTube ISO 8601 时长转成秒。"""
    match = _DURATION_RE.fullmatch(str(value or ""))
    if not match:
        return 0
    values = {name: int(raw or 0) for name, raw in match.groupdict().items()}
    return values["hours"] * 3600 + values["minutes"] * 60 + values["seconds"]


def _top_comments(video_id: str, *, key: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        payload = _api_get(
            "commentThreads",
            key=key,
            params={
                "part": "snippet",
                "videoId": video_id,
                "order": "relevance",
                "maxResults": min(100, max(limit * 3, limit)),
                "textFormat": "plainText",
            },
        )
    except requests.RequestException:
        # 关闭评论的视频返回 403；不应阻塞视频本身入库。
        return []
    comments = []
    for item in payload.get("items") or []:
        thread = item.get("snippet") or {}
        snippet = ((thread.get("topLevelComment") or {}).get("snippet") or {})
        comments.append(
            {
                "id": str(item.get("id") or ""),
                "author": str(snippet.get("authorDisplayName") or "YouTube 用户"),
                "authorAvatarUrl": str(snippet.get("authorProfileImageUrl") or ""),
                "text": str(snippet.get("textDisplay") or ""),
                "likes": int(snippet.get("likeCount") or 0),
                "replies": int(thread.get("totalReplyCount") or 0),
                "publishedAt": str(snippet.get("publishedAt") or ""),
            }
        )
    comments.sort(key=lambda item: item["likes"], reverse=True)
    return comments[:limit]


def _thumbnail(snippet: dict[str, Any]) -> str:
    thumbnails = snippet.get("thumbnails") or {}
    for name in ("maxres", "standard", "high", "medium", "default"):
        url = str((thumbnails.get(name) or {}).get("url") or "").strip()
        if url:
            return url
    return ""


def fetch_source(feed: dict[str, Any], *, key: str) -> list[dict[str, Any]]:
    extra = feed.get("extra_config") or {}
    channel_id = str(extra.get("channel_id") or "").strip()
    if not channel_id:
        raise ValueError(f"{feed.get('id')} 缺少 extra_config.channel_id")

    channel = _api_get(
        "channels",
        key=key,
        params={"part": "contentDetails", "id": channel_id, "maxResults": 1},
    )
    channel_items = channel.get("items") or []
    if not channel_items:
        raise RuntimeError(f"{feed.get('id')} 未找到 YouTube 频道")
    uploads = (
        ((channel_items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        or ""
    )
    max_items = max(1, min(50, int(extra.get("max_items") or 10)))
    playlist = _api_get(
        "playlistItems",
        key=key,
        params={
            "part": "contentDetails",
            "playlistId": uploads,
            "maxResults": max_items,
        },
    )
    video_ids = [
        str((item.get("contentDetails") or {}).get("videoId") or "")
        for item in playlist.get("items") or []
    ]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        return []

    details = _api_get(
        "videos",
        key=key,
        params={
            "part": "snippet,contentDetails,statistics,status",
            "id": ",".join(video_ids),
            "maxResults": len(video_ids),
        },
    )
    by_id = {str(item.get("id") or ""): item for item in details.get("items") or []}
    include_shorts = bool(extra.get("include_shorts"))
    include_live = bool(extra.get("include_live"))
    results = []
    for video_id in video_ids:
        detail = by_id.get(video_id) or {}
        snippet = detail.get("snippet") or {}
        if (detail.get("status") or {}).get("privacyStatus") != "public":
            continue
        duration_sec = parse_duration(str((detail.get("contentDetails") or {}).get("duration") or ""))
        if not include_shorts and 0 < duration_sec < 60:
            continue
        live_kind = str(snippet.get("liveBroadcastContent") or "none")
        if not include_live and live_kind != "none":
            continue
        statistics = detail.get("statistics") or {}
        views = int(statistics.get("viewCount") or 0)
        comment_count = int(statistics.get("commentCount") or 0)
        title = str(snippet.get("title") or "").strip()
        description = str(snippet.get("description") or "").strip()
        channel = str(snippet.get("channelTitle") or feed.get("name") or "").strip()
        thumbnail = _thumbnail(snippet)
        url = f"https://www.youtube.com/watch?v={video_id}"
        comments = _top_comments(video_id, key=key) if comment_count else []
        body = (
            f"{description}\n\n"
            f"视频元数据：频道 {channel}；时长 {duration_sec} 秒；"
            f"播放量 {views}；评论数 {comment_count}。"
        ).strip()
        results.append(
            {
                "title": title,
                "url": url,
                "body": body,
                "published_raw": snippet.get("publishedAt"),
                "image_url": thumbnail,
                "metrics": {
                    "channel": channel,
                    "duration_sec": duration_sec,
                    "views": views,
                    "comments": comment_count,
                },
                "media_assets": {
                    "images": [],
                    "videos": [
                        {
                            "id": video_id,
                            "platform": "youtube",
                            "url": url,
                            "embedUrl": f"https://www.youtube.com/embed/{video_id}?rel=0&playsinline=1",
                            "thumbnailUrl": thumbnail,
                            "durationSec": duration_sec,
                            "views": views,
                            "commentCount": comment_count,
                        }
                    ],
                    "topComments": comments,
                },
                "feed": feed,
            }
        )
    return results


def fetch_video_sources(feeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not feeds:
        return []
    if not key:
        raise RuntimeError("Media 视频源已启用，但缺少 YOUTUBE_API_KEY")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(4, len(feeds))) as executor:
        futures = {executor.submit(fetch_source, feed, key=key): feed for feed in feeds}
        for future in as_completed(futures):
            feed = futures[future]
            try:
                items = future.result()
                results.extend(items)
                log.info("视频源 %s 获取 %d 条", feed.get("id"), len(items))
            except Exception as exc:  # noqa: BLE001 - 单源失败不拖垮整轮
                log.warning("视频源 %s 获取失败：%s", feed.get("id"), exc)
    return results
