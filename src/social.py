"""X 白名单账号采集、线程合并与低成本筛选。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Callable

import requests

from . import config, report

log = logging.getLogger(__name__)
_API = "https://api.x.com/2"
_SESSION = requests.Session()
# 事件词：必须是「发生了一件事」，单独提 model/api/paper 不够。
_EVENT_RE = re.compile(
    r"\b(announce|announcing|introduc(?:e|ing)|launch(?:ed|ing)?|release[sd]?|"
    r"now available|open[- ]source|weights?|pric(?:e|ing)|vulnerabilit|"
    r"license|benchmark|acqui(?:re[sd]?|sition)|funding|raised)\b|"
    r"(发布|上线|推出|开源|权重|定价|降价|漏洞|许可|融资|收购)",
    re.I,
)
_TECH_RE = re.compile(
    r"\b(ai|llm|agent|reasoning|inference|training|multimodal|robot|gpu|"
    r"transformer|rag|benchmark|dataset|eval|safety)\b|"
    r"(人工智能|大模型|智能体|推理|训练|多模态|机器人|算力|评测|数据集|安全)",
    re.I,
)
_NOISE_RE = re.compile(
    r"\b(giveaway|hiring|we'?re hiring|join us|happy birthday|good morning|"
    r"register now|last chance|see you there)\b|"
    r"(抽奖|招聘|加入我们|生日快乐|早上好|立即报名|最后机会|不见不散)",
    re.I,
)
_QUOTE_BOILERPLATE_RE = re.compile(
    r"^(this\.?|this is (?:huge|big|wild|insane)|lol|lmao|agree[d]?|exactly|"
    r"so true|wow|yes\.?|这个|同意|哈哈+|太强了|牛+|赞+)$",
    re.I,
)
_URL_RE = re.compile(r"https?://\S+", re.I)
_MENTION_RE = re.compile(r"@\w+")
_HASHTAG_RE = re.compile(r"#\S+")
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:\.\d+)?%?|[$￥¥]\s*\d+", re.I)
_CODE_RE = re.compile(r"(github\.com|huggingface\.co|arxiv\.org|doi\.org)", re.I)
_ARTICLE_URL_RE = re.compile(
    r"(?:x|twitter)\.com/(?:i/article/|[^/]+/article/)|/i/premium/articles?/",
    re.I,
)
_TWEET_FIELDS = (
    "id,text,author_id,created_at,conversation_id,referenced_tweets,"
    "public_metrics,entities,attachments,edit_history_tweet_ids,lang,note_tweet"
)


@dataclass
class SocialFetchBatch:
    items: list[dict[str, Any]] = field(default_factory=list)
    cursor_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    read_counts: dict[str, int] = field(default_factory=dict)
    successful_accounts: set[str] = field(default_factory=set)


def _api_get(path: str, *, bearer: str, params: dict[str, Any]) -> dict[str, Any]:
    response = _SESSION.get(
        f"{_API}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {bearer}"},
        params=params,
        timeout=30,
    )
    if response.status_code == 429:
        reset = response.headers.get("x-rate-limit-reset") or "unknown"
        raise RuntimeError(f"X API rate limited（reset={reset}）")
    if response.status_code == 402:
        raise RuntimeError(
            "X API 返回 402 Payment Required：请在 Developer Console 购买 credits，"
            "并确认当前 App 有可用余额"
        )
    if response.status_code == 401:
        raise RuntimeError("X API 返回 401 Unauthorized：X_BEARER_TOKEN 无效或已被重新生成")
    if response.status_code == 403:
        raise RuntimeError("X API 返回 403 Forbidden：当前 App 无权访问该端点")
    if response.status_code >= 400:
        detail = (response.text or "")[:300]
        raise RuntimeError(f"X API {response.status_code}: {detail or response.reason}")
    payload = response.json()
    errors = payload.get("errors") or []
    if errors and not payload.get("data"):
        raise RuntimeError(f"X API error: {errors}")
    return payload


def _account_names(feed: dict[str, Any]) -> list[str]:
    accounts = feed.get("accounts") or []
    result = []
    for account in accounts:
        username = str(account.get("username") if isinstance(account, dict) else account).strip()
        username = username.lstrip("@").lower()
        if username and username not in result:
            result.append(username)
    return result


def _resolve_accounts(
    usernames: list[str],
    state: dict[str, Any],
    *,
    bearer: str,
) -> dict[str, dict[str, Any]]:
    """仅解析状态中尚无 user_id 的账号，并把资料写回待持久化状态。"""
    resolved: dict[str, dict[str, Any]] = {}
    missing = []
    for username in usernames:
        saved = state.get(username) if isinstance(state.get(username), dict) else {}
        if saved.get("user_id"):
            resolved[username] = dict(saved)
        else:
            missing.append(username)
    for start in range(0, len(missing), 100):
        batch = missing[start : start + 100]
        if not batch:
            continue
        payload = _api_get(
            "users/by",
            bearer=bearer,
            params={"usernames": ",".join(batch), "user.fields": "name,username,public_metrics"},
        )
        for user in payload.get("data") or []:
            username = str(user.get("username") or "").lower()
            metrics = user.get("public_metrics") or {}
            saved = dict(state.get(username) or {})
            saved.update(
                {
                    "user_id": str(user.get("id") or ""),
                    "name": str(user.get("name") or username),
                    "followers": int(metrics.get("followers_count") or 0),
                }
            )
            state[username] = saved
            resolved[username] = saved
        unresolved = sorted(set(batch) - set(resolved))
        if unresolved:
            log.warning("X 账号未解析：%s", ", ".join(f"@{x}" for x in unresolved))
    return resolved


def _timeline(
    user_id: str,
    *,
    bearer: str,
    since_id: str = "",
    lookback_hours: int = 168,
    max_pages: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    posts: list[dict[str, Any]] = []
    media: dict[str, dict[str, Any]] = {}
    referenced: dict[str, dict[str, Any]] = {}
    token = ""
    start_time = (
        datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    tweet_fields = _TWEET_FIELDS
    use_start_time = not since_id
    for _ in range(max_pages):
        params: dict[str, Any] = {
            "max_results": 100,
            "tweet.fields": tweet_fields,
            "expansions": "attachments.media_keys,referenced_tweets.id",
            "media.fields": "media_key,type,url,preview_image_url,width,height,duration_ms,public_metrics",
            "exclude": "retweets",
        }
        if since_id:
            params["since_id"] = since_id
        elif use_start_time:
            params["start_time"] = start_time
        if token:
            params["pagination_token"] = token
        try:
            payload = _api_get(f"users/{user_id}/tweets", bearer=bearer, params=params)
        except RuntimeError as exc:
            if token:
                raise
            if "note_tweet" in tweet_fields:
                tweet_fields = tweet_fields.replace(",note_tweet", "")
                log.warning("X timeline 不支持 note_tweet，已降级：%s", exc)
                continue
            if use_start_time:
                use_start_time = False
                log.warning("X timeline start_time 被拒，改走本地时间窗：%s", exc)
                continue
            raise
        posts.extend(payload.get("data") or [])
        includes = payload.get("includes") or {}
        for obj in includes.get("media") or []:
            key = str(obj.get("media_key") or "")
            if key:
                media[key] = obj
        for obj in includes.get("tweets") or []:
            key = str(obj.get("id") or "")
            if key:
                referenced[key] = obj
        meta = payload.get("meta") or {}
        token = str(meta.get("next_token") or "")
        if not token:
            break
    if lookback_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))
        filtered = []
        for post in posts:
            try:
                created = datetime.fromisoformat(
                    str(post.get("created_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                filtered.append(post)
                continue
            if created >= cutoff:
                filtered.append(post)
        posts = filtered
    return posts, media, referenced


def _reference_types(post: dict[str, Any]) -> set[str]:
    return {
        str(ref.get("type") or "")
        for ref in post.get("referenced_tweets") or []
        if ref.get("type")
    }


def _post_full_text(post: dict[str, Any]) -> str:
    note = post.get("note_tweet") if isinstance(post.get("note_tweet"), dict) else {}
    return str((note or {}).get("text") or post.get("text") or "").strip()


def _clean_own_text(text: str) -> str:
    cleaned = _URL_RE.sub(" ", text)
    cleaned = _MENTION_RE.sub(" ", cleaned)
    cleaned = _HASHTAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"^RT\s+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _article_cards(posts: list[dict[str, Any]]) -> list[str]:
    cards: list[str] = []
    seen: set[str] = set()
    for post in posts:
        for url in (post.get("entities") or {}).get("urls") or []:
            expanded = str(
                url.get("expanded_url") or url.get("unwound_url") or url.get("url") or ""
            )
            title = str(url.get("title") or "").strip()
            desc = str(url.get("description") or "").strip()
            if not (_ARTICLE_URL_RE.search(expanded) or (title and desc)):
                continue
            key = expanded or title
            if not key or key in seen:
                continue
            seen.add(key)
            chunk = " ".join(part for part in (title, desc, expanded) if part)
            if chunk:
                cards.append(chunk)
    return cards


def _referenced_texts(
    posts: list[dict[str, Any]], referenced_by_id: dict[str, dict[str, Any]]
) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for post in posts:
        for ref in post.get("referenced_tweets") or []:
            ref_id = str(ref.get("id") or "")
            if not ref_id or ref_id in seen:
                continue
            seen.add(ref_id)
            text = _post_full_text(referenced_by_id.get(ref_id) or {})
            if text:
                texts.append(text)
    return texts


def _media_assets(
    posts: list[dict[str, Any]], media_by_key: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    images: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for post in posts:
        for key in (post.get("attachments") or {}).get("media_keys") or []:
            key = str(key)
            if not key or key in seen:
                continue
            seen.add(key)
            obj = media_by_key.get(key) or {}
            kind = str(obj.get("type") or "")
            url = str(obj.get("url") or obj.get("preview_image_url") or "")
            asset = {
                "id": key,
                "platform": "x",
                "type": kind,
                "url": url,
                "width": int(obj.get("width") or 0),
                "height": int(obj.get("height") or 0),
            }
            if kind == "photo":
                images.append(asset)
            else:
                asset["thumbnailUrl"] = str(obj.get("preview_image_url") or "")
                asset["durationSec"] = round(float(obj.get("duration_ms") or 0) / 1000, 1)
                videos.append(asset)
    image_url = str((images[0] if images else {}).get("url") or "")
    return image_url, {"images": images, "videos": videos}


def _build_account_items(
    posts: list[dict[str, Any]],
    media_by_key: dict[str, dict[str, Any]],
    *,
    feed: dict[str, Any],
    username: str,
    profile: dict[str, Any],
    referenced_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    referenced_by_id = referenced_by_id or {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for post in posts:
        conversation = str(post.get("conversation_id") or post.get("id") or "")
        if conversation:
            groups[conversation].append(post)
    out = []
    for conversation, thread_posts in groups.items():
        thread_posts.sort(key=lambda post: int(str(post.get("id") or "0")))
        root = next(
            (post for post in thread_posts if str(post.get("id")) == conversation),
            thread_posts[0],
        )
        refs = set().union(*(_reference_types(post) for post in thread_posts))
        texts = [_post_full_text(post) for post in thread_posts]
        texts = [text for text in texts if text]
        cards = _article_cards(thread_posts)
        image_url, assets = _media_assets(thread_posts, media_by_key)
        has_media = bool(assets.get("images") or assets.get("videos"))
        if not texts and not cards and not has_media:
            continue
        body_parts = list(texts)
        if cards:
            body_parts.extend(cards)
        body = "\n\n---\n\n".join(body_parts)
        first = texts[0] if texts else (cards[0] if cards else "")
        compact = re.sub(r"\s+", " ", first).strip()
        title = compact[:117] + ("..." if len(compact) > 117 else "")
        if not title and has_media:
            title = f"X media post {root.get('id') or ''}".strip()
        post_id = str(root.get("id") or "")
        edit_history = [str(value) for value in root.get("edit_history_tweet_ids") or [] if value]
        canonical_id = edit_history[0] if edit_history else post_id
        url = f"https://x.com/{username}/status/{post_id}"
        public = [post.get("public_metrics") or {} for post in thread_posts]
        likes = sum(int(item.get("like_count") or 0) for item in public)
        replies = sum(int(item.get("reply_count") or 0) for item in public)
        reposts = sum(int(item.get("retweet_count") or 0) for item in public)
        quotes = sum(int(item.get("quote_count") or 0) for item in public)
        bookmarks = sum(int(item.get("bookmark_count") or 0) for item in public)
        engagement = likes + 2 * replies + 2 * reposts + 2 * quotes + bookmarks
        is_retweet = "retweeted" in refs
        is_quote = "quoted" in refs
        # 纯转发没有本人增量；引用转述只计自己写的字。
        own_source = "" if is_retweet and not is_quote else "\n".join(texts)
        own_text = _clean_own_text(own_source)
        quoted_texts = _referenced_texts(thread_posts, referenced_by_id) if is_quote else []
        quoted_text = _clean_own_text("\n".join(quoted_texts))
        item_feed = dict(feed)
        item_feed["name"] = f"X · @{username}"
        item_feed["x_post_id"] = canonical_id
        out.append(
            {
                "title": title,
                "url": url,
                "body": body,
                "published_raw": root.get("created_at"),
                "image_url": image_url,
                "media_assets": assets,
                "metrics": {
                    "platform": "x",
                    "post_id": post_id,
                    "canonical_post_id": canonical_id,
                    "conversation_id": conversation,
                    "account": username,
                    "account_name": profile.get("name") or username,
                    "followers": int(profile.get("followers") or 0),
                    "likes": likes,
                    "replies": replies,
                    "reposts": reposts,
                    "quotes": quotes,
                    "bookmarks": bookmarks,
                    "engagement": engagement,
                    "is_retweet": is_retweet,
                    "is_reply": "replied_to" in refs,
                    "is_quote": is_quote,
                    "thread_count": len(thread_posts),
                    "edit_history_tweet_ids": edit_history,
                    "own_text": own_text,
                    "own_chars": len(own_text),
                    "quoted_text": quoted_text,
                    "quoted_chars": len(quoted_text),
                    "has_article": bool(cards) or any(len(_post_full_text(post)) > 280 for post in thread_posts),
                    "has_media": has_media,
                },
                "feed": item_feed,
            }
        )
    return out


def _fetch_account(
    feed: dict[str, Any],
    username: str,
    profile: dict[str, Any],
    *,
    bearer: str,
) -> tuple[list[dict[str, Any]], str, int]:
    posts, media, referenced = _timeline(
        str(profile["user_id"]),
        bearer=bearer,
        since_id=str(profile.get("since_id") or ""),
        lookback_hours=int(feed.get("lookback_hours") or 168),
        max_pages=int((feed.get("extra_config") or {}).get("max_pages") or 20),
    )
    max_seen = max((str(post.get("id") or "") for post in posts), key=lambda x: int(x or 0), default="")
    return _build_account_items(
        posts,
        media,
        feed=feed,
        username=username,
        profile=profile,
        referenced_by_id=referenced,
    ), max_seen, len(posts)


def fetch_social_sources(
    feeds: list[dict[str, Any]], *, bearer: str | None = None
) -> SocialFetchBatch:
    """增量拉取所有 Social 源；返回的 cursor 由调用方在成功写入后持久化。"""
    result = SocialFetchBatch()
    if not feeds:
        return result
    bearer = (bearer or os.environ.get("X_BEARER_TOKEN") or "").strip()
    if not bearer:
        raise RuntimeError("Social 源已启用，但缺少 X_BEARER_TOKEN")
    jobs: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for feed in feeds:
        source_id = str(feed.get("id") or "")
        state = json.loads(json.dumps(feed.get("cursor_state") or {}))
        resolved = _resolve_accounts(_account_names(feed), state, bearer=bearer)
        result.cursor_states[source_id] = state
        tiers = feed.get("account_tiers") or {}
        poll_hours = (feed.get("social_params") or {}).get("poll_hours") or {"P0": 24, "P1": 24}
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        for username, profile in resolved.items():
            tier = str(tiers.get(username) or feed.get("priority") or "P1").upper()
            interval_ms = int(poll_hours.get(tier, 4)) * 3600000
            if now_ms - int(profile.get("last_fetched_ms") or 0) < interval_ms:
                continue
            jobs.append((feed, username, profile))
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(jobs)))) as executor:
        futures = {
            executor.submit(_fetch_account, feed, username, profile, bearer=bearer): (
                feed,
                username,
                profile,
            )
            for feed, username, profile in jobs
        }
        for future in as_completed(futures):
            feed, username, profile = futures[future]
            source_id = str(feed.get("id") or "")
            key = f"{source_id}:{username}"
            try:
                items, max_seen, count = future.result()
                result.items.extend(items)
                result.read_counts[key] = count
                result.successful_accounts.add(key)
                if max_seen:
                    state = result.cursor_states[source_id][username]
                    state["since_id"] = max_seen
                result.cursor_states[source_id][username]["last_fetched_ms"] = int(
                    datetime.now(timezone.utc).timestamp() * 1000
                )
                log.info("X @%s 读取 %d 条，合并为 %d 条", username, count, len(items))
            except Exception as exc:  # noqa: BLE001 - 单账号失败不拖垮整轮
                log.warning("X @%s 获取失败：%s", username, exc)
    return result


def _novelty_score(text: str, recent_texts: list[str]) -> int:
    sample = re.sub(r"\s+", " ", text.lower())[:800]
    if not sample or not recent_texts:
        return 20
    similarity = max(
        SequenceMatcher(None, sample, re.sub(r"\s+", " ", other.lower())[:800]).ratio()
        for other in recent_texts
        if other
    )
    if similarity < 0.35:
        return 20
    if similarity < 0.65:
        return 12
    if similarity < 0.85:
        return 4
    return 0


def _item_text(item: dict[str, Any]) -> str:
    return f"{item.get('title') or ''}\n{item.get('body') or ''}"


def _has_hard_evidence(item: dict[str, Any]) -> bool:
    """可核验证据：代码/论文链、数字、长线程、X 文章。有图/任意 URL 不算。"""
    metrics = item.get("metrics") or {}
    text = _item_text(item)
    if _CODE_RE.search(text):
        return True
    if _NUMBER_RE.search(text):
        return True
    if int(metrics.get("thread_count") or 0) >= 3:
        return True
    return bool(metrics.get("has_article"))


def _has_media(item: dict[str, Any]) -> bool:
    metrics = item.get("metrics") or {}
    assets = item.get("media_assets") or {}
    return bool(
        metrics.get("has_media")
        or item.get("image_url")
        or assets.get("images")
        or assets.get("videos")
    )


def _annotate_gates(item: dict[str, Any]) -> None:
    metrics = item.setdefault("metrics", {})
    text = _item_text(item)
    metrics["has_event"] = bool(_EVENT_RE.search(text))
    metrics["has_hard_evidence"] = _has_hard_evidence(item)
    metrics["has_media"] = _has_media(item)
    metrics["event_and_evidence"] = bool(metrics["has_event"] and metrics["has_hard_evidence"])
    metrics["strong_signal"] = bool(metrics["has_event"])


def _quote_too_similar(own_text: str, quoted_text: str) -> bool:
    if not own_text or not quoted_text or len(own_text) < 20:
        return False
    return SequenceMatcher(None, own_text.lower()[:800], quoted_text.lower()[:800]).ratio() >= 0.85


def score_item(
    item: dict[str, Any],
    *,
    recent_texts: list[str] | None = None,
    engagement_baseline: float | None = None,
) -> int:
    metrics = item.get("metrics") or {}
    feed = item.get("feed") or {}
    account = str(metrics.get("account") or "").lower()
    tiers = feed.get("account_tiers") or {}
    tier = str(tiers.get(account) or feed.get("priority") or "P1").upper()
    account_score = 20 if tier == "P0" else 12 if tier == "P1" else 6
    text = _item_text(item)
    _annotate_gates(item)
    intent = 25 if metrics.get("has_event") else 18 if _TECH_RE.search(text) else 8
    evidence = 0
    evidence += 8 if _CODE_RE.search(text) else 0
    evidence += 6 if _NUMBER_RE.search(text) else 0
    evidence += 4 if metrics.get("has_article") else 0
    evidence += 4 if int(metrics.get("thread_count") or 0) >= 3 else 0
    evidence = min(20, evidence)
    novelty = _novelty_score(text, recent_texts or [])
    engagement = float(metrics.get("engagement") or 0)
    followers = max(1.0, float(metrics.get("followers") or 0))
    try:
        published = datetime.fromisoformat(str(item.get("published_raw") or "").replace("Z", "+00:00"))
        age_hours = max(0.5, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    except (ValueError, TypeError):
        age_hours = 24.0
    velocity = engagement / age_hours
    baseline = float(engagement_baseline or 0)
    if baseline > 0:
        ratio = velocity / baseline
    else:
        ratio = velocity / max(1.0, followers / 10000)
    engagement_score = min(15, max(0, round(5 * math.log2(1 + ratio))))
    score = account_score + intent + evidence + novelty + engagement_score
    metrics.update(
        {
            "account_tier": tier,
            "social_score": max(0, min(100, score)),
        }
    )
    return int(metrics["social_score"])


def _hard_filter(item: dict[str, Any], params: dict[str, Any]) -> tuple[bool, str]:
    metrics = item.get("metrics") or {}
    _annotate_gates(item)
    own_text = str(metrics.get("own_text") or _clean_own_text(str(item.get("body") or "")))
    own_chars = int(metrics.get("own_chars") or len(own_text))
    if metrics.get("is_retweet") and not metrics.get("is_quote"):
        return False, "retweet"
    if metrics.get("is_quote"):
        drop_chars = int(params.get("quote_drop_own_chars") or 20)
        if own_chars < drop_chars or _QUOTE_BOILERPLATE_RE.match(own_text):
            return False, "quote_thin"
        if _quote_too_similar(own_text, str(metrics.get("quoted_text") or "")):
            return False, "quote_thin"
    if params.get("exclude_replies", True) and metrics.get("is_reply") and int(
        metrics.get("thread_count") or 1
    ) <= 1:
        return False, "reply"
    if _NOISE_RE.search(_item_text(item)) and not metrics.get("has_event"):
        return False, "noise"
    min_chars = int(params.get("min_content_chars") or 30)
    # 图/视频/X 文章豁免字数闸，交给事实闸；纯短闲聊丢掉。
    if (
        own_chars < min_chars
        and not metrics.get("has_media")
        and not metrics.get("has_article")
        and not metrics.get("has_hard_evidence")
    ):
        return False, "short"
    return True, ""


def _default_llm_classifier(item: dict[str, Any]) -> bool:
    metrics = item.get("metrics") or {}
    quoted = str(metrics.get("quoted_text") or "").strip()
    quoted_block = f"\n被引用原文：{quoted[:800]}" if quoted else ""
    prompt = f"""判断下面的 X 帖子是否构成值得进入 AI 行业简报的“新信号”。
