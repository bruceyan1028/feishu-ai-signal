"""集中管理密钥与常量。密钥一律从环境变量读取，绝不写死在代码里。"""
from __future__ import annotations

import os


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("your_") or value.startswith("cli_xxxx"):
        raise ConfigError(f"缺少环境变量 {name}，请在 .env 或 GitHub Secrets 中配置")
    return value


# --- 飞书 ---
FEISHU_APP_ID = _env("FEISHU_APP_ID")
FEISHU_APP_SECRET = _env("FEISHU_APP_SECRET")
FEISHU_BASE_ID = _env("FEISHU_BASE_ID", "RuI1b05wHa8bIUsok0ac35S8nAd")
FEISHU_PARAM_TABLE_ID = _env("FEISHU_PARAM_TABLE_ID", "tblnJ0vumx8ITmlU")
FEISHU_SOURCE_TABLE_ID = _env("FEISHU_SOURCE_TABLE_ID", "tbl2pNAbuC1omjgp")
FEISHU_ENTRY_TABLE_ID = _env("FEISHU_ENTRY_TABLE_ID", "tblgeB5ArAD1ugoi")

# 类型化筛选配置表：某个源出现在哪张表里就按该类型过滤（表内一源一行，主键 source_id）
FEISHU_PAPER_CONFIG_TABLE_ID = _env("FEISHU_PAPER_CONFIG_TABLE_ID", "tblhTzn8NyjeU779")
FEISHU_WECHAT_CONFIG_TABLE_ID = _env("FEISHU_WECHAT_CONFIG_TABLE_ID", "tblNLmDgL2HpI29U")
FEISHU_VIDEO_CONFIG_TABLE_ID = _env("FEISHU_VIDEO_CONFIG_TABLE_ID", "tblh8FXqPevU7pBq")
FEISHU_SOCIAL_CONFIG_TABLE_ID = _env("FEISHU_SOCIAL_CONFIG_TABLE_ID", "tbl7lTtZRBajtmrQ")
FEISHU_GITHUB_CONFIG_TABLE_ID = _env("FEISHU_GITHUB_CONFIG_TABLE_ID", "tblpZTJWyTRzkQyF")
FEISHU_BRIEF_TABLE_ID = os.environ.get("FEISHU_BRIEF_TABLE_ID", "").strip()
FEISHU_RECIPIENT_OPEN_ID = os.environ.get("FEISHU_RECIPIENT_OPEN_ID", "").strip()
_recipient_open_ids = os.environ.get("FEISHU_RECIPIENT_OPEN_IDS", "").strip() or FEISHU_RECIPIENT_OPEN_ID
FEISHU_RECIPIENT_OPEN_IDS = [
    item.strip()
    for item in _recipient_open_ids.split(",")
    if item.strip()
]
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

# --- Jina Reader ---
JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()

# --- 可选下游 ---
DIFY_WEBHOOK_URL = os.environ.get("DIFY_WEBHOOK_URL", "").strip()

# --- LLM 分析（report.py 真实模式用；OpenAI 兼容接口，可指向 DeepSeek/通义/OpenAI 等）---
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = _env("LLM_MODEL", "deepseek-chat")
# report.py 真实模式单次分析的最新条目数上限
REPORT_MAX_ENTRIES = int(os.environ.get("REPORT_MAX_ENTRIES", "20"))
DAILY_CANDIDATE_LIMIT = int(os.environ.get("DAILY_CANDIDATE_LIMIT", "30"))
DAILY_SIGNAL_LIMIT = int(os.environ.get("DAILY_SIGNAL_LIMIT", "30"))

FEISHU_HOST = "https://open.feishu.cn"

# --- 采集常量（与 n8n 版本保持一致）---
MIN_LOOKBACK_HOURS = 168  # 未配置 lookback_window 时的默认值（不再强制抬高已配置值）
MAX_ITEMS_PER_FEED = 80
MAX_ARXIV_ITEMS = int(os.environ.get("MAX_ARXIV_ITEMS", "10"))
ARXIV_MIN_SIGNAL_SCORE = int(os.environ.get("ARXIV_MIN_SIGNAL_SCORE", "55"))
# 论文质量富集（A 录用 / D 社区热度；已去掉作者维）
PAPER_QUALITY_MIN_SCORE = float(os.environ.get("PAPER_QUALITY_MIN_SCORE", "60"))
# arXiv 预印本必须有社区热度（HF upvotes/评论）才保留：把无人讨论的长尾挡在门外，
# 只放行社区真在关注的论文，避免稀释「快速读新闻」体验。可用环境变量关闭。
ARXIV_REQUIRE_COMMUNITY_HEAT = os.environ.get(
    "ARXIV_REQUIRE_COMMUNITY_HEAT", "1"
).strip().lower() not in {"0", "false", "no"}
PAPER_ENRICH_TIMEOUT = int(os.environ.get("PAPER_ENRICH_TIMEOUT", "12"))
PAPER_ENRICH_ENABLED = os.environ.get("PAPER_ENRICH_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
# 单个 Scrape 源每轮最多抓取的文章数（RSS 不受此限，整份 feed 全收）。
# 日更节奏下取 8：足以覆盖高产媒体，慢更新源多出的额度会重复抓旧文再被去重。
# 可用某源 extra_config.max_articles 单独覆盖。
DEFAULT_MAX_ARTICLES = 8
JINA_CONCURRENCY = 3
JINA_TIMEOUT = 60
HTTP_MAX_TRIES = 4
HTTP_WAIT_SECONDS = 5

DEFAULT_KEYWORD = (
    r"(ai|artificial intelligence|llm|agent|model|gpt|claude|gemini|"
    r"inference|rag|deepseek|llama|nvidia|reasoning)"
)

TIER_LABEL = {
    "L1": "L1 一级官方",
    "L2": "L2 结构化平台",
    "L3": "L3 媒体研报",
    "L4": "L4 补充源",
}


def validate() -> None:
    """启动时校验必填密钥。"""
    _require("FEISHU_APP_ID")
    _require("FEISHU_APP_SECRET")
