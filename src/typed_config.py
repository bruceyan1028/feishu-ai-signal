"""类型化筛选配置：读取论文/公众号/视频/社交 4 张飞书配置表，
按 source_id 合并到源上，并在清洗阶段按类型分支过滤。

设计：
- 一个源出现在哪张配置表里，就说明它属于那个类型（表内一源一行，主键 source_id）。
- 现在能立刻生效的过滤：关键词包含/排除、正文/摘要最小长度、期刊白/黑名单（文本匹配）、
  从摘要推断的代码链接与论文信号分。
- 依赖抓取层才能取到的指标（影响因子/引用数/阅读量/播放量/粉丝数/是否预印本…）先留钩子：
  仅当 item 的 metrics 里带了对应字段才会真正生效，否则跳过——等抓取层补齐即可自动启用。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import config, feishu

log = logging.getLogger(__name__)

_LIST_SPLIT_RE = re.compile(r"[,，、;；\n\r]+")
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


def _as_list(value: Any) -> list[str]:
    raw = feishu._read_cell_key(value) if not isinstance(value, str) else value
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    return [p.strip().lower() for p in _LIST_SPLIT_RE.split(text) if p.strip()]


def _as_num(value: Any) -> float | None:
    raw = feishu._read_cell_key(value) if not isinstance(value, (int, float, str)) else value
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raw = feishu._read_cell_key(value)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"true", "1", "yes", "是", "on"}


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


# 每种类型：飞书字段名 -> (内部参数键, 解析器)
_SCHEMAS: dict[str, dict[str, tuple[str, Any]]] = {
    "paper": {
        "最低影响因子": ("min_impact_factor", _as_num),
        "期刊会议白名单": ("venue_whitelist", _as_list),
        "期刊会议黑名单": ("venue_blacklist", _as_list),
        "摘要最少字数": ("min_abstract_chars", _as_num),
        "必含关键词": ("keyword_include", _as_list),
        "排除关键词": ("keyword_exclude", _as_list),
        "排除纯预印本": ("exclude_preprint", _as_bool),
        "需含代码仓库": ("require_code", _as_bool),
        "要求已录用": ("require_acceptance", _as_bool),
        "最低质量分": ("min_quality_score", _as_num),
        "最低社区热度": ("min_community_heat", _as_num),
    },
    "wechat": {
        "账号白名单": ("account_whitelist", _as_list),
        "必含关键词": ("keyword_include", _as_list),
        "排除关键词": ("keyword_exclude", _as_list),
        "最低阅读量": ("min_read_count", _as_num),
        "正文最小字数": ("min_content_chars", _as_num),
        "过滤软广": ("exclude_ad", _as_bool),
    },
    "video": {
        "频道白名单": ("channel_whitelist", _as_list),
        "最短时长秒": ("min_duration_sec", _as_num),
        "最长时长秒": ("max_duration_sec", _as_num),
        "最低播放量": ("min_views", _as_num),
        "需要转写": ("need_transcribe", _as_bool),
        "语言": ("lang", lambda v: (feishu._read_cell_key(v) or "")),
        "必含关键词": ("keyword_include", _as_list),
    },
    "social": {
        "账号白名单": ("account_whitelist", _as_list),
        "最低粉丝数": ("min_followers", _as_num),
        "最低互动数": ("min_engagement", _as_num),
        "排除转发": ("exclude_retweets", _as_bool),
        "必含关键词": ("keyword_include", _as_list),
        "排除关键词": ("keyword_exclude", _as_list),
    },
    # GitHub 热榜：参数在抓取层（scrape._fetch_github_items）用于纯沉淀型选仓/打分，
    # 不参与 apply_typed_filter 的通用数值钩子。
    "github": {
        "检索关键词": ("query_terms", _as_list),
        "主题白名单": ("topic_whitelist", _as_list),
        "语言白名单": ("languages", _as_list),
        "必含关键词正则": ("keyword_regex", lambda v: (feishu._read_cell_key(v) or "")),
        "排除主题": ("exclude_topics", _as_list),
        "排除名称正则": ("exclude_name_regex", lambda v: (feishu._read_cell_key(v) or "")),
        "最低星标": ("min_stars", _as_num),
        "最低Fork数": ("min_forks", _as_num),
        "活跃天数": ("active_pushed_days", _as_num),
        "最大条目数": ("max_items", _as_num),
    },
}

_TABLES: list[tuple[str, str]] = [
    ("paper", config.FEISHU_PAPER_CONFIG_TABLE_ID),
    ("wechat", config.FEISHU_WECHAT_CONFIG_TABLE_ID),
    ("video", config.FEISHU_VIDEO_CONFIG_TABLE_ID),
    ("social", config.FEISHU_SOCIAL_CONFIG_TABLE_ID),
    ("github", config.FEISHU_GITHUB_CONFIG_TABLE_ID),
]


def _parse_row(entity_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    schema = _SCHEMAS[entity_type]
    params: dict[str, Any] = {}
    for field_name, (key, parser) in schema.items():
        if field_name not in fields:
            continue
        val = parser(fields[field_name])
        if val in (None, "", [], False):
            continue
        params[key] = val
    # 备注里可写 min_signal_score=55，作为飞书暂无独立字段时的覆盖入口
    notes = str(feishu._read_cell_key(fields.get("备注")) or "")
    m = re.search(r"min_signal_score\s*=\s*(\d+(?:\.\d+)?)", notes, re.I)
    if m:
        params["min_signal_score"] = float(m.group(1))
    return params


def load_typed_configs(token: str) -> dict[str, dict[str, Any]]:
    """读取 4 张配置表，返回 source_id -> {entity_type, params}。

    读取失败不影响主流程（返回已成功的部分），只记录告警。
    """
    result: dict[str, dict[str, Any]] = {}
    for entity_type, table_id in _TABLES:
        if not table_id:
            continue
        try:
            rows = feishu.read_all_records(token, table_id)
        except feishu.FeishuError as exc:
            log.warning("读取 %s 配置表失败: %s", entity_type, exc)
            continue
        for fields in rows:
            source_id = str(feishu._read_cell_key(fields.get("source_id")) or "").strip()
            if not source_id:
                continue
            if source_id in result:
                log.warning(
                    "source_id %s 出现在多张配置表，保留先读到的 %s",
                    source_id,
                    result[source_id]["entity_type"],
                )
                continue
            params = _parse_row(entity_type, fields)
            # arXiv 默认套用信号分与质量分阈值；顶刊不套，避免误杀
            if (
                entity_type == "paper"
                and source_id.startswith("arxiv-")
                and "min_signal_score" not in params
            ):
                params["min_signal_score"] = float(config.ARXIV_MIN_SIGNAL_SCORE)
            if (
                entity_type == "paper"
                and source_id.startswith("arxiv-")
                and "min_quality_score" not in params
            ):
                params["min_quality_score"] = float(config.PAPER_QUALITY_MIN_SCORE)
            # arXiv 预印本默认要求有社区热度才保留（无人讨论=丢弃）；顶刊/会议不套
            if (
                entity_type == "paper"
                and source_id.startswith("arxiv-")
                and config.ARXIV_REQUIRE_COMMUNITY_HEAT
                and "require_community_heat" not in params
            ):
                params["require_community_heat"] = True
            result[source_id] = {
                "entity_type": entity_type,
                "params": params,
            }
    return result


# 数据依赖型阈值：参数键 -> item.metrics 里的字段名。仅当 metrics 提供该字段才会生效。
_NUMERIC_HOOKS = [
    ("min_impact_factor", "impact_factor"),
    ("min_read_count", "read_count"),
    ("min_views", "views"),
    ("min_followers", "followers"),
    ("min_engagement", "engagement"),
    ("min_duration_sec", "duration_sec"),
    ("min_signal_score", "signal_score"),
    ("min_quality_score", "quality_score"),
    ("min_community_heat", "community_heat"),
]


def apply_typed_filter(
    entity_type: str, params: dict[str, Any], ctx: dict[str, Any]
) -> tuple[bool, str]:
    """按类型过滤单条。返回 (是否保留, 命中原因)。"""
    if not params:
        return True, ""
    text: str = ctx.get("text") or ""
    body_len: int = ctx.get("body_len") or 0
    metrics: dict[str, Any] = ctx.get("metrics") or {}

    # 1) 通用文本过滤（当前即可生效）
    include = params.get("keyword_include")
    if include and not any(term in text for term in include):
        return False, "keyword_include"
    exclude = params.get("keyword_exclude")
    if exclude and any(term in text for term in exclude):
        return False, "keyword_exclude"

    min_chars = params.get("min_abstract_chars") or params.get("min_content_chars")
    if min_chars and body_len < min_chars:
        return False, "min_chars"

    # 2) 论文期刊/会议：白名单对照录用场馆（accepted_venue）；黑名单可对照录用或正文
    if entity_type == "paper":
        wl = params.get("venue_whitelist")
        accepted = str(metrics.get("accepted_venue") or "").lower()
        if wl and accepted and not any(v in accepted for v in wl):
            return False, "venue_whitelist"
        bl = params.get("venue_blacklist")
        hay = f"{accepted} {text}".lower()
        if bl and any(v in hay for v in bl):
            return False, "venue_blacklist"

    # 3) 白名单类（需要抓取层提供 account/channel，暂留钩子）
    account = str(metrics.get("account") or metrics.get("channel") or "").lower()
    for wl_key in ("account_whitelist", "channel_whitelist"):
        wl = params.get(wl_key)
        if wl and account and not any(a in account for a in wl):
            return False, wl_key

    # 4) 数值阈值钩子：metrics 提供了对应字段才判定
    for pkey, mkey in _NUMERIC_HOOKS:
        threshold = params.get(pkey)
        val = metrics.get(mkey)
        if threshold is not None and val is not None:
            try:
                if float(val) < float(threshold):
                    return False, pkey
            except (TypeError, ValueError):
                pass

    max_dur = params.get("max_duration_sec")
    dur = metrics.get("duration_sec")
    if max_dur is not None and dur is not None:
        try:
            if float(dur) > float(max_dur):
                return False, "max_duration_sec"
        except (TypeError, ValueError):
            pass

    # 5) 布尔钩子：metrics 提供事实字段才判定
    # 社区热度硬门：与数值阈值不同，缺失（None/0）也判丢，用于挡掉无人讨论的 arXiv 长尾。
    # 已录用会议/顶刊（accepted_venue）豁免——正式发表本身就是高价值信号。
    if params.get("require_community_heat") and not metrics.get("accepted_venue"):
        heat = metrics.get("community_heat")
        try:
            if heat is None or float(heat) <= 0:
                return False, "require_community_heat"
        except (TypeError, ValueError):
            return False, "require_community_heat"
    if params.get("exclude_preprint") and metrics.get("is_preprint") is True:
        return False, "exclude_preprint"
    if params.get("require_code") and metrics.get("has_code") is not True:
        return False, "require_code"
    if params.get("exclude_ad") and metrics.get("is_ad") is True:
        return False, "exclude_ad"
    if params.get("exclude_retweets") and metrics.get("is_retweet") is True:
        return False, "exclude_retweets"
    if params.get("require_acceptance") and not metrics.get("accepted_venue"):
        return False, "require_acceptance"
    # 期刊黑名单：录用场馆命中即丢（venue_score 已标 blacklisted）
    if metrics.get("venue_reason") == "blacklisted":
        return False, "venue_blacklist"

    return True, ""
