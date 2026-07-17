"""论文质量富集：录用信息(A)、社区热度(D)。

失败降级为 0 / 空，不阻断主流程。不再使用作者影响力（Semantic Scholar）。
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any
from xml.etree import ElementTree as ET

import requests

from . import config

log = logging.getLogger(__name__)

_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([0-9]+\.[0-9]+)(?:v\d+)?", re.I)
_ACCEPT_RE = re.compile(
    r"(?:accepted|to\s+appear)\s*(?:to|at|in|:)?\s*([^\n.;]{3,80})",
    re.I,
)
_KNOWN_VENUE_RE = re.compile(
    r"\b("
    r"neurips|nips|iclr|icml|acl|emnlp|naacl|eacl|coling|findings|"
    r"cvpr|eccv|iccv|wacv|aaai|ijcai|kdd|www|sigir|recsys|"
    r"nature|science|jmlr|tmlr|icra|iros|rss|mlsys|uist|chi|chil"
    r")\b",
    re.I,
)
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "AI-Signal/1.0 (paper-enrich)"})

_META_CACHE: dict[str, dict[str, Any]] = {}
_HEAT_CACHE: dict[str, dict[str, Any]] = {}
_ARXIV_CIRCUIT_OPEN = False
_ARXIV_FAIL_STREAK = 0


def extract_arxiv_id(url: str) -> str:
    m = _ARXIV_ID_RE.search(url or "")
    if not m:
        return ""
    return m.group(1)


def parse_acceptance(comment: str, journal_ref: str = "") -> str:
    """从 comment / journal_ref 抽出可信录用会议/期刊名；解析不到则返回空。"""
    blob = f"{comment or ''} {journal_ref or ''}".strip()
    if not blob:
        return ""
    for m in _ACCEPT_RE.finditer(blob):
        venue = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:|/\\")
        venue = re.split(r"\b(as a|poster|oral|spotlight|workshop)\b", venue, maxsplit=1, flags=re.I)[0]
        venue = venue.strip(" .,;:")[:80]
        if venue and _KNOWN_VENUE_RE.search(venue):
            return venue
    if journal_ref and _KNOWN_VENUE_RE.search(journal_ref):
        return re.sub(r"\s+", " ", journal_ref).strip()[:80]
    return ""


def _score_log(value: float, cap: float) -> float:
    if value <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log1p(value) / math.log1p(cap))


def venue_score(accepted_venue: str, whitelist: list[str] | None, blacklist: list[str] | None) -> tuple[float, str]:
    venue_l = (accepted_venue or "").lower()
    bl = [b.lower() for b in (blacklist or []) if b]
    wl = [w.lower() for w in (whitelist or []) if w]
    if venue_l and bl and any(b in venue_l for b in bl):
        return 0.0, "blacklisted"
    if not venue_l:
        return 20.0, "no_acceptance"
    if wl and any(w in venue_l for w in wl):
        return 100.0, "whitelist"
    return 60.0, "accepted_other"


def community_heat_score(upvotes: float, comments: float) -> float:
    return round(min(100.0, 0.6 * _score_log(upvotes, 500) + 0.4 * _score_log(comments, 80)), 1)


def compute_quality_score(
    *,
    venue: float,
    community: float,
    signal: float,
    community_known: bool = True,
) -> float:
    """综合质量分：录用 + 社区 + 本地 signal（缺社区数据时按可用维归一化）。"""
    parts: list[tuple[float, float]] = [(0.40, venue), (0.35, signal)]
    if community_known:
        parts.append((0.25, community))
    weight_sum = sum(w for w, _ in parts) or 1.0
    return round(min(100.0, sum((w / weight_sum) * v for w, v in parts)), 1)


def fetch_arxiv_meta(arxiv_id: str) -> dict[str, Any]:
    global _ARXIV_CIRCUIT_OPEN, _ARXIV_FAIL_STREAK
    if not arxiv_id:
        return {}
    if arxiv_id in _META_CACHE:
        return _META_CACHE[arxiv_id]
    out: dict[str, Any] = {"arxiv_id": arxiv_id, "comment": "", "journal_ref": "", "authors": []}
    if _ARXIV_CIRCUIT_OPEN:
        _META_CACHE[arxiv_id] = out
        return out
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        resp = _SESSION.get(url, timeout=config.PAPER_ENRICH_TIMEOUT)
        if resp.status_code == 429:
            _ARXIV_FAIL_STREAK += 1
            if _ARXIV_FAIL_STREAK >= 3:
                _ARXIV_CIRCUIT_OPEN = True
                log.warning("arXiv meta 连续限流，本轮跳过后续 comment/journal_ref 拉取")
            _META_CACHE[arxiv_id] = out
            return out
        resp.raise_for_status()
        _ARXIV_FAIL_STREAK = 0
        root = ET.fromstring(resp.text)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        entry = root.find("atom:entry", ns)
        if entry is not None:
            comment = entry.find("arxiv:comment", ns)
            journal = entry.find("arxiv:journal_ref", ns)
            out["comment"] = (comment.text or "").strip() if comment is not None else ""
            out["journal_ref"] = (journal.text or "").strip() if journal is not None else ""
            names = []
            for author in entry.findall("atom:author", ns):
                name = author.find("atom:name", ns)
                if name is not None and name.text:
                    names.append(name.text.strip())
            out["authors"] = names
    except (requests.RequestException, ET.ParseError) as exc:
        log.info("arXiv meta 失败 %s: %s", arxiv_id, exc)
        _ARXIV_FAIL_STREAK += 1
        if _ARXIV_FAIL_STREAK >= 5:
            _ARXIV_CIRCUIT_OPEN = True
    _META_CACHE[arxiv_id] = out
    return out


def fetch_community_heat(arxiv_id: str) -> dict[str, Any]:
    if not arxiv_id:
        return {"upvotes": 0, "comments": 0, "heat_score": 0.0}
    if arxiv_id in _HEAT_CACHE:
        return _HEAT_CACHE[arxiv_id]
    upvotes = 0.0
    comments = 0.0
    try:
        resp = _SESSION.get(
            f"https://huggingface.co/api/papers/{arxiv_id}",
            timeout=config.PAPER_ENRICH_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            upvotes = float(data.get("upvotes") or data.get("uniqueUpvoteCount") or 0)
            comments = float(data.get("numComments") or 0)
    except (requests.RequestException, ValueError, TypeError) as exc:
        log.info("HF Papers 失败 %s: %s", arxiv_id, exc)
    heat = community_heat_score(upvotes, comments)
    out = {"upvotes": upvotes, "comments": comments, "heat_score": heat}
    _HEAT_CACHE[arxiv_id] = out
    return out


def enrich_paper(
    url: str,
    *,
    signal_score: float = 50.0,
    venue_whitelist: list[str] | None = None,
    venue_blacklist: list[str] | None = None,
) -> dict[str, Any]:
    """对单篇论文做 A/D 富集，返回 metrics 增量 + 质量分项。"""
    arxiv_id = extract_arxiv_id(url)
    if not config.PAPER_ENRICH_ENABLED:
        q = compute_quality_score(
            venue=20.0,
            community=0.0,
            signal=float(signal_score or 0),
            community_known=False,
        )
        return {
            "arxiv_id": arxiv_id,
            "accepted_venue": "",
            "arxiv_comment": "",
            "journal_ref": "",
            # 富集关闭时热度未知（None），不要用 0.0 覆盖抽取层已带的 community_heat
            "community_heat": None,
            "community_upvotes": 0,
            "community_comments": 0,
            "venue_score": 20.0,
            "venue_reason": "enrich_disabled",
            "quality_score": q,
            "is_preprint": True if arxiv_id else None,
        }

    meta = fetch_arxiv_meta(arxiv_id) if arxiv_id else {}
    heat = fetch_community_heat(arxiv_id) if arxiv_id else {"upvotes": 0, "comments": 0, "heat_score": 0.0}

    comment = str(meta.get("comment") or "")
    journal_ref = str(meta.get("journal_ref") or "")
    accepted = parse_acceptance(comment, journal_ref)

    v_score, v_reason = venue_score(accepted, venue_whitelist, venue_blacklist)
    c_score = float(heat.get("heat_score") or 0)
    community_known = bool(heat.get("upvotes") or heat.get("comments"))
    q = compute_quality_score(
        venue=v_score,
        community=c_score,
        signal=float(signal_score or 0),
        community_known=community_known,
    )
    return {
        "arxiv_id": arxiv_id,
        "accepted_venue": accepted,
        "arxiv_comment": comment,
        "journal_ref": journal_ref,
        "community_heat": c_score if community_known else None,
        "community_upvotes": heat.get("upvotes") or 0,
        "community_comments": heat.get("comments") or 0,
        "venue_score": v_score,
        "venue_reason": v_reason,
        "quality_score": q,
        "is_preprint": True if arxiv_id and not accepted else None,
    }
