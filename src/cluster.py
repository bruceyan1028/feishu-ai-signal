"""同事件聚类：标题近似匹配 + tier/priority 择优，并为详情页组装「事件聚合」。"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

_TITLE_NOISE_RE = re.compile(
    r"[^\w\u4e00-\u9fff]+|(独家|详解|速递|刚刚|突发|重磅|报道|：|:)",
    re.I,
)

TIER_SCORE = {"L1": 40, "L2": 25, "L3": 12, "L4": 5}
PRIORITY_SCORE = {"P0": 30, "P1": 18, "P2": 8}

GROUP_OFFICIAL = "官方来源"
GROUP_RELATED = "相关报道"
GROUP_CROSS = "交叉验证"
GROUP_EXTRA = "补充视角"
GROUP_TONES = {
    GROUP_OFFICIAL: "#16130f",
    GROUP_RELATED: "#2b6cb0",
    GROUP_CROSS: "#1f7a45",
    GROUP_EXTRA: "#a06600",
}


def normalize_title(title: str) -> str:
    text = str(title or "").strip().lower()
    text = _TITLE_NOISE_RE.sub("", text)
    return text


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # 短标题被长标题包含（常见于「原题 + 媒体后缀」）
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 8 and shorter in longer:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def _tier_code(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    for code in ("L1", "L2", "L3", "L4"):
        if text.startswith(code) or code in text:
            return code
    return "L3"


def prefer_score(
    *,
    tier: Any = "",
    priority: str = "P2",
    source_type: str = "",
    stamp: int = 0,
    body_len: int = 0,
) -> tuple[int, int, int]:
    """分数越高越优先保留；同分时更早发布、正文更长者优先。"""
    tier_pts = TIER_SCORE.get(_tier_code(tier), 10)
    pri_pts = PRIORITY_SCORE.get(str(priority or "P2").upper(), 8)
    type_pts = 0
    st = str(source_type or "")
    if st in ("纯网页", "Company Blog") or "官方" in st:
        type_pts += 6
    if st in ("论文", "Research"):
        type_pts -= 2
    # stamp 越大越新；择优时同级偏早发 → 用负 stamp 排序时再处理
    return (tier_pts + pri_pts + type_pts, -int(stamp or 0), int(body_len or 0))


def cluster_by_title(
    items: list[dict[str, Any]],
    *,
    threshold: float = 0.85,
    title_key: str = "title",
) -> list[list[dict[str, Any]]]:
    """贪心聚类：按输入顺序，与已有簇代表标题相似度 ≥ threshold 则并入。"""
    clusters: list[list[dict[str, Any]]] = []
    reps: list[str] = []
    for item in items:
        title = str(item.get(title_key) or item.get("titleCn") or item.get("zhTitle") or "")
        placed = False
        for index, rep in enumerate(reps):
            if title_similarity(title, rep) >= threshold:
                clusters[index].append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
            reps.append(title)
    return clusters


def _member_payload(item: dict[str, Any], note: str) -> dict[str, str]:
    title = str(
        item.get("titleCn")
        or item.get("zhTitle")
        or item.get("title")
        or item.get("summary")
        or ""
    ).strip()
    summary = str(item.get("summary") or "").strip()
    headline = title or summary[:80]
    return {
        "source": str(item.get("source") or "未知来源"),
        "title": headline,
        "url": str(item.get("url") or ""),
        "note": note,
        "text": f"{headline} —— {note}" if headline else note,
    }


def classify_group(item: dict[str, Any], *, is_primary: bool) -> str:
    tier = _tier_code(item.get("tier") or item.get("层级") or "")
    source_type = str(item.get("source_type") or item.get("contentType") or item.get("kind") or "")
    if is_primary or tier == "L1":
        return GROUP_OFFICIAL
    if source_type in ("论文",) or "arxiv" in str(item.get("url") or "").lower():
        return GROUP_CROSS
    if tier in ("L2", "L3"):
        return GROUP_RELATED
    return GROUP_EXTRA


def _role_note(group: str, *, is_primary: bool) -> str:
    if is_primary or group == GROUP_OFFICIAL:
        return "原始发布 / 公告。"
    if group == GROUP_RELATED:
        return "同事件的其它源头报道。"
    if group == GROUP_CROSS:
        return "可用于交叉印证事实与影响边界。"
    return "补充同事件的背景或旁证。"


def build_event_aggregation(
    primary: dict[str, Any],
    siblings: list[dict[str, Any]],
) -> dict[str, Any]:
    """组装详情页事件聚合分组；每条必须带可点击 url。"""
    buckets: dict[str, list[dict[str, str]]] = {
        GROUP_OFFICIAL: [],
        GROUP_RELATED: [],
        GROUP_CROSS: [],
        GROUP_EXTRA: [],
    }
    primary_group = classify_group(primary, is_primary=True)
    buckets[primary_group].append(
        _member_payload(primary, _role_note(primary_group, is_primary=True))
    )
    for sib in siblings:
        if not str(sib.get("url") or "").strip():
            continue
        # 跳过与主条目同 URL
        if str(sib.get("url") or "").rstrip("/") == str(primary.get("url") or "").rstrip("/"):
            continue
        group = classify_group(sib, is_primary=False)
        buckets[group].append(_member_payload(sib, _role_note(group, is_primary=False)))

    groups = []
    for key in (GROUP_OFFICIAL, GROUP_RELATED, GROUP_CROSS, GROUP_EXTRA):
        items = [it for it in buckets[key] if it.get("url")]
        if not items:
            continue
        groups.append({"key": key, "tone": GROUP_TONES[key], "items": items})
    total = sum(len(g["items"]) for g in groups)
    return {"total": total, "groups": groups}


def collapse_for_brief(
    candidates: list[dict[str, Any]],
    *,
    threshold: float = 0.85,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """对候选做同事件折叠：每簇只保留最优主条目，siblings 挂到 eventPeers。"""
    if not candidates:
        return []

    enriched: list[dict[str, Any]] = []
    for item in candidates:
        fields = item.get("fields") or {}
        title = str(
            _scalar(fields.get("中文标题"))
            or _scalar(fields.get("标题"))
            or item.get("title")
            or ""
        )
        row = {
            **item,
            "title": title,
            "titleCn": str(_scalar(fields.get("中文标题")) or title),
            "source": str(_scalar(fields.get("来源")) or item.get("source") or ""),
            "url": _link(fields.get("链接")) or str(item.get("url") or ""),
            "tier": str(_scalar(fields.get("层级")) or item.get("tier") or ""),
            "source_type": str(_scalar(fields.get("来源类型")) or item.get("source_type") or ""),
            "summary": str(_scalar(fields.get("中文摘要")) or item.get("summary") or ""),
            "stamp": int(item.get("stamp") or 0),
            "priority": str(item.get("priority") or "P2"),
            "body_len": len(str(_scalar(fields.get("原文")) or "")),
        }
        row["_score"] = prefer_score(
            tier=row["tier"],
            priority=row["priority"],
            source_type=row["source_type"],
            stamp=row["stamp"],
            body_len=row["body_len"],
        )
        enriched.append(row)

    clusters = cluster_by_title(enriched, threshold=threshold, title_key="title")
    primaries: list[dict[str, Any]] = []
    for cluster in clusters:
        ordered = sorted(cluster, key=lambda x: x["_score"], reverse=True)
        primary = dict(ordered[0])
        siblings = [dict(x) for x in ordered[1:]]
        primary["eventPeers"] = siblings
        primaries.append(primary)
        if limit is not None and len(primaries) >= limit:
            break
    return primaries


def attach_aggregations(signals: list[dict[str, Any]], *, threshold: float = 0.85) -> list[dict[str, Any]]:
    """给简报信号列表挂上 eventAggregation；同簇非主条目不重复进入最终列表。"""
    if not signals:
        return []
    for signal in signals:
        signal.setdefault("title", signal.get("title") or signal.get("titleCn") or "")
        signal["_score"] = prefer_score(
            tier=signal.get("tier") or "",
            priority=signal.get("priority") or "P2",
            source_type=signal.get("contentType") or signal.get("source_type") or "",
            stamp=_date_stamp(signal.get("publishedDate") or signal.get("date") or ""),
            body_len=len(str(signal.get("summary") or "")),
        )

    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for signal in signals:
        rid = str(signal.get("recordId") or signal.get("url") or id(signal))
        if rid in used:
            continue
        peers = list(signal.get("eventPeers") or [])
        if not peers:
            for other in signals:
                oid = str(other.get("recordId") or other.get("url") or id(other))
                if oid == rid or oid in used:
                    continue
                if title_similarity(
                    str(signal.get("titleCn") or signal.get("title")),
                    str(other.get("titleCn") or other.get("title")),
                ) >= threshold:
                    peers.append(other)
                    used.add(oid)
        used.add(rid)
        for peer in peers:
            used.add(str(peer.get("recordId") or peer.get("url") or id(peer)))
        signal = dict(signal)
        signal["eventAggregation"] = build_event_aggregation(signal, peers)
        signal.pop("eventPeers", None)
        signal.pop("_score", None)
        out.append(signal)
    return out


def enrich_with_pool(
    signals: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    *,
    threshold: float = 0.85,
) -> list[dict[str, Any]]:
    """用条目池为每条信号补齐同事件其它源头，再生成 eventAggregation。"""
    if not signals:
        return []
    signal_ids = {str(s.get("recordId") or "") for s in signals}
    for signal in signals:
        peers: list[dict[str, Any]] = list(signal.get("eventPeers") or [])
        seen = {str(signal.get("recordId") or ""), *(str(p.get("recordId") or "") for p in peers)}
        title = str(signal.get("titleCn") or signal.get("title") or "")
        for other in pool:
            oid = str(other.get("recordId") or "")
            if not oid or oid in seen:
                continue
            other_title = str(other.get("titleCn") or other.get("title") or "")
            sim = title_similarity(title, other_title)
            if sim < threshold:
                continue
            if oid in signal_ids and sim < threshold:
                continue
            peers.append(other)
            seen.add(oid)
        signal["eventPeers"] = peers
    return attach_aggregations(signals, threshold=threshold)


def _scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and value:
        x = value[0]
        if isinstance(x, dict) and "text" in x:
            return x["text"]
        return x
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or value.get("link")
    return str(value)


def _link(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or "").strip()
    return str(value or "").strip()


def _date_stamp(value: str) -> int:
    text = str(value or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return 0
    try:
        from datetime import datetime, timezone

        return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0
