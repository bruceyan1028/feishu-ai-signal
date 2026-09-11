"""本机信号源状态看板：一个源到底在干活、还是在空转、还是根本跑不通。

回答四个问题，每个都得给出可核对的字段，而不是一句「正常 / 异常」：

- **哪些源有效**：健康日志里入库过、且断流天数在阈值内的 active 源。
- **历史上采集了多少**：`output/health/` 逐轮漏斗的累计 raw / written，以及
  `data/items/*.jsonl` 里真正落盘的条目数（两个口径对不上是正常的：前者含
  已被后续环节丢弃的量，后者是本机能看到的成品）。
- **贡献多少**：该源条目数占全量的百分比，以及被选进简报的条数——天天入库却
  从不入选，和一条都抓不到，都是要处理的事，但根因完全不同。
- **静默 / 失效**：这里必须分清两类 0。飞书回写的「条目数」是覆盖式快照，答不了
  这个区别；`output/health/` 里的分源漏斗可以：

  | 表象 | 漏斗特征 | 归属 |
  | --- | --- | --- |
  | 0 条 | `raw > 0` 却被某一级淘汰 | **被规则卡死**，改配置能救 |
  | 0 条 | `raw = 0` 且 `fetch.error` 有值 | **根本没跑通**，链路问题 |
  | 0 条 | 连一行健康记录都没有 | **从未被采集过**，不在点名范围内 |

  第三种最容易被误当成前两种：参数表里 status 是 active，但没有任何一轮尝试过它。

数据来源按可用性降级，全都不需要飞书在线：

    output/health/dt=*.jsonl    逐轮分源漏斗（主数据，只在 output/ 有，git 忽略）
    data/items/*.jsonl  data/tagged/*.jsonl   本地已落盘条目（贡献与历史口径）
    site/data/brief-20*.json    简报入选量
    site/data/sources.json      源配置只读快照（离线替代飞书）
    src/seed_default.json       再退一步的种子；无运行时统计，统计列会是空的

飞书在线时优先读飞书一级参数表，拿到最新配置与最近一轮回写。

    python -m src.source_board                      # 生成 output/source-board.html
    python -m src.source_board --days 60            # 只看最近 60 天的健康记录
    python -m src.source_board --open               # 生成后直接打开
    python -m src.source_board --offline            # 跳过飞书，直接用站点快照

只读，不写飞书，不发网络请求（飞书除外，且失败即降级）。
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
HEALTH_DIR = ROOT / "output" / "health"
ITEMS_DIR = ROOT / "data" / "items"
TAGGED_DIR = ROOT / "data" / "tagged"
SITE_DATA = ROOT / "site" / "data"
SEED_FILE = ROOT / "src" / "seed_default.json"
DEFAULT_HTML = ROOT / "output" / "source-board.html"
DEFAULT_CSV = ROOT / "output" / "source-board.csv"

# 判定「还在产出」的宽限期。日报按天跑，7 天覆盖一个完整自然周的节奏波动；
# 比这更短会把周末不更新的博客误判成停摆，更长则掩盖真实断流。
DRY_LIMIT_DAYS = 7

# health.FUNNEL_STAGES 的中文名。漏斗顺序即清洗顺序，看板上按这个顺序摊开，
# 一眼能看出一个源是在入站就空、还是被自己的关键词正则挡在门口。
STAGE_LABELS: dict[str, str] = {
    "raw": "抓取到",
    "per_feed_cap": "单源上限",
    "missing_title_url": "缺标题或链接",
    "title_exclude_regex": "标题排除正则",
    "missing_or_invalid_date": "缺发布日期",
    "lookback": "超出时间窗",
    "keyword_regex": "未命中关键词",
    "keyword_include": "未命中必含词",
    "keyword_exclude": "命中排除词",
    "min_signal_score": "本地信号分不足",
    "min_quality_score": "富集后质量分不足",
    "min_chars": "正文过短（类型规则）",
    "min_content_chars": "正文过短（补全后仍不足）",
    "min_duration_sec": "时长不足",
    "typed_filter": "类型规则其他",
    "dup_round": "本轮内重复",
    "kept": "留下",
}

# 淘汰原因 -> 归因。区分「改配置能救」和「得改代码 / 换端点」，这是看板存在的意义。
STAGE_ACTION: dict[str, str] = {
    "lookback": "时间窗",
    "missing_or_invalid_date": "时间窗",
    "keyword_regex": "主题门",
    "keyword_include": "主题门",
    "keyword_exclude": "主题门",
    "title_exclude_regex": "主题门",
    "min_signal_score": "质量门",
    "min_quality_score": "质量门",
    "min_chars": "质量门",
    "min_content_chars": "质量门",
    "min_duration_sec": "质量门",
    "typed_filter": "类型规则",
    "per_feed_cap": "配额",
    "dup_round": "去重",
    "missing_title_url": "解析",
}

# 抓取错误 -> 人话。这些字符串来自 health.build_records 记录的 fetch.error，
# 改那边必须同步改这里。
FETCH_ERROR_LABELS: dict[str, str] = {
    "feed_empty": "RSS 返回空 feed",
    "list_empty_or_failed": "列表页没抽到条目",
    "no_links_extracted": "抽到页面但没抽到链接",
    "unparseable_or_empty": "页面无法解析",
    "articles_failed": "列表有了但正文全失败",
    "fetch_failed: HTTPError": "请求被拒（4xx/5xx）",
    "fetch_failed: ConnectTimeout": "连接超时",
    "fetch_failed: ConnectionError": "连接失败",
    "fetch_failed: SSLError": "TLS 失败",
}

OUTCOME_LABELS = {
    "effective": "有效",
    "degraded": "低产",
    "rule_blocked": "被规则卡死",
    "post_filter": "过清洗没入库",
    "fetch_broken": "抓取失败",
    "dry": "静默无产出",
    "never_run": "从未采集",
    "paused": "已暂停",
}
OUTCOME_ORDER = (
    "effective",
    "degraded",
    "rule_blocked",
    "post_filter",
    "fetch_broken",
    "dry",
    "never_run",
    "paused",
)
OUTCOME_HELP = {
    "effective": f"active 源，近 {DRY_LIMIT_DAYS} 天内有入库",
    "degraded": f"active 源，能入库但断流 {DRY_LIMIT_DAYS} 天以上",
    "rule_blocked": "抓到了原始条目，却被自己的规则一条不剩地筛掉：改配置能救",
    "post_filter": "清洗全过了，死在末端去重或富集后打分：不是抓取问题，改规则也要看末端",
    "fetch_broken": "一条原始条目都没抓到，抓取环节就失败了：改规则救不了",
    "dry": "抓取与规则都正常，观测期内仍无入库",
    "never_run": "参数表里有它，但观测期内没有任何一轮采集尝试过它",
    "paused": "status 非 active，正式流水线不跑",
}

STATUS_LABELS = {"active": "已接入", "experimental": "待测", "paused": "已暂停"}


def _brief_board(dimension: str, source_format: str) -> str:
    """日报分池归类，必须与 daily 的技术开源判定保持一致。"""
    if source_format == "视频":
        return "视频"
    if source_format == "播客":
        return "播客"
    if source_format in {"社交媒体", "Social"}:
        return "社媒"
    if dimension == "技术研究开源" or source_format in {"论文", "Github热榜"}:
        return "技术开源"
    return "新闻"


def _priority_rule(board: str, configured_priority: str) -> str:
    if board == "新闻":
        return f"生效（{configured_priority or 'P2'}）"
    return "已取消（独立排序）"


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def _env_from_dotenv() -> None:
    """本地直接跑时把 .env 灌进环境；已经设过的变量不覆盖。"""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_config(*, offline: bool = False) -> tuple[list[dict[str, Any]], str]:
    """源配置：飞书 -> 站点快照 -> 种子。返回 (记录, 来源标记)。

    三个来源的字段形状不同，统一收敛成飞书字段名，后面的逻辑只认这一种形状。
    """
    if not offline:
        try:
            _env_from_dotenv()
            from . import config as cfg
            from . import feishu

            cfg.validate()
            records = feishu.read_param_records(feishu.get_tenant_access_token())
            if records:
                return records, "feishu"
        except Exception as exc:  # noqa: BLE001 - 看板不该因为飞书挂了就起不来
            print(f"读取飞书失败（{exc.__class__.__name__}: {exc}），改用站点快照")

    payload = _read_json(SITE_DATA / "sources.json")
    rows = (payload or {}).get("sources") or []
    if rows:
        stamp = str((payload or {}).get("generatedAt") or "")
        return [_snapshot_to_record(row) for row in rows], f"site-snapshot({stamp})"

    bundle = _read_json(SEED_FILE) or {}
    seed = bundle.get("一级参数") or []
    return [{"record_id": "", "fields": row} for row in seed], "seed(无运行时统计)"


def _snapshot_to_record(row: dict[str, Any]) -> dict[str, Any]:
    """站点快照的 camelCase 字段还原成飞书字段名。"""
    status = str(row.get("status") or "")
    return {
        "record_id": str(row.get("recordId") or ""),
        "fields": {
            "source_id": row.get("id"),
            "name": row.get("name"),
            "endpoint": {"link": row.get("url") or "", "text": row.get("url") or ""},
            "status": status,
            "来源类型": row.get("format"),
            "dimension": row.get("type"),
            "priority": {"高": "P0", "中": "P1", "低": "P2"}.get(
                str(row.get("priority") or ""), str(row.get("priority") or "")
            ),
            "fetch_method": row.get("fetchMethod"),
            "lookback_window": row.get("lookback"),
            "最近采集时间": _stamp_to_ms(row.get("last")),
            "条目数": row.get("perDay"),
        },
    }


def _stamp_to_ms(text: Any) -> int:
    """站点快照的 `09-10 11:04` 还原成毫秒时间戳，年份按当前年补。"""
    raw = str(text or "").strip()
    if not raw or raw == "-":
        return 0
    for fmt in ("%m-%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=datetime.now(CN_TZ).year)
        return int(parsed.timestamp() * 1000)
    return 0


def load_health(days: int) -> list[dict[str, Any]]:
    if not HEALTH_DIR.is_dir():
        return []
    cutoff = (datetime.now(CN_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for path in sorted(HEALTH_DIR.glob("dt=*.jsonl")):
        if path.name[len("dt=") : -len(".jsonl")] < cutoff:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def load_items() -> tuple[dict[str, int], dict[str, int], str, str]:
    """本地落盘条目：源名 -> 条数 / 天数，返回 (条数, 覆盖天数, 起始日, 结束日)。

    条目表只存源显示名不存 source_id，所以这里按名字归并，聚合阶段再用名字反查 id。
    数据源优先 data/tagged（含打标结果，行数与 items 一致），缺了才退回 items。
    """
    directory = TAGGED_DIR if any(TAGGED_DIR.glob("*.jsonl")) else ITEMS_DIR
    counts: dict[str, int] = {}
    days: dict[str, set[str]] = {}
    seen_dates: list[str] = []
    if not directory.is_dir():
        return counts, {}, "", ""
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            name = str(item.get("source") or "").strip()
            date = str(item.get("date") or "").strip()
            if date:
                seen_dates.append(date)
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
            if date:
                days.setdefault(name, set()).add(date)
    span = (min(seen_dates), max(seen_dates)) if seen_dates else ("", "")
    return counts, {k: len(v) for k, v in days.items()}, span[0], span[1]


def load_brief_counts(limit: int = 8) -> tuple[dict[str, int], dict[str, str], int]:
    """近 limit 期简报里每个源的入选次数、最近入选日和期数。"""
    counts: dict[str, int] = {}
    last_dates: dict[str, str] = {}
    files = sorted(SITE_DATA.glob("brief-20*.json"), reverse=True)[:limit]
    for path in files:
        payload = _read_json(path) or {}
        groups = [
            payload.get("signals") or [], payload.get("technicalSignals") or [],
            payload.get("paperSignals") or [],
            payload.get("videoSignals") or [], payload.get("podcastSignals") or [],
            payload.get("socialPosts") or [],
        ]
        for signals in groups:
            for signal in signals:
                sid = str((signal or {}).get("sourceId") or "").strip()
                if sid:
                    counts[sid] = counts.get(sid, 0) + 1
                    date = str(payload.get("date") or path.stem.removeprefix("brief-"))
                    if date > last_dates.get(sid, ""):
                        last_dates[sid] = date
    return counts, last_dates, len(files)


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------
def _cell(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("text") or raw.get("link") or "").strip()
    if isinstance(raw, list):
        return "".join(_cell(x) for x in raw).strip()
    return str(raw or "").strip()


def _int(raw: Any) -> int:
    try:
        return int(float(_cell(raw) or 0))
    except (TypeError, ValueError):
        return 0


def _name_key(name: str) -> str:
    """源名的宽松等价形式：条目表只存显示名，改名后精确匹配会对不上。"""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# 采集端点常年是 RSS/Atom 路径，不是"官网"。少数几个挂在第三方播客
# CDN（megaphone/fireside/libsyn/...）或代理（rsshub/google news）上的源，
# 域名本身就不对，剥不出真官网，只能手工核实后固定下来。
_HOMEPAGE_OVERRIDES = {
    "podcast-no-priors": "https://www.no-priors.com/",
    "podcast-cognitive-revolution": "https://www.cognitiverevolution.ai/",
    "podcast-twiml": "https://twimlai.com/",
    "podcast-training-data": "https://www.sequoiacap.com/podcast/training-data/",
    "podcast-nvidia-ai": "https://ai-podcast.nvidia.com/",
    "podcast-eye-on-ai": "https://www.eye-on.ai/",
    "podcast-last-week-in-ai": "https://lastweekin.ai/",
    "podcast-crossing": "https://www.xiaoyuzhoufm.com/podcast/60502e253c92d4f62c2a9577",
    "podcast-silicon-valley-101": "https://sv101.fireside.fm/",
    "podcast-dwarkesh": "https://www.dwarkeshpatel.com/",
    "podcast-gradient-dissent": "https://wandb.ai/site/resources/podcast/",
    "podcast-latent-space": "https://www.latent.space/",
    "podcast-practical-ai": "https://practicalai.fm/",
    "latepost-ai": "https://www.latepost.com/",
    "reuters-ai": "https://www.reuters.com/technology/artificial-intelligence/",
    "jiemian": "https://www.jiemian.com/",
    "social-media": "https://x.com/",
    "utility-dive": "https://www.utilitydive.com/",
    "podcast-ai-alchemy": "https://www.ximalaya.com/album/74194808/",
    "jmlr": "https://jmlr.org/",
}

# 路径/文件名整段就是 rss|feed|atom|index.xml（可带 .xml/.rss/.atom 后缀）的，
# 只是格式标记，砍掉之后剩下的路径基本还是真实的栏目页/仓库页。
_FEED_SUFFIX_RE = re.compile(
    r"(?:/(?:rss|feed|atom|releases\.atom|index\.xml)(?:\.(?:xml|rss|atom))?/?|\.(?:rss|atom))$", re.I
)


def _homepage(source_id: str, endpoint: str) -> str:
    """把采集端点尽量还原成官网链接；不确定就宁可不给链接，不瞎猜。"""
    if source_id in _HOMEPAGE_OVERRIDES:
        return _HOMEPAGE_OVERRIDES[source_id]
    url = (endpoint or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return ""
    parsed = urlsplit(url)
    if parsed.netloc == "rss.arxiv.org":
        cat = parsed.path.rsplit("/", 1)[-1]
        return f"https://arxiv.org/list/{cat}/recent" if cat else "https://arxiv.org/"
    stripped = _FEED_SUFFIX_RE.sub("", parsed.path)
    if stripped != parsed.path:
        return urlunsplit((parsed.scheme, parsed.netloc, stripped or "/", "", ""))
    return url


def _stamp_text(ms: int) -> str:
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%Y-%m-%d %H:%M")


def _classify(
    *,
    status: str,
    runs: int,
    raw: int,
    written: int,
    cleaned: int,
    dry_days: int | None,
    fetch_error: str,
) -> str:
    """先分「跑不跑」，再分「跑得怎么样」。顺序即优先级，别调换。

    零产出的三种成因在这里分开，因为它们要求的动作完全不同：raw=0 是链路挂了，
    raw>0 但 cleaned>0 是末端去重/质量分吃掉的，raw>0 且 cleaned=0 才是自己的规则卡死。
    """
    if status != "active":
        return "paused"
    if runs == 0:
        return "never_run"
    if written > 0:
        return "effective" if (dry_days is not None and dry_days <= DRY_LIMIT_DAYS) else "degraded"
    if cleaned > 0:
        return "post_filter"  # 清洗全部通过，死在末端：跨轮去重或富集后打分
    if raw > 0:
        return "rule_blocked"
    if fetch_error:
        return "fetch_broken"
    return "dry"


def build_rows(
    records: list[dict[str, Any]],
    health_rows: list[dict[str, Any]],
    item_counts: dict[str, int],
    item_days: dict[str, int],
    brief_counts: dict[str, int],
    brief_last_dates: dict[str, str],
    *,
    days: int,
) -> list[dict[str, Any]]:
    """把四份数据合成一行一个源。health 里出现过但配置里没有的源也保留。"""
    by_source: dict[str, dict[str, Any]] = {}
    for row in health_rows:
        sid = str(row.get("source_id") or "").strip()
        if not sid:
            continue
        agg = by_source.setdefault(
            sid,
            {
                "runs": 0,
                "raw": 0,
                "written": 0,
                "cleaned": 0,
                "dedup_dropped": 0,
                "funnel": {},
                "fetch_errors": {},
                "engines": {},
                "last_run_dt": "",
                "last_written_dt": "",
                "run_days": set(),
                "written_days": set(),
                "observed_days": [],
                "list_chars": [],
                "fetch_totals": {},
                "fetch_rounds": {},
            },
        )
        agg["runs"] += 1
        agg["raw"] += _int((row.get("funnel") or {}).get("raw"))
        agg["written"] += _int(row.get("written"))
        agg["cleaned"] += _int(row.get("cleaned"))
        agg["dedup_dropped"] += _int(row.get("dedup_dropped"))
        for stage, value in (row.get("funnel") or {}).items():
            agg["funnel"][stage] = agg["funnel"].get(stage, 0) + _int(value)
        fetch = row.get("fetch") or {}
        error = str(fetch.get("error") or "").strip()
        if error:
            agg["fetch_errors"][error] = agg["fetch_errors"].get(error, 0) + 1
        # 抓取质量按 token 累加：链路上每一步的绝对量比一个错误名更能定位问题
        for token in ("entries", "list_chars", "links", "article_ok", "article_fail"):
            if fetch.get(token) is not None:
                agg["fetch_totals"][token] = agg["fetch_totals"].get(token, 0) + _int(fetch.get(token))
                agg["fetch_rounds"][token] = agg["fetch_rounds"].get(token, 0) + 1
        engine = str(fetch.get("engine") or "").strip()
        if engine:
            agg["engines"][engine] = agg["engines"].get(engine, 0) + 1
        dt = str(row.get("dt") or "")
        if dt:
            agg["run_days"].add(dt)
            agg["observed_days"].append(dt)
            if dt > agg["last_run_dt"]:
                agg["last_run_dt"] = dt
            if _int(row.get("written")) and dt > agg["last_written_dt"]:
                agg["last_written_dt"] = dt
                agg["written_days"].add(dt)

    name_to_id: dict[str, str] = {}
    configured: list[dict[str, Any]] = []
    for rec in records:
        fields = rec.get("fields") or {}
        sid = _cell(fields.get("source_id"))
        if not sid:
            continue
        name = _cell(fields.get("name")) or sid
        name_to_id.setdefault(name, sid)
        configured.append({"record": rec, "fields": fields, "sid": sid, "name": name})

    # 条目表和飞书只靠显示名对上，源改过名就会整段丢失。宽松键只做兜底，
    # 精确命中过的名字先剔除，避免两个源争抢同一批条目。
    claimed = {name for name in item_counts if name in name_to_id}
    fuzzy_items: dict[str, int] = {}
    fuzzy_days: dict[str, int] = {}
    for name, count in item_counts.items():
        if name in claimed:
            continue
        key = _name_key(name)
        if key:
            fuzzy_items[key] = fuzzy_items.get(key, 0) + count
            fuzzy_days[key] = max(fuzzy_days.get(key, 0), item_days.get(name, 0))

    today = datetime.now(CN_TZ).date()
    total_items = sum(item_counts.values()) or 0
    rows: list[dict[str, Any]] = []

    for entry in configured:
        fields, sid, name = entry["fields"], entry["sid"], entry["name"]
        agg = by_source.pop(sid, None) or {}
        written = int(agg.get("written") or 0)
        last_written = str(agg.get("last_written_dt") or "")
        if last_written:
            dry_days: int | None = (today - datetime.strptime(last_written, "%Y-%m-%d").date()).days
        else:
            dry_days = None
        funnel = agg.get("funnel") or {}
        fetch_errors = agg.get("fetch_errors") or {}
        top_error = max(fetch_errors, key=lambda k: fetch_errors[k]) if fetch_errors else ""
        status = _cell(fields.get("status")).lower() or "paused"
        if status not in STATUS_LABELS:
            status = "paused"
        writes_per_run = round(written / agg["runs"], 2) if agg.get("runs") else 0.0
        cleaned = int(agg.get("cleaned") or 0)
        outcome = _classify(
            status=status,
            runs=int(agg.get("runs") or 0),
            raw=int(agg.get("raw") or 0),
            written=written,
            cleaned=cleaned,
            dry_days=dry_days,
            fetch_error=top_error,
        )
        locals_items = item_counts.get(name, 0)
        locals_days = item_days.get(name, 0)
        renamed = False
        if not locals_items:
            key = _name_key(name)
            if fuzzy_items.get(key):
                locals_items, locals_days, renamed = fuzzy_items[key], fuzzy_days.get(key, 0), True
        dimension = _cell(fields.get("dimension"))
        source_format = _cell(fields.get("来源类型"))
        priority = _cell(fields.get("priority"))
        board = _brief_board(dimension, source_format)
        rows.append(
            {
                "sourceId": sid,
                "name": name,
                "status": status,
                "statusLabel": STATUS_LABELS[status],
                "outcome": outcome,
                "outcomeLabel": OUTCOME_LABELS[outcome],
                "recordId": str(entry["record"].get("record_id") or ""),
                "dimension": dimension,
                "format": source_format,
                "priority": priority,
                "briefBoard": board,
                "priorityRule": _priority_rule(board, priority),
                "fetchMethod": _cell(fields.get("fetch_method")),
                "endpoint": _cell(fields.get("endpoint")),
                "homepage": _homepage(sid, _cell(fields.get("endpoint"))),
                "lookback": _cell(fields.get("lookback_window")),
                "keywordRegex": _cell(fields.get("keyword_regex")),
                "minContentChars": _int(fields.get("min_content_chars")),
                "notes": _cell(fields.get("notes")),
                # 跑批统计（来自 output/health 分源漏斗）
                "runs": int(agg.get("runs") or 0),
                "observedDays": len(agg.get("run_days") or ()),
                "raw": int(agg.get("raw") or 0),
                "written": written,
                "cleaned": cleaned,
                # 清洗全过却被末端吃掉的量。>0 说明问题不在抓取和筛选规则，在去重或富集打分。
                "postFilterDrop": max(0, cleaned - written),
                "dedupDropped": int(agg.get("dedup_dropped") or 0),
                "keepRate": round(written / agg["raw"] * 100, 1) if agg.get("raw") else 0.0,
                "writesPerRun": writes_per_run,
                "dryDays": dry_days,
                "lastRunDt": str(agg.get("last_run_dt") or ""),
                "lastWrittenDt": last_written,
                "funnel": {k: int(v) for k, v in funnel.items()},
                "blockedAt": _top_block(funnel, written),
                "fetchErrors": fetch_errors,
                "topFetchError": top_error,
                "topFetchErrorLabel": FETCH_ERROR_LABELS.get(top_error, top_error),
                "engine": (max(agg["engines"], key=lambda k: agg["engines"][k]) if agg.get("engines") else ""),
                "fetchTotals": {k: int(v) for k, v in (agg.get("fetch_totals") or {}).items()},
                "entriesPerRun": round(
                    (agg.get("fetch_totals") or {}).get("entries", 0) / max(1, (agg.get("fetch_rounds") or {}).get("entries", 1)),
                    1,
                )
                if (agg.get("fetch_totals") or {}).get("entries")
                else None,
                # 本机落盘条目（历史贡献口径）
                "historyItems": locals_items,
                "historyDays": locals_days,
                "historyRenamed": renamed,
                "sharePct": round(locals_items / total_items * 100, 2) if total_items else 0.0,
                "briefCount": brief_counts.get(sid, 0),
                "briefLastDate": brief_last_dates.get(sid, ""),
                # 飞书回写快照（只代表最近一轮）
                "paramLastMs": _int(fields.get("最近采集时间")),
                "paramLast": _stamp_text(_int(fields.get("最近采集时间"))),
                "paramPerDay": _int(fields.get("条目数")),
                "paramDedup": _int(fields.get("查重过滤")),
                "paramWindow": _int(fields.get("时间窗过滤")),
                "signalScoreDrop": int(funnel.get("min_signal_score") or 0)
                + int(funnel.get("min_quality_score") or 0),
            }
        )

    # 健康日志里有、配置里没有的历史遗留源：展示出来，别让它们凭空消失。
    name_to_id_rev = {v: k for k, v in name_to_id.items()}
    for sid, agg in sorted(by_source.items()):
        written = int(agg.get("written") or 0)
        rows.append(
            {
                "sourceId": sid,
                "name": name_to_id_rev.get(sid, sid),
                "status": "missing",
                "statusLabel": "不在参数表",
                "outcome": "dry" if not written else "degraded",
                "outcomeLabel": "已从参数表移除" if not written else "已从参数表移除（曾入库）",
                "recordId": "",
                "dimension": "",
                "format": "",
                "priority": "",
                "briefBoard": "未知",
                "priorityRule": "未知（已从参数表移除）",
                "fetchMethod": "",
                "endpoint": "",
                "homepage": "",
                "lookback": "",
                "keywordRegex": "",
                "minContentChars": 0,
                "notes": "健康日志里有采集记录，但当前参数表已无此源",
                "runs": int(agg.get("runs") or 0),
                "observedDays": len(agg.get("run_days") or ()),
                "raw": int(agg.get("raw") or 0),
                "written": written,
                "cleaned": int(agg.get("cleaned") or 0),
                "postFilterDrop": max(0, int(agg.get("cleaned") or 0) - written),
                "dedupDropped": int(agg.get("dedup_dropped") or 0),
                "keepRate": round(written / agg["raw"] * 100, 1) if agg.get("raw") else 0.0,
                "writesPerRun": 0.0,
                "dryDays": None,
                "lastRunDt": str(agg.get("last_run_dt") or ""),
                "lastWrittenDt": str(agg.get("last_written_dt") or ""),
                "funnel": {k: int(v) for k, v in (agg.get("funnel") or {}).items()},
                "blockedAt": _top_block(agg.get("funnel") or {}, written),
                "fetchErrors": agg.get("fetch_errors") or {},
                "topFetchError": max(agg["fetch_errors"], key=lambda k: agg["fetch_errors"][k])
                if agg.get("fetch_errors")
                else "",
                "topFetchErrorLabel": "",
                "engine": max(agg["engines"], key=lambda k: agg["engines"][k]) if agg.get("engines") else "",
                "fetchTotals": {k: int(v) for k, v in (agg.get("fetch_totals") or {}).items()},
                "entriesPerRun": None,
                "historyItems": 0,
                "historyDays": 0,
                "historyRenamed": False,
                "sharePct": 0.0,
                "briefCount": 0,
                "briefLastDate": "",
                "paramLastMs": 0,
                "paramLast": "",
                "paramPerDay": 0,
                "paramDedup": 0,
                "paramWindow": 0,
                "signalScoreDrop": 0,
            }
        )

    rows.sort(
        key=lambda r: (
            OUTCOME_ORDER.index(r["outcome"]) if r["outcome"] in OUTCOME_ORDER else 9,
            -(r["historyItems"] or 0),
            -(r["written"] or 0),
            r["name"],
        )
    )
    return rows


def _top_block(funnel: dict[str, Any], written: int) -> str:
    """零产出时指出淘汰最多的一级；有产出则该字段无意义，留空。"""
    if written or not _int(funnel.get("raw")):
        return ""
    drops = {
        stage: _int(value)
        for stage, value in funnel.items()
        if stage not in {"raw", "kept"} and _int(value) > 0
    }
    return max(drops, key=lambda k: drops[k]) if drops else ""


def summarize(
    rows: list[dict[str, Any]], *, days: int, source: str, span: str, item_total: int = 0
) -> dict[str, Any]:
    tally: dict[str, int] = {key: 0 for key in OUTCOME_ORDER}
    for row in rows:
        tally[row["outcome"]] = tally.get(row["outcome"], 0) + 1
    total_items = sum(r["historyItems"] for r in rows)
    contributors = [r for r in rows if r["historyItems"] > 0]
    return {
        "generatedAt": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        "configSource": source,
        "healthDays": days,
        "itemSpan": span,
        "totalSources": len(rows),
        "tally": tally,
        "configuredActive": sum(1 for r in rows if r["status"] == "active"),
        "totalRaw": sum(r["raw"] for r in rows),
        "totalWritten": sum(r["written"] for r in rows),
        "historyItems": total_items,
        # 条目表只存显示名，源改名或中途下线就会有一批条目归不到任何现行源上。
        # 这个残差必须显式报出来，否则「各源之和」与「本地条目总数」对不上时无人察觉。
        "unattributedItems": max(0, item_total - total_items),
        "itemTotal": item_total,
        "contributingSources": len(contributors),
        # 头部 N 个源占了多少：贡献高度集中时，一个源挂掉就是一半产能没了
        "topShare": round(
            sum(sorted((r["historyItems"] for r in rows), reverse=True)[:5]) / total_items * 100, 1
        )
        if total_items
        else 0.0,
        "rulesDrop": sum(v for r in rows for k, v in r["funnel"].items() if k not in {"raw", "kept"}),
    }


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------
CSV_COLUMNS = (
    ("sourceId", "source_id"),
    ("name", "名称"),
    ("status", "状态"),
    ("outcomeLabel", "结论"),
    ("dimension", "分类"),
    ("fetchMethod", "采集方式"),
    ("lookback", "时间窗"),
    ("visibility", "可见性"),
    ("lastWrittenDt", "最近入库"),
    ("dryDays", "断流天数"),
    ("runs", "采集轮次"),
    ("observedDays", "覆盖天数"),
    ("raw", "抓到原始"),
    ("written", "入库"),
    ("keepRate", "留存率%"),
    ("postFilterDrop", "末端损耗"),
    ("entriesPerRun", "每轮抓取"),
    ("historyItems", "历史条目"),
    ("sharePct", "贡献占比%"),
    ("briefCount", "近8期入选"),
    ("briefLastDate", "最近入选"),
    ("blockedAt", "卡点"),
    ("topFetchErrorLabel", "抓取错误"),
    ("engine", "抓取引擎"),
)


def write_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in CSV_COLUMNS])
        for row in rows:
            writer.writerow([_view(row, key) for key, _ in CSV_COLUMNS])
    return path


def _view(row: dict[str, Any], key: str) -> Any:
    """导出与看板共用的字段视图：派生字段在这里算，别在两处各写一遍。"""
    if key == "visibility":
        if row["status"] == "missing":
            return "已下线"
        if row["status"] != "active":
            return "不参与"
        return "未采集" if not row["runs"] else "已采集"
    if key == "blockedAt":
        stage = row.get("blockedAt") or ""
        return f"{STAGE_LABELS.get(stage, stage)}（{STAGE_ACTION.get(stage, '其他')}）" if stage else ""
    return row.get(key, "")


def board_payload(
    rows: list[dict[str, Any]], stats: dict[str, Any]
) -> dict[str, Any]:
    return {
        "stats": stats,
        "stageLabels": STAGE_LABELS,
        "stageAction": STAGE_ACTION,
        "outcomeLabels": OUTCOME_LABELS,
        "outcomeHelp": OUTCOME_HELP,
        "outcomeOrder": list(OUTCOME_ORDER),
        "dryLimitDays": DRY_LIMIT_DAYS,
        "rows": [{**row, "visibility": _view(row, "visibility"), "blockedAtLabel": _view(row, "blockedAt")} for row in rows],
    }


def write_html(rows: list[dict[str, Any]], stats: dict[str, Any], path: Path) -> Path:
    payload = json.dumps(board_payload(rows, stats), ensure_ascii=False)
    # (筛选键, 数值, 标题, 说明)。筛选键空＝不筛（点总数看全部）。
    tiles: list[tuple[str, Any, str, str]] = [
        ("", stats["totalSources"], "源总数", "参数表与健康日志的并集"),
        ("active", stats["configuredActive"], "已接入", "status=active，正式流水线会跑"),
        ("effective", stats["tally"]["effective"], "有效", f"近 {DRY_LIMIT_DAYS} 天有入库"),
        ("degraded", stats["tally"]["degraded"], "低产", "能入库但已断流或产出偏低"),
        ("rule_blocked", stats["tally"]["rule_blocked"], "被规则卡死", "抓到了，被自己的规则筛光"),
        ("post_filter", stats["tally"]["post_filter"], "过清洗没入库", "死在末端去重或富集打分"),
        ("fetch_broken", stats["tally"]["fetch_broken"], "抓取失败", "一条原始条目都没抓到"),
        ("dry", stats["tally"]["dry"], "静默无产出", "链路正常但观测期内零入库"),
        ("never_run", stats["tally"]["never_run"], "从未采集", "观测期内没有任何一轮跑过它"),
        ("paused", stats["tally"]["paused"], "已暂停", "status 非 active"),
    ]
    missing = sum(1 for r in rows if r["status"] == "missing")
    if missing:
        tiles.append(("missing", missing, "已下线", "健康日志有记录、参数表已无"))
    tile_html = "".join(
        f'<button class="tile" data-outcome="{html.escape(key)}" aria-pressed="false">'
        f'<span class="tile-n">{value}</span>'
        f'<span class="tile-k">{html.escape(label)}</span>'
        f'<span class="tile-h">{html.escape(note)}</span></button>'
        for key, value, label, note in tiles
    )
    doc = TEMPLATE.replace("__PAYLOAD__", payload).replace("__TILES__", tile_html).replace(
        "__GENERATED__", html.escape(str(stats.get("generatedAt") or ""))
    ).replace("__CONFIG_SOURCE__", html.escape(str(stats.get("configSource") or "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 模板：单文件、零依赖、列可拖动
# --------------------------------------------------------------------------
TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>信号源状态看板</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#fff; --line:#e3e6ea; --line-strong:#c9ced6;
    --text:#1a1d21; --muted:#6b7280; --accent:#2563eb;
    --ok:#12805c; --ok-bg:#e6f6ef; --warn:#a16207; --warn-bg:#fdf3e0;
    --bad:#b42318; --bad-bg:#fdeceb; --idle:#4b5563; --idle-bg:#eef0f3;
    --pin-h:0px;      /* 分类行 + 筛选条的总高，表头贴在它下面 */
    --pin-tiles:0px;  /* 只到分类行底部，筛选条贴在这里 */
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:13px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
  header{padding:18px 20px 12px}
  h1{margin:0 0 4px;font-size:19px;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12px}
  .sub code{background:#eceff3;padding:1px 5px;border-radius:4px}
  /* 分类行与列名行都吸顶：往下翻 116 行时仍然看得见当前口径和列含义。
     --pin-h 由脚本实测分类行高度后写入，列名行贴在它下面。阴影是常驻的——
     判断“是否已贴住”需要监听滚动，在部分宿主环境里不可靠，不值得为一点视觉细节冒险。 */
  .tiles{display:flex;flex-wrap:wrap;gap:8px;padding:12px 20px 4px;
    position:sticky;top:0;z-index:3;background:var(--bg)}
  .tiles::after{content:"";position:absolute;left:0;right:0;bottom:-10px;height:10px;
    background:repeating-linear-gradient(var(--bg) 0 calc(100% - 1px),var(--line) calc(100% - 1px) 100%);
    box-shadow:0 6px 10px -8px rgba(15,23,42,.32);pointer-events:none}
  .tile{display:flex;flex-direction:column;gap:2px;align-items:flex-start;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:9px 13px;min-width:118px;cursor:pointer;text-align:left;font:inherit;color:inherit}
  .tile:hover{border-color:var(--line-strong)}
  .tile[aria-pressed="true"]{border-color:var(--accent);box-shadow:0 0 0 2px #dbe6fd}
  .tile-n{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
  .tile-k{font-size:12px;font-weight:600}
  .tile-h{font-size:11px;color:var(--muted)}
  /* 筛选条跟在分类行下面一起吸顶：两者是同一套“当前口径”，分开吸顶只会互相遮挡 */
  .toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 20px;
    position:sticky;top:var(--pin-tiles,0px);z-index:2;background:var(--bg);
    box-shadow:0 6px 10px -8px rgba(15,23,42,.28)}
  .toolbar::after{content:"";position:absolute;left:0;right:0;bottom:-10px;height:10px;
    background:var(--bg);pointer-events:none}
  .toolbar input[type=search],.toolbar select{font:inherit;padding:6px 9px;border:1px solid var(--line-strong);
    border-radius:7px;background:var(--panel);color:inherit}
  .toolbar input[type=search]{min-width:230px}
  .toolbar .spacer{flex:1}
  button.act{font:inherit;padding:6px 11px;border:1px solid var(--line-strong);border-radius:7px;
    background:var(--panel);cursor:pointer}
  button.act:hover{border-color:var(--accent);color:var(--accent)}
  .hint{padding:0 20px 8px;color:var(--muted);font-size:12px}
  .wrap{padding:0 20px 40px}
  /* 不能给 table 加 overflow:hidden 或 border——前者会让 thead 的 sticky 失效，
     后者在表头吸顶脱离表格盒时会被切成两段。外框与圆角交给 .frame。 */
  table{border-collapse:separate;border-spacing:0;width:100%;background:var(--panel);
    border-radius:0 0 10px 10px}
  .frame{background:var(--panel);border:1px solid var(--line);border-radius:10px}
  thead th{position:sticky;top:var(--pin-h,0px);z-index:2;background:#eef1f5;
    border-bottom:1px solid var(--line);
    text-align:left;padding:0;white-space:nowrap;user-select:none;
    box-shadow:0 6px 10px -8px rgba(15,23,42,.35)}
  thead th:first-child{border-top-left-radius:10px}
  thead th:last-child{border-top-right-radius:10px}
  .th{display:flex;align-items:center;gap:5px;padding:7px 9px;cursor:pointer}
  .th .grip{color:#b3bac4;cursor:grab;font-size:11px;letter-spacing:-1px}
  th.dragging{opacity:.45}
  th.drop-before{box-shadow:inset 2px 0 0 var(--accent)}
  th.drop-after{box-shadow:inset -2px 0 0 var(--accent)}
  th.fixed .th{cursor:default}
  .sortmark{color:var(--accent);font-size:10px}
  tbody td{padding:7px 9px;border-bottom:1px solid #f0f2f5;vertical-align:top;white-space:nowrap}
  tbody tr:hover td{background:#fafbfc}
  tbody tr.expandable{cursor:pointer}
  .num{text-align:right;font-variant-numeric:tabular-nums}
  .name{font-weight:600;max-width:260px;overflow:hidden;text-overflow:ellipsis}
  .name-link{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line-strong)}
  .name-link:hover{color:var(--accent);border-bottom-color:var(--accent)}
  .sid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
  .muted{color:var(--muted)}
  .notes-input{display:block;width:100%;min-width:180px;box-sizing:border-box;resize:none;overflow:hidden;
    font:inherit;font-size:12px;line-height:1.45;color:var(--text);background:transparent;
    border:1px solid transparent;border-radius:6px;padding:4px 6px}
  .notes-input:hover{border-color:var(--line)}
  .notes-input:focus{outline:none;border-color:var(--accent);background:#fff;
    box-shadow:0 0 0 2px rgba(37,99,235,.12)}
  .pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap}
  .p-effective{background:var(--ok-bg);color:var(--ok)}
  .p-degraded{background:var(--warn-bg);color:var(--warn)}
  .p-rule_blocked{background:#eef2ff;color:#4338ca}
  .p-post_filter{background:#fff7ed;color:#c2410c}
  .p-fetch_broken{background:var(--bad-bg);color:var(--bad)}
  .p-dry{background:var(--idle-bg);color:var(--idle)}
  .p-never_run{background:#f3e8ff;color:#7e22ce}
  .p-paused{background:#f1f3f5;color:#868e96}
  .trend{color:var(--ok)}.trend.down{color:var(--bad)}
  .detail td{background:#fbfcfd;white-space:normal}
  .detail h4{margin:2px 0 6px;font-size:12px;color:var(--muted);font-weight:600}
  .funnel{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
  .fstep{border:1px solid var(--line);border-radius:7px;padding:5px 9px;background:#fff;font-size:12px}
  .fstep b{font-variant-numeric:tabular-nums}
  .fstep.kept{border-color:#bfe3d2;background:var(--ok-bg)}
  .fstep.drop{border-color:#f2dede;background:#fdf6f5}
  .kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:4px 16px;font-size:12px}
  .kv div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .kv span{color:var(--muted)}
  dialog{border:1px solid var(--line);border-radius:12px;padding:0;max-width:760px;width:92vw}
  dialog::backdrop{background:rgba(15,23,42,.35)}
  .dlg-head{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;
    border-bottom:1px solid var(--line);font-weight:600}
  .cols{padding:12px 16px 18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:6px}
  .cols label{display:flex;align-items:center;gap:7px;padding:5px 7px;border:1px solid var(--line);
    border-radius:7px;background:#fff;cursor:pointer}
  .cols label:hover{border-color:var(--line-strong)}
  footer{padding:0 20px 30px;color:var(--muted);font-size:12px}
  footer b{color:var(--text)}
</style>
</head>
<body>
<header>
  <h1>信号源状态看板</h1>
  <div class="sub">
    生成于 __GENERATED__ · 配置来源 <code>__CONFIG_SOURCE__</code> ·
    拖动表头 <b>⠿</b> 可调整列顺序，点击表头排序，点击任意行展开该源的漏斗与字段
  </div>
</header>
<div class="tiles" id="tiles">__TILES__</div>
<div class="toolbar">
  <input type="search" id="q" placeholder="搜索名称 / ID / 分类 / 备注…">
  <select id="f-status"><option value="">全部状态</option></select>
  <select id="f-method"><option value="">全部采集方式</option></select>
  <select id="f-dim"><option value="">全部分类</option></select>
  <select id="f-board"><option value="">全部日报板块</option></select>
  <select id="f-health">
    <option value="">全部结论</option>
    <option value="problem">只看有问题的</option>
    <option value="silent">只看零产出</option>
    <option value="contrib">只看有历史贡献的</option>
  </select>
  <span class="spacer"></span>
  <button class="act" id="btn-cols">列管理</button>
  <button class="act" id="btn-csv">导出当前视图 CSV</button>
  <button class="act" id="btn-reset">恢复默认列</button>
</div>
<div class="hint" id="hint"></div>
<div class="wrap">
  <div class="frame">
    <table id="tbl">
      <thead><tr id="head"></tr></thead>
      <tbody id="body"></tbody>
    </table>
  </div>
</div>
<footer>
  <div><b>被规则卡死</b>＝抓到过原始条目但一条没留下（多是 keyword_regex / 时间窗 / 质量分）；<b>过清洗没入库</b>＝清洗全过，死在末端去重或富集打分；<b>抓取失败</b>＝连原始条目都没抓到；<b>从未采集</b>＝参数表里有它但观测期内没有任何一轮跑过它。四种都显示 0 条，处理方式完全不同。</div>
  <div style="margin-top:4px">「历史条目」来自本机 <code>data/tagged|items</code> 落盘条目，「入库」来自 <code>output/health</code> 逐轮漏斗，口径不同不可直接相减。<span id="foot-note"></span></div>
  <div style="margin-top:4px" id="residual"></div>
</footer>

<dialog id="dlg">
  <div class="dlg-head"><span>显示列</span><button class="act" id="dlg-close">关闭</button></div>
  <div class="cols" id="col-list"></div>
</dialog>

<script id="board-data" type="application/json">__PAYLOAD__</script>
<script>
(function(){
  const DATA = JSON.parse(document.getElementById('board-data').textContent);
  const ROWS = DATA.rows, STATS = DATA.stats;
  const LS_COLS = 'source-board.columns.v1';

  const fmt = (v) => (v === null || v === undefined || v === '') ? '—' : v;
  const num = (v) => (v ? Number(v).toLocaleString('zh-CN') : '0');

  // 每一列自带取值、对齐、排序键与说明。拖动的顺序就是这个数组的顺序。
  const COLUMNS = [
    {key:'name', label:'源', width:230, render:r=>{
      const label = `<span class="name-text">${esc(r.name)}</span>`;
      if (!r.homepage) return `<div class="name" title="${esc(r.name)}">${label}</div>`;
      // stopPropagation：这一格外层是整行的展开点击区，链接不拦下会顺带把详情行也翻开
      return `<div class="name" title="${esc(r.name)}">`
        + `<a class="name-link" href="${esc(r.homepage)}" target="_blank" rel="noopener noreferrer" `
        + `onclick="event.stopPropagation()" title="官网：${esc(r.homepage)}">${label}</a></div>`;
    }},
    {key:'outcome', label:'结论', width:104, render:r=>`<span class="pill p-${r.outcome}">${esc(r.outcomeLabel)}</span>`,
      sort:r=>DATA.outcomeOrder.indexOf(r.outcome), text:r=>r.outcomeLabel},
    {key:'sourceId', label:'source_id', width:170, render:r=>`<span class="sid">${esc(r.sourceId)}</span>`, text:r=>r.sourceId},
    {key:'statusLabel', label:'状态', width:78, render:r=>`<span class="muted">${esc(r.statusLabel)}</span>`, text:r=>r.statusLabel},
    {key:'visibility', label:'可见性', width:80,
      text:r=>r.visibility,
      note:'已采集 / 未采集（active 但一轮没跑过）/ 不参与（非 active）/ 已下线（参数表已无）'},
    {key:'historyItems', label:'历史条目', width:88, align:'num',
      render:r=>`<b>${num(r.historyItems)}</b>`, sort:r=>r.historyItems,
      note:'本机 data/tagged（或 items）落盘条目数，即历史上真正产出的量'},
    {key:'sharePct', label:'贡献占比', width:82, align:'num',
      render:r=>r.sharePct ? r.sharePct.toFixed(2)+'%' : '<span class="muted">0%</span>', sort:r=>r.sharePct,
      note:'该源历史条目 / 全部源历史条目'},
    {key:'briefCount', label:'近8期入选', width:82, align:'num', render:r=>num(r.briefCount), sort:r=>r.briefCount,
      note:'最近 8 期简报的累计入选次数，不代表今天入选'},
    {key:'briefLastDate', label:'最近入选', width:94, text:r=>r.briefLastDate || '—',
      note:'该源最近一次被选入日报的日期'},
    {key:'written', label:'入库(观测期)', width:96, align:'num', render:r=>num(r.written), sort:r=>r.written,
      note:'output/health 逐轮漏斗累计的 final 写入量'},
    {key:'raw', label:'抓到原始', width:86, align:'num', render:r=>num(r.raw), sort:r=>r.raw},
    {key:'keepRate', label:'留存率', width:74, align:'num',
      render:r=>r.raw ? r.keepRate.toFixed(1)+'%' : '<span class="muted">—</span>', sort:r=>r.keepRate,
      note:'入库 / 抓到原始。极低＝规则过严，为零且抓到过＝被卡死'},
    {key:'postFilterDrop', label:'末端损耗', width:84, align:'num', render:r=>num(r.postFilterDrop), sort:r=>r.postFilterDrop,
      note:'清洗通过却被末端吃掉的量（跨轮去重 + 富集后质量分）。有值说明问题不在抓取和筛选规则'},
    {key:'entriesPerRun', label:'每轮抓取', width:82, align:'num',
      render:r=>r.entriesPerRun === null ? '<span class="muted">—</span>' : r.entriesPerRun, sort:r=>r.entriesPerRun || 0,
      note:'抓取阶段每轮平均拿到的条目数，来自 fetch 统计：能区分「列表拿到但全文失败」和「列表就是空的」'},
    {key:'runs', label:'采集轮次', width:76, align:'num', render:r=>num(r.runs), sort:r=>r.runs,
      note:'观测期内被点名采集的次数。0＝从未跑过，而不是跑失败'},
    {key:'observedDays', label:'覆盖天数', width:76, align:'num', render:r=>num(r.observedDays), sort:r=>r.observedDays},
    {key:'dryDays', label:'断流天数', width:78, align:'num',
      render:r=>r.dryDays === null ? '<span class="muted">从未入库</span>' : r.dryDays + '天',
      sort:r=>r.dryDays === null ? 99999 : r.dryDays,
      note:'距该源最近一次入库的天数；7 天以内算还在产出'},
    {key:'lastWrittenDt', label:'最近入库', width:96, render:r=>fmt(r.lastWrittenDt), text:r=>r.lastWrittenDt},
    {key:'lastRunDt', label:'最近采集', width:96, render:r=>fmt(r.lastRunDt || r.paramLast), text:r=>r.lastRunDt},
    {key:'blockedAtLabel', label:'卡在哪一步', width:150,
      render:r=>r.blockedAt ? `<span class="trend down">${esc(r.blockedAtLabel)}</span>` : '<span class="muted">—</span>',
      sort:r=>r.blockedAt, text:r=>r.blockedAtLabel},
    {key:'signalScoreDrop', label:'信号分淘汰', width:92, align:'num', render:r=>num(r.signalScoreDrop), sort:r=>r.signalScoreDrop,
      note:'本地信号分 + 富集后质量分两项淘汰之和，这两项通常是最难发现的一整源归零原因'},
    {key:'topFetchErrorLabel', label:'抓取错误', width:170,
      render:r=>r.topFetchErrorLabel ? `<span class="trend down">${esc(r.topFetchErrorLabel)}</span>` : '<span class="muted">—</span>',
      text:r=>r.topFetchErrorLabel},
    {key:'engine', label:'抓取引擎', width:96, render:r=>fmt(r.engine), text:r=>r.engine},
    {key:'fetchMethod', label:'采集方式', width:84, text:r=>r.fetchMethod},
    {key:'dimension', label:'分类', width:120, text:r=>r.dimension},
    {key:'format', label:'来源类型', width:80, text:r=>r.format},
    {key:'briefBoard', label:'日报板块', width:92, text:r=>r.briefBoard,
      note:'技术开源、视频、播客、社媒均独立于新闻日报名额'},
    {key:'priorityRule', label:'优先级规则', width:136,
      render:r=>r.briefBoard === '新闻' ? `<span class="muted">${esc(r.priorityRule)}</span>` : `<span class="pill p-rule_blocked">${esc(r.priorityRule)}</span>`,
      text:r=>r.priorityRule,
      note:'独立板块不使用 P0/P1/P2；技术开源按质量分与发布时间筛选排序'},
    {key:'priority', label:'配置优先级', width:82, text:r=>r.priority,
      note:'保留在参数表的旧配置；独立板块不读取该值'},
    {key:'lookback', label:'时间窗', width:70, text:r=>r.lookback},
    {key:'paramPerDay', label:'上轮条目数', width:88, align:'num', render:r=>num(r.paramPerDay), sort:r=>r.paramPerDay,
      note:'飞书一级参数表回写的最近一轮条目数（覆盖式快照，只代表最后一轮）'},
    {key:'paramWindow', label:'上轮窗过滤', width:92, align:'num', render:r=>num(r.paramWindow), sort:r=>r.paramWindow},
    {key:'paramDedup', label:'上轮查重过滤', width:100, align:'num', render:r=>num(r.paramDedup), sort:r=>r.paramDedup},
    {key:'notes', label:'备注', width:260, sort:r=>r.notes || '',
      render:r=>`<textarea class="notes-input" rows="1" data-sid="${esc(r.sourceId)}" `
        + `placeholder="点击输入…" title="我自己的备注，保存在本地浏览器，不写回飞书">${esc(r.notes || '')}</textarea>`,
      text:r=>r.notes || ''},
  ];
  const DEFAULT_VISIBLE = ['name','outcome','sourceId','statusLabel','visibility','historyItems','sharePct',
    'briefCount','briefLastDate','written','raw','keepRate','runs','dryDays','lastWrittenDt','blockedAtLabel','topFetchErrorLabel',
    'fetchMethod','dimension','briefBoard','priorityRule','notes'];
  const FIXED = new Set(['name']);   // 首列钉住，否则拖动时行标识会跑到视野外

  const clone = v => Array.isArray(v) ? v.slice() : (v && typeof v === 'object' ? Object.assign({}, v) : v);
  function load(key, fallback){
    try { const raw = localStorage.getItem(key); return raw ? JSON.parse(raw) : clone(fallback); }
    catch(e){ return clone(fallback); }
  }
  function save(){ try{ localStorage.setItem(LS_COLS, JSON.stringify(visible)); }catch(e){} }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  // 备注是看板自用的，不写回飞书，落在浏览器本地；key 用 source_id，
  // 源改名也不会把备注丢了。
  const LS_NOTES = 'source-board.notes.v1';
  const NOTES = load(LS_NOTES, {});
  ROWS.forEach(r => { r.notes = NOTES[r.sourceId] !== undefined ? NOTES[r.sourceId] : (r.notes || ''); });
  function saveNotes(){ try{ localStorage.setItem(LS_NOTES, JSON.stringify(NOTES)); }catch(e){} }

  let visible = load(LS_COLS, DEFAULT_VISIBLE).filter(k => COLUMNS.some(c => c.key === k));
  // 默认列全部消失时（比如本地存了一份错配置）退回默认，别渲染一张空表
  if (!visible.length) visible = DEFAULT_VISIBLE.slice();
  let sortKey = 'historyItems', sortDir = -1;
  let outcomeFilter = '', expanded = new Set();
  const colByKey = k => COLUMNS.find(c => c.key === k);

  // 分类行吸顶，列名行贴在它正下方。分类行高度随窗口宽度换行而变，只能实测。
  const tileBar = document.getElementById('tiles');
  const toolBar = document.querySelector('.toolbar');
  function syncPin(){
    // 视口还没量出来时（后台标签、打印预览、0 宽的 iframe）测到的行高是废的，直接跳过
    if (!window.innerHeight || !window.innerWidth) return;
    const tiles = Math.round(tileBar.getBoundingClientRect().height);
    const bar = Math.round(toolBar.getBoundingClientRect().height);
    const root = document.documentElement.style;
    root.setProperty('--pin-tiles', tiles + 'px');
    root.setProperty('--pin-h', (tiles + bar) + 'px');
  }
  syncPin();
  window.addEventListener('resize', syncPin);
  // 系统字体晚到时行高会变，吸顶偏移跟着量一次
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncPin);
  // 「列管理」弹窗遮罩会把 --pin-h 继承进 dialog，清掉免得表头在弹窗里也偏移
  document.getElementById('dlg').style.setProperty('--pin-h', '0px');

  function cellText(r, col){
    if (col.text) return col.text(r);
    const v = r[col.key];
    return (v === null || v === undefined) ? '' : String(v);
  }

  function renderHead(){
    const head = document.getElementById('head');
    head.innerHTML = '';
    visible.forEach((key, idx) => {
      const col = colByKey(key); if (!col) return;
      const th = document.createElement('th');
      th.dataset.key = key; th.dataset.idx = idx;
      if (idx === 0) th.classList.add('fixed');
      if (key === sortKey) th.classList.add('sorted');
      th.style.width = (col.width || 100) + 'px';
      th.innerHTML = `<div class="th">${idx === 0 ? '' : '<span class="grip" title="拖动调整列顺序">⠿</span>'}`
        + `<span>${esc(col.label)}</span>`
        + (key === sortKey ? `<span class="sortmark">${sortDir < 0 ? '▼' : '▲'}</span>` : '')
        + `</div>`;
      th.addEventListener('click', () => {
        if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = col.align === 'num' ? -1 : 1; }
        renderHead(); renderBody();
      });
      if (idx > 0) enableDrag(th);
      head.appendChild(th);
    });
  }

    function enableDrag(th){
    th.draggable = true;
    th.addEventListener('dragstart', e => {
      e.stopPropagation();
      dragKey = th.dataset.key;
      th.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', dragKey); } catch(err){}
    });
    th.addEventListener('dragend', () => {
      th.classList.remove('dragging');
      dragKey = null;
      clearMarks();
    });
    th.addEventListener('dragover', e => {
      if (!dragKey || dragKey === th.dataset.key) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const after = dropSide(th, e.clientX);
      clearMarks();
      th.classList.add(after ? 'drop-after' : 'drop-before');
    });
    th.addEventListener('dragleave', () => th.classList.remove('drop-before','drop-after'));
    th.addEventListener('drop', e => {
      e.preventDefault(); e.stopPropagation();
      const target = th.dataset.key;
      if (!dragKey || dragKey === target) return;
      const after = dropSide(th, e.clientX);
      const key = dragKey; dragKey = null;
      reorder(key, target, after);
    });
  }

  let dragKey = null;
  // 落点按指针在表头中线的左右决定，插到目标列前面还是后面
  function dropSide(th, clientX){
    const box = th.getBoundingClientRect();
    return (clientX - box.left) > box.width / 2;
  }
  function clearMarks(){ document.querySelectorAll('#head th').forEach(x => x.classList.remove('drop-before','drop-after')); }

  function reorder(key, target, after){
    const from = visible.indexOf(key); if (from < 0) return;
    visible.splice(from, 1);
    let to = visible.indexOf(target);
    if (to < 0) to = visible.length - 1;
    visible.splice(after ? to + 1 : to, 0, key);
    if (visible[0] !== 'name') { // 首列钉死：把 name 挪回最前
      visible.splice(visible.indexOf('name'), 1);
      visible.unshift('name');
    }
    save(); renderHead(); renderBody();
  }

  function matches(r){
    const q = document.getElementById('q').value.trim().toLowerCase();
    if (q){
      const hay = [r.name, r.sourceId, r.dimension, r.fetchMethod, r.notes, r.endpoint].join(' ').toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    if (outcomeFilter && r.outcome !== outcomeFilter && r.status !== outcomeFilter) return false;
    const f = (id) => document.getElementById(id).value;
    if (f('f-status') && r.status !== f('f-status')) return false;
    if (f('f-method') && r.fetchMethod !== f('f-method')) return false;
    if (f('f-dim') && r.dimension !== f('f-dim')) return false;
    if (f('f-board') && r.briefBoard !== f('f-board')) return false;
    if (f('f-health') === 'problem' && !['rule_blocked','post_filter','fetch_broken','dry','never_run','degraded'].includes(r.outcome)) return false;
    if (f('f-health') === 'silent' && (r.historyItems || r.written)) return false;
    if (f('f-health') === 'contrib' && !r.historyItems) return false;
    return true;
  }

  function sortRows(rows){
    const col = colByKey(sortKey) || {key: sortKey};
    const get = col.sort || (r => r[sortKey]);
    return rows.slice().sort((a, b) => {
      const va = get(a), vb = get(b);
      if (typeof va === 'number' || typeof vb === 'number'){
        const na = Number(va) || 0, nb = Number(vb) || 0;
        return (na - nb) * sortDir;
      }
      return String(va).localeCompare(String(vb), 'zh-CN') * sortDir;
    });
  }

  function renderBody(){
    const rows = sortRows(ROWS.filter(matches));
    const body = document.getElementById('body');
    body.innerHTML = '';
    rows.forEach(r => {
      const tr = document.createElement('tr');
      tr.className = 'expandable';
      visible.forEach((key, idx) => {
        const col = colByKey(key); if (!col) return;
        const td = document.createElement('td');
        if (col.align === 'num') td.className = 'num';
        td.innerHTML = col.render ? col.render(r) : esc(cellText(r, col));
        tr.appendChild(td);
      });
      tr.addEventListener('click', () => {
        const key = r.sourceId;
        if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
        renderBody();
      });
      body.appendChild(tr);
      if (expanded.has(r.sourceId)) body.appendChild(detailRow(r));
    });
    bindNotesInputs(body);
    document.getElementById('hint').textContent =
      `显示 ${rows.length} / ${ROWS.length} 个源 · 有效 ${STATS.tally.effective} · 被规则卡死 ${STATS.tally.rule_blocked} · 抓取失败 ${STATS.tally.fetch_broken} · 从未采集 ${STATS.tally.never_run} · 历史条目合计 ${num(STATS.historyItems)} 条，前 5 个源占 ${STATS.topShare}%`;
  }

  // 备注格：行数跟着内容长，输入即存，回车换行不触发整行展开
  function bindNotesInputs(scope){
    scope.querySelectorAll('textarea.notes-input').forEach(ta => {
      autoGrow(ta);
      ta.addEventListener('input', () => {
        const sid = ta.dataset.sid;
        NOTES[sid] = ta.value;
        const row = ROWS.find(r => r.sourceId === sid);
        if (row) row.notes = ta.value;
        saveNotes();
        autoGrow(ta);
      });
      // 行本身是展开热区，输入框里的操作（选词、换行、点光标）不该把详情行翻出来
      ta.addEventListener('click', e => e.stopPropagation());
      ta.addEventListener('keydown', e => { if (e.key === 'Enter') e.stopPropagation(); });
    });
  }
  function autoGrow(ta){
    ta.style.height = 'auto';
    ta.style.height = (ta.scrollHeight + 2) + 'px';
  }

  function detailRow(r){
    const tr = document.createElement('tr');
    tr.className = 'detail';
    const td = document.createElement('td');
    td.colSpan = visible.length;
    const order = ['raw','per_feed_cap','missing_title_url','title_exclude_regex','missing_or_invalid_date',
      'lookback','keyword_regex','keyword_include','keyword_exclude','min_signal_score','min_quality_score',
      'min_chars','min_content_chars','min_duration_sec','typed_filter','dup_round','kept'];
    const steps = order.filter(s => r.funnel[s] !== undefined)
      .map(s => `<div class="fstep ${s === 'kept' ? 'kept' : (s === 'raw' ? '' : 'drop')}">`
        + `${esc(DATA.stageLabels[s] || s)} <b>${num(r.funnel[s])}</b>`
        + (DATA.stageAction[s] ? ` <span class="muted">${esc(DATA.stageAction[s])}</span>` : '')
        + `</div>`).join('') || '<div class="fstep">观测期内没有漏斗记录</div>';
    const errs = Object.keys(r.fetchErrors || {}).length
      ? Object.entries(r.fetchErrors).map(([k, v]) => `${esc(k)} ×${v}`).join('；') : '';
    const kv = [
      ['source_id', r.sourceId], ['飞书 record_id', r.recordId || '—'],
      ['状态', r.statusLabel + '（' + r.status + '）'], ['结论', r.outcomeLabel],
      ['分类', r.dimension || '—'],
      ['日报板块 / 优先级规则', [r.briefBoard, r.priorityRule].filter(Boolean).join(' · ') || '—'],
      ['参数表配置优先级', r.priority || '—'],
      ['采集方式 / 引擎', [r.fetchMethod, r.engine].filter(Boolean).join(' · ') || '—'],
      ['时间窗', r.lookback || '—'], ['正文下限', r.minContentChars || '—'],
      ['采集端点', r.endpoint || '—'],
      ['官网', r.homepage || '—'],
      ['采集轮次 / 覆盖天数', r.runs + ' / ' + r.observedDays],
      ['抓到原始 / 入库', r.raw + ' / ' + r.written],
      ['清洗通过 / 跨轮去重掉', r.cleaned + ' / ' + r.dedupDropped],
      ['末端损耗（清洗过但没入库）', r.postFilterDrop],
      ['每轮抓取条目（fetch.entries）', r.entriesPerRun === null ? '—' : r.entriesPerRun],
      ['卡在哪一步', r.blockedAtLabel || '—'],
      ['抓取错误分布', errs || '—'],
      ['上轮回写（条目/查重/时间窗）', [r.paramPerDay, r.paramDedup, r.paramWindow].join(' / ')],
      ['历史条目 / 贡献占比', r.historyItems + ' 条 · ' + r.sharePct + '%' + (r.historyRenamed ? '（按改名后的名称匹配）' : '')],
      ['近 8 期入选 / 最近入选', r.briefCount + ' / ' + (r.briefLastDate || '—')],
      ['最近入库 / 最近采集', [r.lastWrittenDt || '—', r.lastRunDt || '—'].join(' · ')],
      ['备注', r.notes || '—'],
    ].map(([k, v]) => `<div><span>${esc(k)}：</span>${esc(v)}</div>`).join('');
    td.innerHTML = `<h4>清洗漏斗（观测期累计 · 括号内为归因）</h4><div class="funnel">${steps}</div>`
      + `<h4>字段</h4><div class="kv">${kv}</div>`;
    tr.appendChild(td);
    return tr;
  }

  function renderColumnDialog(){
    const list = document.getElementById('col-list');
    list.innerHTML = '';
    COLUMNS.forEach(col => {
      const label = document.createElement('label');
      label.innerHTML = `<input type="checkbox" ${visible.includes(col.key) ? 'checked' : ''} ${FIXED.has(col.key) ? 'disabled' : ''}>`
        + `<span>${esc(col.label)}${col.note ? `<br><span class="muted" style="font-size:11px">${esc(col.note)}</span>` : ''}</span>`;
      label.querySelector('input').addEventListener('change', e => {
        if (e.target.checked){ if (!visible.includes(col.key)) visible.push(col.key); }
        else visible = visible.filter(k => k !== col.key);
        save(); renderHead(); renderBody();
      });
      list.appendChild(label);
    });
  }

  function fillSelect(id, values){
    const sel = document.getElementById(id);
    values.filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).sort()
      .forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); });
  }

  function exportCSV(){
    const rows = sortRows(ROWS.filter(matches));
    const cols = visible.map(colByKey).filter(Boolean);
    const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
    const lines = [cols.map(c => q(c.label)).join(',')];
    rows.forEach(r => lines.push(cols.map(c => q(c.text ? c.text(r) : r[c.key])).join(',')));
    const blob = new Blob(['﻿' + lines.join('\n')], {type: 'text/csv;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'source-board-view.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // 顶部磁贴：点一下把结论/状态筛出来，再点取消
  document.getElementById('tiles').addEventListener('click', e => {
    const tile = e.target.closest('.tile'); if (!tile) return;
    const key = tile.dataset.outcome || '';
    outcomeFilter = (outcomeFilter === key) ? '' : key;
    document.querySelectorAll('.tile').forEach(t =>
      t.setAttribute('aria-pressed', String((t.dataset.outcome || '') === outcomeFilter)));
    renderBody();
  });

  ['q','f-status','f-method','f-dim','f-board','f-health'].forEach(id =>
    document.getElementById(id).addEventListener('input', renderBody));
  document.getElementById('btn-csv').addEventListener('click', exportCSV);
  document.getElementById('btn-cols').addEventListener('click', () => { renderColumnDialog(); document.getElementById('dlg').showModal(); });
  document.getElementById('dlg-close').addEventListener('click', () => document.getElementById('dlg').close());
  document.getElementById('btn-reset').addEventListener('click', () => {
    visible = DEFAULT_VISIBLE.slice();
    sortKey = 'historyItems'; sortDir = -1;
    save(); renderHead(); renderBody();
  });

  fillSelect('f-status', ROWS.map(r => r.status));
  fillSelect('f-method', ROWS.map(r => r.fetchMethod));
  fillSelect('f-dim', ROWS.map(r => r.dimension));
  fillSelect('f-board', ROWS.map(r => r.briefBoard));
  document.getElementById('foot-note').textContent = ' 配置来源：' + STATS.configSource + '。';
  // 归不到现行源的条目：源改名或中途下线时必然出现，报出来才不会被当成数据缺失
  document.getElementById('residual').textContent = STATS.unattributedItems
    ? `本地条目合计 ${num(STATS.itemTotal)} 条，其中 ${num(STATS.unattributedItems)} 条归不到现行源上：源改名或已从参数表移除（条目表只存显示名，不存 source_id）。`
    : `本地条目合计 ${num(STATS.itemTotal)} 条，全部归到了现行源上。`;
  renderHead();
  renderBody();
  syncPin();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build(*, days: int = 30, offline: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records, source = load_config(offline=offline)
    health_rows = load_health(days)
    item_counts, item_days, span_start, span_end = load_items()
    brief_counts, brief_last_dates, brief_files = load_brief_counts()
    rows = build_rows(records, health_rows, item_counts, item_days, brief_counts, brief_last_dates, days=days)
    stats = summarize(
        rows,
        days=days,
        source=source,
        span=f"{span_start} ~ {span_end}" if span_start else "无本地条目",
        item_total=sum(item_counts.values()),
    )
    stats["briefFiles"] = brief_files
    stats["healthRecords"] = len(health_rows)
    return rows, stats


def run(argv: list[str] | None = None) -> int:
    _env_from_dotenv()
    parser = argparse.ArgumentParser(description="生成信号源状态看板")
    parser.add_argument("--days", type=int, default=30, help="健康日志回看天数，默认 30")
    parser.add_argument("--out", default=str(DEFAULT_HTML), help="HTML 输出路径")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="CSV 输出路径，空字符串则不导出")
    parser.add_argument("--json", default="", help="额外导出看板 JSON 的路径")
    parser.add_argument("--offline", action="store_true", help="跳过飞书，直接用站点快照 / 种子")
    parser.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = parser.parse_args(argv)

    rows, stats = build(days=args.days, offline=args.offline)
    if not rows:
        print("没有任何源配置，先跑 python -m tools.export_seed 或检查飞书凭据")
        return 1
    html_path = write_html(rows, stats, Path(args.out).resolve())
    try:
        written = [str(html_path.relative_to(ROOT))]
    except ValueError:
        written = [str(html_path)]
    if args.csv:
        csv_path = write_csv(rows, Path(args.csv).resolve())
        try:
            written.append(str(csv_path.relative_to(ROOT)))
        except ValueError:
            written.append(str(csv_path))
    if args.json:
        Path(args.json).write_text(
            json.dumps(board_payload(rows, stats), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(args.json)

    tally = stats["tally"]
    print(
        f"{stats['totalSources']} 个源 · 配置来源 {stats['configSource']} · "
        f"健康记录 {stats['healthRecords']} 行（{args.days} 天）"
    )
    print(
        f"  有效 {tally['effective']} · 低产 {tally['degraded']} · "
        f"被规则卡死 {tally['rule_blocked']} · 过清洗没入库 {tally['post_filter']} · "
        f"抓取失败 {tally['fetch_broken']} · 静默 {tally['dry']} · "
        f"从未采集 {tally['never_run']} · 已暂停 {tally['paused']}"
    )
    print(
        f"  历史条目 {stats['historyItems']} 条（{stats['contributingSources']} 个源贡献，"
        f"前 5 名占 {stats['topShare']}%）· 本地条目覆盖 {stats['itemSpan']}"
    )
    if stats["unattributedItems"]:
        print(
            f"  另有 {stats['unattributedItems']} 条本地条目归不到现行源上"
            "（源改名或已从参数表下线，看板底部有说明）"
        )
    print("  " + " / ".join(written))
    if args.open:
        import webbrowser

        webbrowser.open(html_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
