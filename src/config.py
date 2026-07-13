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
FEISHU_ENTRY_TABLE_ID = _env("FEISHU_ENTRY_TABLE_ID", "tblgeB5ArAD1ugoi")
FEISHU_BRIEF_TABLE_ID = os.environ.get("FEISHU_BRIEF_TABLE_ID", "").strip()
FEISHU_RECIPIENT_OPEN_ID = os.environ.get("FEISHU_RECIPIENT_OPEN_ID", "").strip()
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
DAILY_SIGNAL_LIMIT = int(os.environ.get("DAILY_SIGNAL_LIMIT", "15"))

FEISHU_HOST = "https://open.feishu.cn"

# --- 采集常量（与 n8n 版本保持一致）---
MIN_LOOKBACK_HOURS = 168
MAX_ITEMS_PER_FEED = 80
MAX_ARXIV_ITEMS = int(os.environ.get("MAX_ARXIV_ITEMS", "10"))
DEFAULT_MAX_ARTICLES = 3
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
