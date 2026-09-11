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
# 不设默认值：漏配时必须显式报错，否则会静默跑到别人的 base 上
FEISHU_BASE_ID = _env("FEISHU_BASE_ID", "")
FEISHU_PARAM_TABLE_ID = _env("FEISHU_PARAM_TABLE_ID", "")
FEISHU_ENTRY_TABLE_ID = _env("FEISHU_ENTRY_TABLE_ID", "")

# 类型化筛选配置表：某个源出现在哪张表里就按该类型过滤（表内一源一行，主键 source_id）
# 留空则该类型的筛选规则不生效（load_typed_configs 会跳过）
FEISHU_PAPER_CONFIG_TABLE_ID = _env("FEISHU_PAPER_CONFIG_TABLE_ID", "")
FEISHU_WECHAT_CONFIG_TABLE_ID = _env("FEISHU_WECHAT_CONFIG_TABLE_ID", "")
FEISHU_VIDEO_CONFIG_TABLE_ID = _env("FEISHU_VIDEO_CONFIG_TABLE_ID", "")
FEISHU_SOCIAL_CONFIG_TABLE_ID = _env("FEISHU_SOCIAL_CONFIG_TABLE_ID", "")
FEISHU_GITHUB_CONFIG_TABLE_ID = _env("FEISHU_GITHUB_CONFIG_TABLE_ID", "")
FEISHU_BRIEF_TABLE_ID = os.environ.get("FEISHU_BRIEF_TABLE_ID", "").strip()
FEISHU_WEEKLY_TABLE_ID = os.environ.get("FEISHU_WEEKLY_TABLE_ID", "").strip()
FEISHU_WEEKLY_PENDING_TABLE_ID = os.environ.get(
    "FEISHU_WEEKLY_PENDING_TABLE_ID", ""
).strip()
FEISHU_TRACKED_ENTITY_TABLE_ID = os.environ.get(
    "FEISHU_TRACKED_ENTITY_TABLE_ID", ""
).strip()
FEISHU_TRACKED_EVENT_TABLE_ID = os.environ.get(
    "FEISHU_TRACKED_EVENT_TABLE_ID", ""
).strip()


def require_tables(*names: str) -> None:
    """入口处校验必需的表配置，避免带着空 table_id 去调 API 后拿到含糊的报错。"""
    missing = [name for name in names if not globals().get(name)]
    if missing:
        raise ConfigError("缺少环境变量 " + "、".join(missing) + "，请在 .env 或 GitHub Secrets 中配置")


FEISHU_RECIPIENT_OPEN_ID = os.environ.get("FEISHU_RECIPIENT_OPEN_ID", "").strip()
_recipient_open_ids = os.environ.get("FEISHU_RECIPIENT_OPEN_IDS", "").strip() or FEISHU_RECIPIENT_OPEN_ID
FEISHU_RECIPIENT_OPEN_IDS = [
    item.strip()
    for item in _recipient_open_ids.split(",")
    if item.strip()
]
# 与 FEISHU_RECIPIENT_OPEN_IDS 按顺序对应；用于日志和发送结果卡片，避免暴露 open_id。
FEISHU_RECIPIENT_NAMES = [
    item.strip()
    for item in os.environ.get("FEISHU_RECIPIENT_NAMES", "").split(",")
    if item.strip()
]
FEISHU_RECIPIENT_NAME_BY_OPEN_ID = {
    open_id: FEISHU_RECIPIENT_NAMES[index]
    for index, open_id in enumerate(FEISHU_RECIPIENT_OPEN_IDS)
    if index < len(FEISHU_RECIPIENT_NAMES)
}
# 群聊优先：配置后日报/周报只发群，不再逐人私聊。chat_id 形如 oc_xxx。
FEISHU_RECIPIENT_CHAT_IDS = [
    item.strip()
    for item in os.environ.get("FEISHU_RECIPIENT_CHAT_IDS", "").split(",")
    if item.strip()
]
FEISHU_RECIPIENT_CHAT_NAMES = [
    item.strip()
    for item in os.environ.get("FEISHU_RECIPIENT_CHAT_NAMES", "").split(",")
    if item.strip()
]
FEISHU_RECIPIENT_CHAT_NAME_BY_ID = {
    chat_id: FEISHU_RECIPIENT_CHAT_NAMES[index]
    for index, chat_id in enumerate(FEISHU_RECIPIENT_CHAT_IDS)
    if index < len(FEISHU_RECIPIENT_CHAT_NAMES)
}
FEISHU_DELIVERY_REPORT_OPEN_ID = os.environ.get(
    "FEISHU_DELIVERY_REPORT_OPEN_ID", ""
).strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