只依据帖子内容；互动高、账号大、配了图都不能单独成为通过理由。

keep=true 当且仅当能抽出一条可核验的新事实，例如：
产品/模型发布或能力变更、开源、价格、论文、融资并购、正式文档/功能上线。
引用或转发官方帖也可以 keep=true，只要本人有增量说明，且能抽出上述事实。

keep=false：闲聊、心情、站队、求互动/报名/招聘、纯使用心得、没有新事实的短评转发。
core_fact 必须是完整陈述句；抽不出就留空，且 keep=false。
只输出严格 JSON：
{{"keep": true, "event_type": "", "core_fact": "", "evidence": "", "relation": "", "confidence": 0, "reject_reason": ""}}

账号：@{metrics.get("account")}
帖子：{item.get("body")}{quoted_block}
"""
    raw = report._llm_json(prompt)
    fact = str(raw.get("core_fact") or "").strip()
    confidence = int(raw.get("confidence") or 0)
    metrics["llm_core_fact"] = fact
    metrics["llm_event_type"] = str(raw.get("event_type") or "")
    metrics["llm_confidence"] = confidence
    # A 策略只看是否抽出了可核验事实。keep/confidence 经常和事实不一致，不作为否决。
    if len(fact) < 16:
        return False
    return not _NOISE_RE.search(fact)


def _resolve_classifier(
    params: dict[str, Any],
    classifier: Callable[[dict[str, Any]], bool] | None,
) -> tuple[Callable[[dict[str, Any]], bool] | None, str]:
    if not params.get("enable_llm_filter", True):
        return None, "llm_disabled"
    if classifier is not None:
        return classifier, ""
    if config.LLM_API_KEY:
        return _default_llm_classifier, ""
    return None, "llm_unavailable"


def _apply_llm_gate(
    item: dict[str, Any],
    decide: Callable[[dict[str, Any]], bool],
) -> str:
    try:
        if decide(item):
            return "llm_keep"
    except Exception as exc:  # noqa: BLE001
        log.warning("X 事实闸智能精筛失败，按精确率优先丢弃：%s", exc)
        return "llm_error"
    return "llm_reject"


def filter_social_items(
    items: list[dict[str, Any]],
    *,
    recent_texts: list[str] | None = None,
    existing_account_counts: dict[str, int] | None = None,
    classifier: Callable[[dict[str, Any]], bool] | None = None,
    skip_daily_cap: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """硬过滤 → 事件∧证据直接过 / 其余走事实闸 LLM → 账号日配额。"""
    recent_texts = recent_texts or []
    counts = Counter(existing_account_counts or {})
    stats: Counter[str] = Counter(raw=len(items))
    candidates: list[dict[str, Any]] = []
    pending_llm: list[dict[str, Any]] = []
    shared_decide: Callable[[dict[str, Any]], bool] | None = None
    for item in items:
        feed = item.get("feed") or {}
        params = feed.get("social_params") or {}
        keep, reason = _hard_filter(item, params)
        if not keep:
            (item.get("metrics") or {})["drop_reason"] = reason
            stats[reason] += 1
            continue
        account = str((item.get("metrics") or {}).get("account") or "").lower()
        baselines = params.get("engagement_baselines") or {}
        score_item(
            item,
            recent_texts=recent_texts,
            engagement_baseline=baselines.get(account),
        )
        metrics = item.get("metrics") or {}
        if metrics.get("event_and_evidence"):
            metrics["gate"] = "direct"
            stats["direct"] += 1
            candidates.append(item)
            continue
        decide, missing = _resolve_classifier(params, classifier)
        if decide is None:
            (item.get("metrics") or {})["drop_reason"] = missing
            stats[missing] += 1
            continue
        shared_decide = decide
        pending_llm.append(item)
    if pending_llm and shared_decide is not None:
        workers = min(4, len(pending_llm))
        if workers == 1:
            outcomes = [_apply_llm_gate(item, shared_decide) for item in pending_llm]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                outcomes = list(executor.map(lambda item: _apply_llm_gate(item, shared_decide), pending_llm))
        for item, reason in zip(pending_llm, outcomes):
            stats[reason] += 1
            if reason == "llm_keep":
                (item.get("metrics") or {})["gate"] = "llm"
                candidates.append(item)
            else:
                (item.get("metrics") or {})["drop_reason"] = reason
    candidates.sort(
        key=lambda item: (
            int((item.get("metrics") or {}).get("social_score") or 0),
            str(item.get("published_raw") or ""),
        ),
        reverse=True,
    )
    kept = []
    for item in candidates:
        if skip_daily_cap:
            kept.append(item)
            continue
        metrics = item.get("metrics") or {}
        account = str(metrics.get("account") or "").lower()
        feed = item.get("feed") or {}
        params = feed.get("social_params") or {}
        tier = str(metrics.get("account_tier") or "P1")
        cap = int((params.get("daily_caps") or {}).get(tier) or (5 if tier == "P0" else 2))
        if counts[account] >= cap:
            (item.get("metrics") or {})["drop_reason"] = "daily_cap"
            stats["daily_cap"] += 1
            continue
        counts[account] += 1
        kept.append(item)
    stats["kept"] = len(kept)
    return kept, dict(stats)


def preview_account(
    username: str,
    *,
    lookback_hours: int = 2160,
    bearer: str | None = None,
    classifier: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], SocialFetchBatch]:
    """只读回放一个账号：不读飞书、不写 cursor、不套日配额。"""
    handle = username.lstrip("@").lower()
    feed = {
        "id": "social-preview",
        "name": f"X · @{handle}",
        "fetch_method": "Social",
        "source_type": "社交媒体",
        "priority": "P0",
        "lookback_hours": lookback_hours,
        "accounts": [handle],
        "account_tiers": {handle: "P0"},
        "cursor_state": {},
        "extra_config": {"max_pages": 32},
        "social_params": {
            "min_content_chars": 30,
            "enable_llm_filter": True,
            "exclude_replies": True,
            "quote_min_own_chars": 80,
            "quote_drop_own_chars": 20,
        },
    }
    batch = fetch_social_sources([feed], bearer=bearer)
    kept, funnel = filter_social_items(
        batch.items,
        classifier=classifier,
        skip_daily_cap=True,
    )
    return kept, funnel, batch


def recent_context(
    records: list[dict[str, Any]], *, now: datetime | None = None
) -> tuple[list[str], dict[str, int]]:
    """从近 7 天飞书条目构造新颖度语料与今日账号已入库计数。"""
    now = now or datetime.now(timezone.utc)
    cutoff = int((now - timedelta(days=7)).timestamp() * 1000)
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    texts: list[str] = []
    account_counts: Counter[str] = Counter()
    for fields in records:
        try:
            stamp = int(float(fields.get("发布时间") or 0))
        except (TypeError, ValueError):
            continue
        if stamp < cutoff:
            continue
        title = str(fields.get("标题") or "")
        body = str(fields.get("原文") or "")
        texts.append(f"{title}\n{body}"[:1000])
        source = str(fields.get("来源") or "")
        match = re.match(r"X\s*·\s*@([\w-]+)", source, re.I)
        if match and datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(
            timezone(timedelta(hours=8))
        ).date() == today:
            account_counts[match.group(1).lower()] += 1
    return texts, dict(account_counts)
