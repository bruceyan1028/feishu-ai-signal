"""首页两块数据看板：模型榜单（AA 智能指数 + HF 社区热度）+ AI 概念股行情。

和简报不同，这两块是「状态量」而非「事件流」：读者想在翻信号之前先扫一眼今天模型
排名和资金面有没有变。所以它们不进条目表、不参与去重和打分，只有一份当期快照。

    python -m src.dashboard --output site/data/dashboard-latest.json

日报 `src.publish` 结束时会调一次；流水线不再单独跑。两个来源相互独立，任一挂掉
不影响另一块，也不会让发布失败——看板缺数据顶多少一屏，把整轮日报拖挂就得不偿失。
失败原因写进 payload 的 error 字段，页面照实显示「暂无数据」而不是留白，否则线上
停更了没人看得出来。

榜单用 Artificial Analysis 免费 API：它按统一口径重测所有模型（智能指数、吞吐、
单价），比各家自报的跑分可比。需要 `ARTIFICIAL_ANALYSIS_API_KEY`，官方要求 key 不
得进客户端代码且响应要缓存，所以只能在流水线里取、落成静态 JSON。按其条款，页面上
必须署名并链回 artificialanalysis.ai。

总榜之外还有两张按参数规模分档的开源榜（4B–40B、≤4B）。接口既不给参数规模也不给
开源标记，分不出这两档，只能读 AA 站上对应的分档页面；页面里每张图都内联一份
schema.org Dataset，比解析 Next.js 的流式载荷稳定得多。

再单独一块换口径：Hugging Face 首页「Trending this week」的模型 / 应用 / 数据集三栏，
各取 5 条。AA 答的是「哪个模型更强」，这里答的是「社区这周在看什么」——应用和数据集
根本不是模型，没有智能指数可比，也没有别处可看。三类各有自己的列表接口，公开、不需要
token，但要点名 `expand[]` 才回参数规模、运行状态这些字段。

行情用腾讯免费行情接口：A 股 / 美股 / 港股同一套字段，无需鉴权。返回 GBK 编码的
`v_<code>="a~b~c~..."`，字段按位取，各市场前 35 位布局一致。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "site" / "data" / "dashboard-latest.json"

AA_SITE = "https://artificialanalysis.ai"
AA_ENDPOINT = f"{AA_SITE}/api/v2/data/llms/models"
AA_ATTRIBUTION = f"{AA_SITE}/"
LEADERBOARD_LIMIT = 12
HTTP_TIMEOUT = 20

# 按参数规模分档的开源榜。key 前端用来记住选中页，note 是档位区间。
AA_SIZE_PAGES: tuple[tuple[str, str, str, str], ...] = (
    ("small", "小模型", "4B–40B", f"{AA_SITE}/models/open-source/small"),
    ("tiny", "微型", "≤4B", f"{AA_SITE}/models/open-source/tiny"),
)

# 同一页里智能指数会内联两份 Dataset，字段名不同、数值一致，命中哪份都行。
_INTELLIGENCE_FIELDS = ("artificialAnalysisIntelligenceIndex", "intelligenceIndex")
_PARAM_FIELDS = ("totalParameters", "activeParams")
_LD_JSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)
# 分档页是网页而非接口，带上项目标识便于对方在日志里认出这点访问量。
_PAGE_HEADERS = {"User-Agent": "feishu-ai-signal (+https://artificialanalysis.ai)"}

HF_SITE = "https://huggingface.co"
# 首页那一栏只给 5 条，跟着它，不要自作主张取 12——「本周在看什么」是个短名单
HF_TRENDING_LIMIT = 5
# 三类各有自己的列表接口，但排序参数和响应外形一致。第四位是该类要显式 expand
# 的字段：列表接口默认只回 id / likes / trendingScore 那几位，其余不点名就不给，
# 漏一个的表现是那一列静静地空掉，不会报错。
# 栏目名保留 HF 自己的说法（Models / Spaces / Datasets）：Spaces 译成「应用」会
# 丢掉它在 HF 语境里的含义，而这三个词读者在官网上天天见。
HF_KINDS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "models",
        "Models",
        "models",
        ("author", "downloads", "likes", "trendingScore", "pipeline_tag",
         "lastModified", "safetensors", "inferenceProviderMapping"),
    ),
    (
        # Spaces 没有下载量，identity 靠 cardData 里的标题和 emoji——作者多是个人
        # 账号，拿组织名认 logo 没有意义，首页也是用 emoji 认脸的。
        "spaces",
        "Spaces",
        "spaces",
        ("author", "likes", "trendingScore", "lastModified", "sdk", "runtime",
         "cardData"),
    ),
    (
        "datasets",
        "Datasets",
        "datasets",
        ("author", "downloads", "likes", "trendingScore", "lastModified"),
    ),
)
_HF_HEADERS = {"User-Agent": "feishu-ai-signal (+https://huggingface.co/)"}

# runtime.stage 的取值里只有这几个算「现在能点开就用」，其余（BUILDING、
# RUNTIME_ERROR、PAUSED…）都不算。
_HF_LIVE_STAGES = {"RUNNING", "RUNNING_APP_STARTING", "RUNNING_BUILDING"}

# pipeline_tag 是英文机器标签，窄栏里换成中文短词。认不出的照原样显示，
# HF 新增任务类型时不会变成空白一格。
_HF_TASKS = {
    "text-generation": "文本生成",
    "image-text-to-text": "图文理解",
    "video-text-to-text": "视频理解",
    "audio-text-to-text": "语音理解",
    "any-to-any": "全模态",
    "text-to-image": "文生图",
    "image-to-image": "图生图",
    "text-to-video": "文生视频",
    "image-to-video": "图生视频",
    "image-text-to-video": "图文生视频",
    "video-to-video": "视频转绘",
    "text-to-3d": "文生 3D",
    "image-to-3d": "图生 3D",
    "text-to-speech": "语音合成",
    "text-to-audio": "音频生成",
    "audio-to-audio": "音频转换",
    "automatic-speech-recognition": "语音识别",
    "feature-extraction": "向量表征",
    "sentence-similarity": "文本相似",
    "text-ranking": "重排序",
    "visual-document-retrieval": "文档检索",
    "fill-mask": "掩码填充",
    "translation": "机器翻译",
    "image-classification": "图像分类",
    "image-segmentation": "图像分割",
    "object-detection": "目标检测",
    "time-series-forecasting": "时序预测",
    "robotics": "机器人",
}

QUOTE_ENDPOINT = "https://qt.gtimg.cn/q="
_QUOTE_RE = re.compile(r'v_(\w+)="([^"]*)"')
# 榜单条目名里的推理档位后缀：`(max)`、`(Adaptive Reasoning, High)`
_VARIANT_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")

# 腾讯行情返回位（各市场一致）：1 名称、3 现价、4 昨收、5 今开、30 时间戳、
# 32 涨跌幅、33 最高、34 最低、45 总市值（亿，计价币种）。
_Q_NAME, _Q_SYMBOL, _Q_PRICE, _Q_PREV_CLOSE, _Q_OPEN = 1, 2, 3, 4, 5
_Q_STAMP, _Q_CHANGE_PCT, _Q_HIGH, _Q_LOW, _Q_MARKET_CAP = 30, 32, 33, 34, 45

# 只收和 AI 产业链更近的标的：算力芯片、云与大模型、光模块、AI 服务器、国产替代。
# 腾讯 / 阿里 / 百度 / 小米这类互联网平台不进名单，即使它们也在投模型。
# 第三位是取 favicon 用的品牌域名；Google 没收录的站点留空，直接走字标。
TICKERS: tuple[tuple[str, str, str], ...] = (
    ("usNVDA", "美股", "nvidia.com"),
    ("usAMD", "美股", "amd.com"),
    ("usAVGO", "美股", "broadcom.com"),
    ("usTSM", "美股", "tsmc.com"),
    ("usMU", "美股", "micron.com"),
    ("usARM", "美股", "arm.com"),
    ("usMRVL", "美股", "marvell.com"),
    ("usMSFT", "美股", "microsoft.com"),
    ("usGOOGL", "美股", "google.com"),
    ("usAMZN", "美股", "amazon.com"),
    ("usMETA", "美股", "meta.com"),
    ("usAAPL", "美股", "apple.com"),
    ("usORCL", "美股", "oracle.com"),
    ("usTSLA", "美股", "tesla.com"),
    ("usSPCX", "美股", "spacex.com"),
    ("usPLTR", "美股", "palantir.com"),
    ("usCRWV", "美股", "coreweave.com"),
    ("usSNOW", "美股", "snowflake.com"),
    ("sh688256", "A股", "cambricon.com"),
    ("sh688836", "A股", "unitree.com"),
    ("sh688041", "A股", ""),
    ("sh688795", "A股", ""),
    ("sh688802", "A股", ""),
    ("sh688981", "A股", ""),
    ("sz300308", "A股", "innolight.com"),
    ("sz300502", "A股", "eoptolink.com"),
    ("sz000977", "A股", "inspur.com"),
    ("sh601138", "A股", "fii-foxconn.com"),
    ("sz002230", "A股", "iflytek.com"),
    ("sh688111", "A股", ""),
    ("sz300418", "A股", ""),
    ("sh601360", "A股", "360.cn"),
    ("hk02513", "港股", "z.ai"),
    ("hk00100", "港股", "minimaxi.com"),
    ("hk06082", "港股", ""),
    ("hk09903", "港股", ""),
)

_CURRENCIES = {"us": "USD", "hk": "HKD"}

# 行情接口给的是交易所全称 / 未盈利标记，窄栏放不下，展示时收成常用简称。
_NAME_ALIASES = {
    "Meta Platforms": "Meta",
    "Snowflake Inc.": "Snowflake",
    "MINIMAX-W": "MiniMax",
    "MINIMAX-": "MiniMax",
}


def display_quote_name(raw: str) -> str:
    name = str(raw or "").strip()
    if name in _NAME_ALIASES:
        return _NAME_ALIASES[name]
    if re.match(r"(?i)^meta\s+platform", name):
        return "Meta"
    if re.match(r"(?i)^snowflake", name):
        return "Snowflake"
    if re.match(r"(?i)^minimax", name):
        return "MiniMax"
    name = re.sub(r"-[UW]$", "", name)
    name = re.sub(r"\s+Inc\.?$", "", name, flags=re.I)
    return name


def quote_url(code: str, domain: str = "") -> str:
    """点进行情页：美股 Nasdaq、沪市上交所、深市深交所、港股港交所。

    domain 只给 logo 用，不拿来拼链接——公司主页不是股票站。
    """
    del domain
    prefix, rest = code[:2].lower(), code[2:]
    if not rest:
        return ""
    if prefix == "us":
        return f"https://www.nasdaq.com/market-activity/stocks/{rest.lower()}"
    if prefix == "sh":
        return (
            "https://www.sse.com.cn/assortment/stock/list/info/company/index.shtml"
            f"?COMPANY_CODE={rest}"
        )
    if prefix == "sz":
        return f"https://www.szse.cn/certificate/individual/index.html?code={rest}"
    if prefix == "hk":
        return (
            "https://www.hkex.com.hk/Market-Data/Securities-Prices/Equities/"
            f"Equities-Quote?sc_lang=zh-HK&sym={rest.lstrip('0') or rest}"
        )
    return ""

# 榜单的模型厂商由接口动态给出，按名字取 logo 域名；认不出的前端回退字标。
# 放在这里而不是前端：页面不该知道「智谱」对应哪个域名。
_CREATOR_DOMAINS: tuple[tuple[str, str], ...] = (
    ("openai", "openai.com"),
    ("anthropic", "anthropic.com"),
    ("google", "google.com"),
    ("deepmind", "deepmind.google"),
    # minimax 必须排在 xai 前面：这是顺序敏感的子串匹配，HF 的组织名 MiniMaxAI
    # 去掉分隔符是 minimaxai，含 xai，排在后面就会挂上 Grok 的 logo。
    ("minimax", "minimaxi.com"),
    ("xai", "x.ai"),
    ("meta", "meta.com"),
    ("deepseek", "deepseek.com"),
    ("alibaba", "qwen.ai"),
    ("qwen", "qwen.ai"),
    ("zhipu", "z.ai"),
    ("zai", "z.ai"),
    ("kimi", "moonshot.cn"),
    ("moonshot", "moonshot.cn"),
    ("bytedance", "bytedance.com"),
    ("stepfun", "stepfun.com"),
    ("openbmb", "openbmb.cn"),
    ("modelbest", "modelbest.cn"),
    # 南北阁没有独立官网，模型发在 Hugging Face；nanbeige.com 是他们指向该组织的域名
    ("nanbeige", "nanbeige.com"),
    ("xiaomi", "mi.com"),
    ("baidu", "baidu.com"),
    ("tencent", "tencent.com"),
    ("mistral", "mistral.ai"),
    ("cohere", "cohere.com"),
    ("microsoft", "microsoft.com"),
    ("amazon", "aws.amazon.com"),
    ("nvidia", "nvidia.com"),
    ("ibm", "ibm.com"),
    ("ai21", "ai21.com"),
    ("reka", "reka.ai"),
    ("perplexity", "perplexity.ai"),
    ("liquid", "liquid.ai"),
    ("allen", "allenai.org"),
    ("nous", "nousresearch.com"),
    ("upstage", "upstage.ai"),
    ("databricks", "databricks.com"),
    ("snowflake", "snowflake.com"),
    ("servicenow", "servicenow.com"),
    ("naver", "naver.com"),
    ("lgai", "lgresearch.ai"),
)


def _creator_domain(name: str) -> str:
    key = name.lower().replace(" ", "").replace(".", "")
    for needle, domain in _CREATOR_DOMAINS:
        if needle in key:
            return domain
    return ""


def _load_dotenv() -> None:
    """本机 `python -m src.dashboard` 不经过 bootstrap，需要自己把 .env 灌进环境。

    已在环境里的变量不覆盖：CI 用 GitHub Secrets，本机已 export 的优先。
    """
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _num(value: Any) -> float | None:
    """行情与榜单里的空位混用 ''、'-'、None，统一折成 None，别让 0 冒充真实报价。"""
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _int(value: Any) -> int | None:
    """计数类字段（下载、点赞、热度分）落成整数，别让 JSON 里出现 151021.0。"""
    parsed = _num(value)
    return None if parsed is None else int(parsed)


# --- 模型竞技场榜单 ---


def _model_row(item: dict[str, Any], rank: int) -> dict[str, Any]:
    evaluations = item.get("evaluations") or {}
    pricing = item.get("pricing") or {}
    creator = item.get("model_creator") or {}
    creator_name = creator.get("name") or ""
    return {
        "rank": rank,
        "id": item.get("id") or "",
        # 展示去掉档位后的模型名：分数已按最高档取，挂着 `(max)` 反而像是只测了这一档
        "name": _base_model_name(item.get("name") or ""),
        "variant": item.get("name") or "",
        "creator": creator_name,
        "logoDomain": _creator_domain(creator_name),
        "intelligence": _round(
            _num(evaluations.get("artificial_analysis_intelligence_index")), 1
        ),
        "coding": _round(_num(evaluations.get("artificial_analysis_coding_index")), 1),
        "math": _round(_num(evaluations.get("artificial_analysis_math_index")), 1),
        "price": _round(_num(pricing.get("price_1m_blended_3_to_1")), 2),
        "speed": _round(_num(item.get("median_output_tokens_per_second")), 1),
        "latency": _round(_num(item.get("median_time_to_first_token_seconds")), 2),
    }


def _base_model_name(name: str) -> str:
    """去掉 `(max)`、`(Adaptive Reasoning, High)` 这类推理档位后缀，留模型本身。"""
    return _VARIANT_SUFFIX_RE.sub("", name).strip() or name.strip()


def _variant_tag(name: str) -> str:
    """取出档位后缀里的字，供窄栏用小字单独排一段，不挤占模型名的位置。"""
    match = _VARIANT_SUFFIX_RE.search(name or "")
    if not match or not _VARIANT_SUFFIX_RE.sub("", name).strip():
        return ""
    return match.group(0).strip()[1:-1].strip()


def fetch_aa_models() -> tuple[list[dict[str, Any]], str]:
    """拉 AA 全量模型接口，返回 (行, 错误)。总榜和分档榜共用这一次请求。"""
    api_key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY", "").strip()
    if not api_key:
        log.warning("跳过模型榜单：缺少 ARTIFICIAL_ANALYSIS_API_KEY")
        return [], "未配置 ARTIFICIAL_ANALYSIS_API_KEY"
    try:
        response = requests.get(
            AA_ENDPOINT, headers={"x-api-key": api_key}, timeout=HTTP_TIMEOUT
        )
        response.raise_for_status()
        return response.json().get("data") or [], ""
    except Exception as exc:  # noqa: BLE001 - 榜单挂了不该拖垮整轮发布
        log.warning("模型榜单获取失败：%s", exc)
        return [], str(exc)


def rank_models(
    data: list[dict[str, Any]], limit: int = LEADERBOARD_LIMIT
) -> list[dict[str, Any]]:
    """按智能指数取前 N。缺指数的模型直接丢掉：没有排序依据的行放进榜里只会误导。

    同一模型的不同推理档位在接口里是并列的独立条目，直接取前 N 会被 Claude Opus 5
    的四个档位占满，看不出到底几家在竞争。按模型去重，每个只留分最高的那一档。
    """
    scored = [
        item
        for item in data
        if _num((item.get("evaluations") or {}).get("artificial_analysis_intelligence_index"))
        is not None
    ]
    scored.sort(
        key=lambda item: _num(
            item["evaluations"]["artificial_analysis_intelligence_index"]
        )
        or 0,
        reverse=True,
    )
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for item in scored:
        creator = (item.get("model_creator") or {}).get("name") or ""
        key = (creator.lower(), _base_model_name(item.get("name") or "").lower())
        if key not in best:  # 已按分数降序，先到的就是该模型的最高档
            best[key] = item
        if len(best) >= limit:
            break
    log.info("模型榜单 %d 条（候选 %d，评分 %d）", len(best), len(data), len(scored))
    return [_model_row(item, rank) for rank, item in enumerate(best.values(), 1)]


def fetch_leaderboard(limit: int = LEADERBOARD_LIMIT) -> dict[str, Any]:
    """总榜 + 按参数规模分档的两张开源榜。"""
    board: dict[str, Any] = {
        "source": "Artificial Analysis",
        "sourceUrl": AA_ATTRIBUTION,
        "metric": "智能指数",
        "updatedAt": "",
        "error": "",
        "models": [],
        "buckets": [],
    }
    data, board["error"] = fetch_aa_models()
    board["models"] = rank_models(data, limit)
    if board["models"]:
        board["updatedAt"] = _now_iso()
    # 分档榜自己读页面，接口挂了照样有数据，只是认不出厂商、logo 退成字标。
    board["buckets"] = fetch_size_buckets(data, limit)
    return board


# --- 分档开源榜（4B–40B / ≤4B） ---


def _slug_of(details_url: Any) -> str:
    return str(details_url or "").rstrip("/").rsplit("/", 1)[-1]


def _ld_json_datasets(html: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in _LD_JSON_RE.findall(html):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("@type") == "Dataset":
            out.append(obj)
    return out


def _dataset_field(row: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for field in fields:
        if field in row:
            return _num(row[field])
    return None


def parse_size_page(html: str) -> list[dict[str, Any]]:
    """从分档页解析智能指数榜，按指数降序。

    页面每张图都内联一份 schema.org Dataset；智能指数那张给分数，参数规模那张给
    模型大小，两者按 detailsUrl 里的 slug 对齐。
    """
    datasets = _ld_json_datasets(html)
    params: dict[str, float] = {}
    for dataset in datasets:
        for row in dataset.get("data") or []:
            value = _dataset_field(row, _PARAM_FIELDS)
            slug = _slug_of(row.get("detailsUrl"))
            if slug and value is not None:
                params.setdefault(slug, value)

    rows: dict[str, dict[str, Any]] = {}
    for dataset in datasets:
        for row in dataset.get("data") or []:
            score = _dataset_field(row, _INTELLIGENCE_FIELDS)
            slug = _slug_of(row.get("detailsUrl"))
            label = str(row.get("label") or "").strip()
            if score is None or not slug or not label:
                continue
            rows.setdefault(
                slug,
                {
                    "slug": slug,
                    "label": label,
                    "intelligence": _round(score, 1),
                    "params": _round(params.get(slug), 2),
                },
            )
    return sorted(rows.values(), key=lambda row: row["intelligence"], reverse=True)


def _bucket_row(
    row: dict[str, Any], rank: int, creators: dict[str, str]
) -> dict[str, Any]:
    creator = creators.get(row["slug"], "")
    label = row["label"]
    return {
        "rank": rank,
        "slug": row["slug"],
        "label": label,
        # 档位后缀单独拆出来：4 行都叫 Qwen3.8 27B 时，靠这一段才分得清是哪一档
        "name": _base_model_name(label),
        "tag": _variant_tag(label),
        "creator": creator,
        "logoDomain": _creator_domain(creator),
        "intelligence": row["intelligence"],
        "params": row["params"],
        "url": f"{AA_SITE}/models/{row['slug']}",
    }


def fetch_size_bucket(
    key: str,
    label: str,
    note: str,
    url: str,
    creators: dict[str, str],
    limit: int = LEADERBOARD_LIMIT,
) -> dict[str, Any]:
    """抓一张分档页。这里照搬页面的排序和条目名，不再按模型去重——分档榜的用处
    正是复现那一页，AA 把 Qwen3.8 27B 的四个档位并列展示，我们也并列。
    """
    bucket: dict[str, Any] = {
        "key": key,
        "label": label,
        "note": note,
        "sourceUrl": url,
        "updatedAt": "",
        "error": "",
        "models": [],
    }
    try:
        response = requests.get(url, headers=_PAGE_HEADERS, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        rows = parse_size_page(response.text)
    except Exception as exc:  # noqa: BLE001 - 一档挂了不影响另一档和总榜
        bucket["error"] = str(exc)
        log.warning("分档榜 %s 获取失败：%s", key, exc)
        return bucket
    if not rows:
        # 页面结构变了要能看出来，否则只会表现为「这一档突然空了」
        bucket["error"] = "分档页未给出智能指数数据"
        log.warning("分档榜 %s 未解析出模型，页面结构可能已变", key)
        return bucket
    bucket["models"] = [
        _bucket_row(row, rank, creators) for rank, row in enumerate(rows[:limit], 1)
    ]
    bucket["updatedAt"] = _now_iso()
    log.info("分档榜 %s（%s）%d 条", key, note, len(bucket["models"]))
    return bucket


def fetch_size_buckets(
    data: list[dict[str, Any]], limit: int = LEADERBOARD_LIMIT
) -> list[dict[str, Any]]:
    """两张分档榜。厂商名从接口按 slug 借一份，只为取 logo。"""
    creators = {
        item["slug"]: (item.get("model_creator") or {}).get("name") or ""
        for item in data or []
        if item.get("slug")
    }
    return [
        fetch_size_bucket(key, label, note, url, creators, limit)
        for key, label, note, url in AA_SIZE_PAGES
    ]


# --- Hugging Face 本周热度（模型 / 应用 / 数据集） ---


def _hf_task_label(tag: Any) -> str:
    key = str(tag or "").strip()
    return _HF_TASKS.get(key, key)


def _hf_params_b(model: dict[str, Any]) -> float | None:
    """参数规模换算成「多少 B」。

    接口不给现成的规模，只在 safetensors 索引里给张量总数。GGUF 量化仓库和不传
    safetensors 的权重没有这一位，留 None——0 会被读成「零参数」而不是「不知道」。
    """
    total = _num((model.get("safetensors") or {}).get("total"))
    return None if total is None else total / 1e9


def _hf_base_row(item: dict[str, Any], rank: int, kind: str) -> dict[str, Any]:
    """三类共有的那几位。`id` 一律是 `owner/name` 形式，展示时只留后半段。"""
    repo = str(item.get("id") or "")
    owner, _, name = repo.partition("/")
    if not name:  # 少数官方仓库没有组织前缀，例如 gpt2
        owner, name = "", repo
    owner = str(item.get("author") or owner)
    # 数据集的 URL 多一段 /datasets/，模型和应用不一样
    prefix = {"datasets": "/datasets", "spaces": "/spaces"}.get(kind, "")
    return {
        "rank": rank,
        "repo": repo,
        "name": name,
        "owner": owner,
        "trending": _int(item.get("trendingScore")),
        "likes": _int(item.get("likes")),
        "downloads": _int(item.get("downloads")),
        "updatedAt": str(item.get("lastModified") or "")[:10],
        "url": f"{HF_SITE}{prefix}/{repo}" if repo else f"{HF_SITE}/",
    }


def _hf_model_row(item: dict[str, Any], rank: int) -> dict[str, Any]:
    row = _hf_base_row(item, rank, "models")
    row.update(
        {
            # 组织归属由 logo 说清楚了，窄栏里只留仓库名那一段
            "logoDomain": _creator_domain(row["owner"]),
            "params": _round(_hf_params_b(item), 1),
            "task": _hf_task_label(item.get("pipeline_tag")),
            # 有厂商接了推理服务才能直接调，这是「今天能不能用上」的分界
            "inference": bool(item.get("inferenceProviderMapping")),
        }
    )
    return row


def _hf_space_row(item: dict[str, Any], rank: int) -> dict[str, Any]:
    """应用没有下载量，认脸靠 cardData 里的标题和 emoji——首页也是这么排的。"""
    row = _hf_base_row(item, rank, "spaces")
    card = item.get("cardData") or {}
    stage = str((item.get("runtime") or {}).get("stage") or "")
    row.update(
        {
            # 作者多是个人账号，拿它认 logo 没有意义，用 emoji 当头像
            "logoDomain": "",
            "emoji": str(card.get("emoji") or ""),
            # cardData 的标题是作者给人看的名字，仓库名常常是 wan555 这种缩写
            "name": str(card.get("title") or row["name"]),
            "note": str(card.get("short_description") or ""),
            "sdk": str(item.get("sdk") or card.get("sdk") or ""),
            "stage": stage,
            # 停在 BUILDING / RUNTIME_ERROR 的应用点开是白屏，值得先标出来
            "live": stage in _HF_LIVE_STAGES,
        }
    )
    return row


def _hf_dataset_row(item: dict[str, Any], rank: int) -> dict[str, Any]:
    row = _hf_base_row(item, rank, "datasets")
    row["logoDomain"] = _creator_domain(row["owner"])
    return row


_HF_ROW_BUILDERS = {
    "models": _hf_model_row,
    "spaces": _hf_space_row,
    "datasets": _hf_dataset_row,
}


def rank_hf_items(
    data: list[dict[str, Any]], kind: str, limit: int = HF_TRENDING_LIMIT
) -> list[dict[str, Any]]:
    """按热度分降序取前 N，丢掉没有热度分的条目。

    排序参数是查询串里的东西，这里不指望接口一定照办，自己再排一次；顺手把没有
    排序依据的条目去掉，免得它们靠返回顺序混进前几名。
    """
    build = _HF_ROW_BUILDERS[kind]
    scored = [
        item
        for item in data or []
        if isinstance(item, dict) and _num(item.get("trendingScore")) is not None
    ]
    scored.sort(key=lambda item: _num(item.get("trendingScore")) or 0, reverse=True)
    return [build(item, rank) for rank, item in enumerate(scored[:limit], 1)]


def fetch_hf_section(
    key: str,
    label: str,
    path: str,
    expand: tuple[str, ...],
    limit: int = HF_TRENDING_LIMIT,
) -> dict[str, Any]:
    """抓一类。三类各自成败：应用接口挂了不该把模型和数据集也换成「暂无」。"""
    section: dict[str, Any] = {
        "key": key,
        "label": label,
        "sourceUrl": f"{HF_SITE}/{path}",
        "updatedAt": "",
        "error": "",
        "items": [],
    }
    params = [("sort", "trendingScore"), ("direction", "-1"), ("limit", str(limit))]
    params += [("expand[]", field) for field in expand]
    try:
        response = requests.get(
            f"{HF_SITE}/api/{path}",
            params=params,
            headers=_HF_HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - 同上，一类挂了不该拖垮整轮发布
        section["error"] = str(exc)
        log.warning("HF %s 获取失败：%s", key, exc)
        return section
    if not isinstance(data, list):
        # 接口改成 {"models": [...]} 这类包装、或者 expand 写错回了报错体时，
        # 都会走到这里；不写 error 只会表现为「这一栏突然空了」
        section["error"] = "HF 接口未返回列表"
        log.warning("HF %s 响应不是列表，接口结构可能已变：%r", key, data)
        return section
    section["items"] = rank_hf_items(data, key, limit)
    if not section["items"]:
        section["error"] = "HF 接口未给出热度分"
        log.warning("HF %s 无有效条目，trendingScore 可能已改名", key)
        return section
    section["updatedAt"] = _now_iso()
    log.info("HF %s %d 条（返回 %d）", key, len(section["items"]), len(data))
    return section


def fetch_hf_trending(limit: int = HF_TRENDING_LIMIT) -> dict[str, Any]:
    """HF 首页「Trending this week」那三栏：模型 / 应用 / 数据集。

    公开接口，不需要 token。和 AA 的榜是两个问题：AA 按统一口径测「哪个模型更
    强」，这里是「社区这周在看什么」。应用和数据集尤其没有别处可看——一个开源
    数据集突然被下载几十万次，往往比又一次刷榜更早说明方向。
    """
    board: dict[str, Any] = {
        "source": "Hugging Face",
        "sourceUrl": f"{HF_SITE}/",
        "metric": "热度分",
        "updatedAt": "",
        "error": "",
        "sections": [],
    }
    board["sections"] = [
        fetch_hf_section(key, label, path, expand, limit)
        for key, label, path, expand in HF_KINDS
    ]
    if any(section["items"] for section in board["sections"]):
        board["updatedAt"] = _now_iso()
    else:
        # 三类全挂才算整块挂了，页面照实说原因而不是留白
        board["error"] = next(
            (s["error"] for s in board["sections"] if s["error"]), "HF 三类均无数据"
        )
    return board


# --- AI 概念股行情 ---


def _quote_row(
    code: str, market: str, logo_domain: str, parts: list[str]
) -> dict[str, Any] | None:
    price = _num(parts[_Q_PRICE])
    prev_close = _num(parts[_Q_PREV_CLOSE])
    if price is None or not price:
        return None
    # 涨跌额自己按现价减昨收算：接口那一位在个别市场会给空串，而现价和昨收一直有值。
    change = None if prev_close is None else price - prev_close
    change_pct = _num(parts[_Q_CHANGE_PCT])
    if change_pct is None and change is not None and prev_close:
        change_pct = change / prev_close * 100
    return {
        "code": code,
        "market": market,
        "name": display_quote_name(parts[_Q_NAME]),
        # 美股这一位带交易所后缀（NVDA.OQ），终端上只认代码本身
        "symbol": parts[_Q_SYMBOL].split(".")[0],
        "logoDomain": logo_domain,
        "url": quote_url(code, logo_domain),
        "currency": _CURRENCIES.get(code[:2], "CNY"),
        "price": price,
        "change": _round(change, 2),
        "changePct": _round(change_pct, 2),
        "open": _num(parts[_Q_OPEN]),
        "high": _num(parts[_Q_HIGH]),
        "low": _num(parts[_Q_LOW]),
        "prevClose": prev_close,
        "marketCap": _round(_num(parts[_Q_MARKET_CAP]), 1),
        "quotedAt": parts[_Q_STAMP],
    }


def parse_quotes(
    body: str,
    markets: dict[str, str],
    logos: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """腾讯把所有标的拼成一串 `v_code="..."`，逐条按位取值；字段不够长的直接跳过。"""
    logos = logos or {}
    rows: list[dict[str, Any]] = []
    for code, payload in _QUOTE_RE.findall(body):
        parts = payload.split("~")
        if len(parts) <= _Q_MARKET_CAP:
            log.warning("行情字段数异常，跳过 %s（%d 位）", code, len(parts))
            continue
        row = _quote_row(code, markets.get(code, ""), logos.get(code, ""), parts)
        if row:
            rows.append(row)
    return rows


def fetch_market(
    tickers: tuple[tuple[str, str, str], ...] = TICKERS
) -> dict[str, Any]:
    board: dict[str, Any] = {
        "source": "腾讯财经",
        "sourceUrl": "https://stockapp.finance.qq.com/",
        "updatedAt": "",
        "error": "",
        "quotes": [],
    }
    markets = {code: market for code, market, _ in tickers}
    logos = {code: domain for code, _, domain in tickers}
    try:
        response = requests.get(
            QUOTE_ENDPOINT + ",".join(code for code, _, _ in tickers),
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        # 接口固定 GBK，requests 按响应头猜会把中文名解成乱码。
        response.encoding = "gbk"
        quotes = parse_quotes(response.text, markets, logos)
    except Exception as exc:  # noqa: BLE001 - 同上，行情挂了不影响简报
        board["error"] = str(exc)
        log.warning("行情获取失败：%s", exc)
        return board

    if not quotes:
        board["error"] = "行情接口未返回任何有效报价"
        log.warning("行情接口未返回任何有效报价")
        return board
    order = {code: index for index, (code, _, _) in enumerate(tickers)}
    quotes.sort(key=lambda row: order.get(row["code"], len(order)))
    board["quotes"] = quotes
    board["updatedAt"] = _now_iso()
    log.info("行情 %d 只（请求 %d 只）", len(quotes), len(tickers))
    return board


# --- 落盘 ---


def build_payload() -> dict[str, Any]:
    return {
        "generatedAt": _now_iso(),
        "leaderboard": fetch_leaderboard(),
        "hfTrending": fetch_hf_trending(),
        "market": fetch_market(),
    }


def write_payload(payload: dict[str, Any], output: Path | str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _board_has_rows(board: dict[str, Any] | None, key: str) -> bool:
    if not board:
        return False
    rows = board.get(key) or []
    return bool(rows)


def keep_last_good_buckets(
    incoming: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    rows_key: str = "models",
) -> list[dict[str, Any]]:
    """逐段回退：小模型页挂了不该把微型页也换成旧数据。

    AA 分档榜和 HF 三栏是同一个形状——一列带 `key` 的子块，各自成败，所以共用
    这一份；差别只在装行的字段叫 `models` 还是 `items`。
    """
    if not incoming:
        return previous
    old = {bucket.get("key"): bucket for bucket in previous}
    kept = []
    for bucket in incoming:
        stale = old.get(bucket.get("key"))
        if (
            bucket.get("error")
            and not _board_has_rows(bucket, rows_key)
            and _board_has_rows(stale, rows_key)
        ):
            kept.append(stale)
        else:
            kept.append(bucket)
    return kept


def keep_last_good(
    payload: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    """新拉失败时留下昨天的榜/行情，避免发布把可用快照写成「暂无数据」。"""
    if not previous:
        return payload
    merged = dict(payload)
    for board_key, rows_key in (
        ("leaderboard", "models"),
        ("market", "quotes"),
    ):
        incoming = payload.get(board_key) or {}
        old = previous.get(board_key) or {}
        if incoming.get("error") and not _board_has_rows(incoming, rows_key):
            if _board_has_rows(old, rows_key):
                merged[board_key] = old
    # 分档榜和总榜各自成败，上面整块换成旧榜时也要把这次拉到的分档接回去
    buckets = keep_last_good_buckets(
        (payload.get("leaderboard") or {}).get("buckets") or [],
        (previous.get("leaderboard") or {}).get("buckets") or [],
    )
    if buckets and isinstance(merged.get("leaderboard"), dict):
        merged["leaderboard"] = {**merged["leaderboard"], "buckets": buckets}
    # HF 那块没有「整块」的行，只有三栏；逐栏回退就够，不必再判整块
    sections = keep_last_good_buckets(
        (payload.get("hfTrending") or {}).get("sections") or [],
        (previous.get("hfTrending") or {}).get("sections") or [],
        rows_key="items",
    )
    if sections and isinstance(merged.get("hfTrending"), dict):
        merged["hfTrending"] = {**merged["hfTrending"], "sections": sections}
    return merged


def refresh_into(output: Path | str) -> Path:
    """拉当期榜单与行情并落盘；一侧失败则沿用该侧旧数据。"""
    _load_dotenv()
    path = Path(output)
    previous = None
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("无法读取旧看板 %s：%s", path, exc)
    payload = keep_last_good(build_payload(), previous)
    return write_payload(payload, path)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成首页数据看板 JSON")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)

    path = refresh_into(args.output)
    log.info("看板数据已写入 %s", path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    raise SystemExit(run())