# --- Jina Reader ---
JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()

# --- 可选下游 ---
DIFY_WEBHOOK_URL = os.environ.get("DIFY_WEBHOOK_URL", "").strip()

# --- LLM 分析（report.py 真实模式用；OpenAI 兼容接口，可指向 DeepSeek/通义/OpenAI 等）---
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = _env("LLM_MODEL", "deepseek-chat")
# 单次日报会并发请求模型网关。把连接、读取和总预算拆开，避免无响应网关占满 worker。
LLM_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("LLM_CONNECT_TIMEOUT_SECONDS", "10"))
LLM_READ_TIMEOUT_SECONDS = float(os.environ.get("LLM_READ_TIMEOUT_SECONDS", "45"))
LLM_TOTAL_TIMEOUT_SECONDS = float(os.environ.get("LLM_TOTAL_TIMEOUT_SECONDS", "60"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
# 备用模型必须是独立供应商/网关，才可规避单点限流或网络故障。任一组缺字段即跳过。
LLM_FALLBACK_PROVIDERS = tuple(
    {
        "name": f"fallback_{index}",
        "api_key": os.environ.get(f"LLM_FALLBACK_{index}_API_KEY", "").strip(),
        "base_url": os.environ.get(f"LLM_FALLBACK_{index}_BASE_URL", "").strip().rstrip("/"),
        "model": os.environ.get(f"LLM_FALLBACK_{index}_MODEL", "").strip(),
    }
    for index in (1, 2)
    if all(
        os.environ.get(f"LLM_FALLBACK_{index}_{field}", "").strip()
        for field in ("API_KEY", "BASE_URL", "MODEL")
    )
)
LLM_PROVIDER_FAILURE_THRESHOLD = int(
    os.environ.get("LLM_PROVIDER_FAILURE_THRESHOLD", "3")
)
LLM_PROVIDER_COOLDOWN_SECONDS = float(
    os.environ.get("LLM_PROVIDER_COOLDOWN_SECONDS", "600")
)
# 图片筛选默认复用文本模型的网关和密钥，只单独指定模型；必要时仍可覆盖。
VISION_API_KEY = os.environ.get("VISION_API_KEY", "").strip() or LLM_API_KEY
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "").strip().rstrip("/") or LLM_BASE_URL
VISION_MODEL = _env("VISION_MODEL", "gpt-5.6-luna")
VISION_MAX_CANDIDATES = int(os.environ.get("VISION_MAX_CANDIDATES", "12"))
VISION_COVER_MIN_CONFIDENCE = float(
    os.environ.get("VISION_COVER_MIN_CONFIDENCE", "0.72")
)
VISION_COVER_MIN_QUALITY = float(os.environ.get("VISION_COVER_MIN_QUALITY", "0.72"))
VISION_BODY_MIN_CONFIDENCE = float(
    os.environ.get("VISION_BODY_MIN_CONFIDENCE", "0.62")
)
VISION_BODY_MIN_TOPIC_RELEVANCE = float(
    os.environ.get("VISION_BODY_MIN_TOPIC_RELEVANCE", "0.72")
)
# 对聚合页、日报页等混合内容的候选图做主线过滤。上下文不足时保持宽容，
# 完整上下文与标题/摘要没有交集时才在送入视觉模型前排除。
VISION_STORY_CONTEXT_MIN_CHARS = int(
    os.environ.get("VISION_STORY_CONTEXT_MIN_CHARS", "24")
)
VISION_STORY_MIN_SHARED_NGRAMS = int(
    os.environ.get("VISION_STORY_MIN_SHARED_NGRAMS", "2")
)
# 缺图时的生成式兜底默认关闭，显式开启才会产生图片 API 费用。
IMAGE_GENERATION_ENABLED = os.environ.get(
    "IMAGE_GENERATION_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}
IMAGE_GENERATION_API_KEY = (
    os.environ.get("IMAGE_GENERATION_API_KEY", "").strip() or VISION_API_KEY
)
IMAGE_GENERATION_BASE_URL = _env(
    "IMAGE_GENERATION_BASE_URL", VISION_BASE_URL
).rstrip("/")
IMAGE_GENERATION_MODEL = _env("IMAGE_GENERATION_MODEL", "gemini-3.1-flash-image")
IMAGE_GENERATION_ENDPOINT = _env("IMAGE_GENERATION_ENDPOINT", "auto").lower()
IMAGE_GENERATION_MAX_ATTEMPTS = int(os.environ.get("IMAGE_GENERATION_MAX_ATTEMPTS", "3"))
IMAGE_GENERATION_MIN_CONFIDENCE = float(
    os.environ.get("IMAGE_GENERATION_MIN_CONFIDENCE", "0.72")
)
# 播客托管语音转写。单独配置，不能假设文本 LLM 服务也实现 audio/transcriptions。
ASR_API_KEY = os.environ.get("ASR_API_KEY", "").strip()
ASR_BASE_URL = _env("ASR_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ASR_MODEL = _env("ASR_MODEL", "gpt-4o-transcribe")
ASR_CHUNK_SECONDS = int(os.environ.get("ASR_CHUNK_SECONDS", "1200"))
PODCAST_MAX_DURATION_SECONDS = int(os.environ.get("PODCAST_MAX_DURATION_SECONDS", "14400"))
PODCAST_MAX_AUDIO_MB = int(os.environ.get("PODCAST_MAX_AUDIO_MB", "500"))
PODCAST_TRANSCRIPT_CHUNK_CHARS = int(
    os.environ.get("PODCAST_TRANSCRIPT_CHUNK_CHARS", "10000")
)
# report.py 真实模式单次分析的最新条目数上限
REPORT_MAX_ENTRIES = int(os.environ.get("REPORT_MAX_ENTRIES", "20"))
DAILY_CANDIDATE_LIMIT = int(os.environ.get("DAILY_CANDIDATE_LIMIT", "30"))
DAILY_SIGNAL_LIMIT = int(os.environ.get("DAILY_SIGNAL_LIMIT", "30"))
# 单条日报分析会触发文本/视觉 LLM，保守并发以避免网关限流后重试反而拉长总耗时。
DAILY_ANALYSIS_CONCURRENCY = int(os.environ.get("DAILY_ANALYSIS_CONCURRENCY", "3"))
# 历史条目的长解读是可异步补全内容；日报主链路不应因它逐条等待模型。
DAILY_FILL_LEGACY_DEEP_ANALYSIS = os.environ.get(
    "DAILY_FILL_LEGACY_DEEP_ANALYSIS", "0"
).strip().lower() in {"1", "true", "yes", "on"}
# 每份简报里「论文」类条目的硬上限，避免论文稀释「快速读新闻」体验
DAILY_MAX_PAPERS = int(os.environ.get("DAILY_MAX_PAPERS", "4"))
# 视频更适合作为补充材料：候选最多 4 条，排序分轻微降权，避免同日频道更新挤占新闻。
DAILY_MAX_VIDEOS = int(os.environ.get("DAILY_MAX_VIDEOS", "4"))
DAILY_MIN_VIDEOS = int(os.environ.get("DAILY_MIN_VIDEOS", "1"))
DAILY_VIDEO_WEIGHT = float(os.environ.get("DAILY_VIDEO_WEIGHT", "0.9"))
# 播客独立成栏，不与新闻同池竞争，也不设条数上限；候选质量排序仍用该权重轻度降权。
DAILY_PODCAST_WEIGHT = float(os.environ.get("DAILY_PODCAST_WEIGHT", "0.85"))
# 榜单类每天变化很小，长期霸榜项目会反复占位，故单独限额。
DAILY_MAX_GITHUB = int(os.environ.get("DAILY_MAX_GITHUB", "5"))
# 单个来源在候选池中的上限：防止一个源（尤其是榜单/聚合源）刷屏。
DAILY_MAX_PER_SOURCE = int(os.environ.get("DAILY_MAX_PER_SOURCE", "4"))
# 给非 P0 来源保留的名额：P0 源数量多时会填满候选池，
# 导致中文媒体、实验室等 P1/P2 源永远进不了简报。
DAILY_MIN_NON_P0 = int(os.environ.get("DAILY_MIN_NON_P0", "8"))
WEEKLY_LOOKBACK_DAYS = int(os.environ.get("WEEKLY_LOOKBACK_DAYS", "7"))
WEEKLY_SIGNAL_LIMIT = int(os.environ.get("WEEKLY_SIGNAL_LIMIT", "80"))
TIMELINE_DEFAULT_LOOKBACK_DAYS = int(
    os.environ.get("TIMELINE_DEFAULT_LOOKBACK_DAYS", "90")
)
TIMELINE_DEFAULT_MIN_IMPACT = int(
    os.environ.get("TIMELINE_DEFAULT_MIN_IMPACT", "0")
)
# 英文正文翻译的字数上限：超长部分截断并提示读原文。
# 3000 字符只够译出一千余字中文，正文普遍断在句子中间，故普通条目抬到 6000。
BODY_TRANSLATE_LIMIT = int(os.environ.get("BODY_TRANSLATE_LIMIT", "6000"))
# P0 来源与高影响分条目走全译档，长文也能读完
BODY_TRANSLATE_LIMIT_FULL = int(os.environ.get("BODY_TRANSLATE_LIMIT_FULL", "16000"))
# 单次 LLM 调用的翻译片段大小：再长模型就会自己压缩甚至截断输出
BODY_TRANSLATE_CHUNK = int(os.environ.get("BODY_TRANSLATE_CHUNK", "3000"))
# 走全译档的影响分门槛
BODY_TRANSLATE_FULL_IMPACT = int(os.environ.get("BODY_TRANSLATE_FULL_IMPACT", "80"))
# 照抄原文的中文稿在上屏前做一次 LLM 清理排版，超出这个字数的尾部按原样附回
BODY_POLISH_LIMIT = int(os.environ.get("BODY_POLISH_LIMIT", "20000"))
# 单次清理的片段大小：片段越长，模型越容易顺手改写而不是只做删减
BODY_POLISH_CHUNK = int(os.environ.get("BODY_POLISH_CHUNK", "3500"))

FEISHU_HOST = "https://open.feishu.cn"

# --- 采集常量（与 n8n 版本保持一致）---
MIN_LOOKBACK_HOURS = 168  # 未配置 lookback_window 时的默认值（不再强制抬高已配置值）
MAX_ITEMS_PER_FEED = 80
MAX_ARXIV_ITEMS = int(os.environ.get("MAX_ARXIV_ITEMS", "10"))
ARXIV_MIN_SIGNAL_SCORE = int(os.environ.get("ARXIV_MIN_SIGNAL_SCORE", "55"))
# arXiv 条目在简报排序里的质量权重（<1 即降权），让有限的论文名额优先给
# hf/pwc/已录用等更高价值来源，而非原始 arXiv 长尾
ARXIV_QUALITY_WEIGHT = float(os.environ.get("ARXIV_QUALITY_WEIGHT", "0.7"))
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
# RSS 源之间无数据依赖。受控并发能隔离单源重试等待，避免慢源阻塞整批。
RSS_CONCURRENCY = int(os.environ.get("RSS_CONCURRENCY", "6"))
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
    """启动时校验必填密钥与目标表。"""
    _require("FEISHU_APP_ID")
    _require("FEISHU_APP_SECRET")
    _require("FEISHU_BASE_ID")
    require_tables("FEISHU_PARAM_TABLE_ID", "FEISHU_ENTRY_TABLE_ID")
