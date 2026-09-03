"""论文评估：本地信号分 → 信号分门槛 → 录用信息(A) / 社区热度(D) 富集 → 质量分。

清洗漏斗里所有论文专属逻辑（含唯一一处外网请求）都收在这里，`evaluate_paper`
是唯一入口；process / diag_paper 只拿结果，不碰富集细节。
失败降级为 0 / 空，不阻断主流程。不再使用作者影响力（Semantic Scholar）。
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

import requests

from . import config

log = logging.getLogger(__name__)

_CODE_LINK_RE = re.compile(
    r"(?:github\.com|gitlab\.com)/[\w.-]+/[\w.-]+|"
    r"huggingface\.co/(?:spaces|models|datasets)/[\w.-]+|"
    r"(?:https?://)?[\w.-]+\.github\.io/",
    re.I,
)

# 论文轻量信号分：仅用标题+摘要，不依赖外部 API。基准 50，再加减。
_SIGNAL_POS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(state[- ]of[- ]the[- ]art|sota)\b", re.I), 12),
    (re.compile(r"\b(benchmark|leaderboard)\b", re.I), 8),
    (re.compile(r"\b(open[- ]source|released? (?:code|model|weights))\b", re.I), 10),
    (re.compile(r"\b(foundation model|large language model|\bllm\b|multimodal|agentic)\b", re.I), 8),
    (re.compile(r"\b(reasoning|planning|tool[- ]use|rlhf|dpo|grpo|moe)\b", re.I), 8),
    (re.compile(r"\b(outperform|surpass|beats?|improves? over)\b", re.I), 6),
    (re.compile(r"\b(neurips|iclr|icml|cvpr|eccv|acl|emnlp|aaai|nature|science|jmlr)\b", re.I), 15),
]
_SIGNAL_NEG: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(lecture notes?|homework|course(?:work| project)|problem set|tutorial slides?)\b", re.I), -45),
    (re.compile(r"\b(undergraduate|course project|class project|term paper)\b", re.I), -35),
    (re.compile(r"\b(retracted|withdrawn|duplicate submission)\b", re.I), -50),
    (re.compile(r"\b(position paper|opinion|perspective only)\b", re.I), -8),
    (re.compile(r"\b(preliminary|work in progress|extended abstract)\b", re.I), -10),
]

# enrich_paper 返回值里要同步进 metrics 的键（供 typed_filter 的数值/布尔钩子判定）
_METRIC_KEYS = ("accepted_venue", "community_heat", "venue_score", "venue_reason", "quality_score")


def infer_paper_metrics(title: str, body: str, url: str = "") -> dict[str, Any]:
    """从标题/摘要推断论文轻量指标（无需外部 API）。"""
    text = f"{title}\n{body}\n{url}"
    has_code = bool(_CODE_LINK_RE.search(text))
    score = 50
    if has_code:
        score += 15
    if len(body or "") >= 800:
        score += 5
    elif len(body or "") < 200:
        score -= 10
    for pattern, delta in _SIGNAL_POS:
        if pattern.search(text):
            score += delta
    for pattern, delta in _SIGNAL_NEG:
        if pattern.search(text):
            score += delta
    return {
        "has_code": has_code,
        "signal_score": max(0, min(100, score)),
        "is_preprint": "arxiv.org/" in (url or "").lower(),
    }


@dataclass
class PaperVerdict:
    keep: bool
    reason: str = ""
    quality_fields: dict[str, Any] = field(default_factory=dict)
    enriched: bool = False
    enrich_ms: float = 0.0


def evaluate_paper(
    title: str,
    body_text: str,
    url: str,
    params: dict[str, Any] | None,
    metrics: dict[str, Any],
) -> PaperVerdict:
    """漏斗里的论文分支：本地信号分 → min_signal_score 硬门 → 外网富集 → 质量分。

    metrics 就地更新（signal_score / has_code / is_preprint 及富集回填的录用、热度、
    质量分），供后续 apply_typed_filter 读取。信号分门槛放在富集之前，是为了把
    明显不像论文的条目挡在外网请求之外。
    """
    params = params or {}
    metrics.update(infer_paper_metrics(title, body_text, url))

    min_sig = params.get("min_signal_score")
    if min_sig is not None and metrics.get("signal_score") is not None:
        if float(metrics["signal_score"]) < float(min_sig):
            return PaperVerdict(keep=False, reason="min_signal_score")

    t0 = time.perf_counter()
    enriched = enrich_paper(
        url,
        signal_score=float(metrics.get("signal_score") or 50),
        venue_whitelist=params.get("venue_whitelist"),
        venue_blacklist=params.get("venue_blacklist"),
    )
    enrich_ms = (time.perf_counter() - t0) * 1000

    # 录用后不再视为纯预印本
    if enriched.get("accepted_venue"):
        metrics["is_preprint"] = False
    elif enriched.get("arxiv_id"):
        metrics["is_preprint"] = True
    metrics.update({k: enriched[k] for k in _METRIC_KEYS if enriched.get(k) is not None})

    quality_fields = {
        "quality_score": enriched.get("quality_score"),
        "accepted_venue": enriched.get("accepted_venue") or "",
        "community_heat": enriched.get("community_heat"),
        "paper_metrics_json": {
            "arxiv_id": enriched.get("arxiv_id"),
            "comment": enriched.get("arxiv_comment"),
            "journal_ref": enriched.get("journal_ref"),
            "venue_score": enriched.get("venue_score"),
            "venue_reason": enriched.get("venue_reason"),
            "accepted_venue": enriched.get("accepted_venue"),
            "community": {
                "upvotes": enriched.get("community_upvotes"),
                "comments": enriched.get("community_comments"),
                "heat": enriched.get("community_heat"),
            },
            "signal_score": metrics.get("signal_score"),
            "quality_score": enriched.get("quality_score"),
        },
    }
    return PaperVerdict(keep=True, quality_fields=quality_fields, enriched=True, enrich_ms=enrich_ms)

_ARXIV_ID_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/)"
    r"([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
    re.I,
)
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
