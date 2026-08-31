"""首页两块数据看板：模型竞技场榜单 + AI 概念股行情。

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

AA_ENDPOINT = "https://artificialanalysis.ai/api/v2/data/llms/models"
AA_ATTRIBUTION = "https://artificialanalysis.ai/"
LEADERBOARD_LIMIT = 12
HTTP_TIMEOUT = 20

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
    ("xai", "x.ai"),
    ("meta", "meta.com"),
    ("deepseek", "deepseek.com"),
    ("alibaba", "qwen.ai"),
    ("qwen", "qwen.ai"),
    ("zhipu", "z.ai"),
    ("zai", "z.ai"),
    ("kimi", "moonshot.cn"),
    ("moonshot", "moonshot.cn"),
    ("minimax", "minimaxi.com"),
    ("bytedance", "bytedance.com"),
    ("stepfun", "stepfun.com"),
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


def fetch_leaderboard(limit: int = LEADERBOARD_LIMIT) -> dict[str, Any]:
    """按智能指数取前 N。缺指数的模型直接丢掉：没有排序依据的行放进榜里只会误导。

    同一模型的不同推理档位在接口里是并列的独立条目，直接取前 N 会被 Claude Opus 5
    的四个档位占满，看不出到底几家在竞争。按模型去重，每个只留分最高的那一档。
    """
    board: dict[str, Any] = {
        "source": "Artificial Analysis",
        "sourceUrl": AA_ATTRIBUTION,
        "metric": "智能指数",
        "updatedAt": "",
        "error": "",
        "models": [],
    }
    api_key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY", "").strip()
    if not api_key:
        board["error"] = "未配置 ARTIFICIAL_ANALYSIS_API_KEY"
        log.warning("跳过模型榜单：缺少 ARTIFICIAL_ANALYSIS_API_KEY")
        return board
    try:
        response = requests.get(
            AA_ENDPOINT, headers={"x-api-key": api_key}, timeout=HTTP_TIMEOUT
        )
        response.raise_for_status()
        data = response.json().get("data") or []
    except Exception as exc:  # noqa: BLE001 - 榜单挂了不该拖垮整轮发布
        board["error"] = str(exc)
        log.warning("模型榜单获取失败：%s", exc)
        return board

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
    board["models"] = [
        _model_row(item, rank) for rank, item in enumerate(best.values(), 1)
    ]
    board["updatedAt"] = _now_iso()
    log.info("模型榜单 %d 条（候选 %d，评分 %d）", len(board["models"]), len(data), len(scored))
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


def keep_last_good(
    payload: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    """新拉失败时留下昨天的榜/行情，避免发布把可用快照写成「暂无数据」。"""
    if not previous:
        return payload
    merged = dict(payload)
    for board_key, rows_key in (("leaderboard", "models"), ("market", "quotes")):
        incoming = payload.get(board_key) or {}
        old = previous.get(board_key) or {}
        if incoming.get("error") and not _board_has_rows(incoming, rows_key):
            if _board_has_rows(old, rows_key):
                merged[board_key] = old
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
