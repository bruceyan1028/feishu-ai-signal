"""生成真实每日情报简报，并将分析与简报结果写回飞书多维表。"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any

from . import (
    cluster,
    config,
    feishu,
    paper_fulltext,
    policy_document,
    process,
    report,
    rss,
    scrape,
    sources,
)

log = logging.getLogger("daily")
CN_TZ = timezone(timedelta(hours=8))
TOPIC_OPTIONS = {"AI", "LLM", "Agent", "RAG", "推理", "多模态", "开源", "硬件", "端侧", "监管", "融资", "产品", "其他"}
URGENCY_TO_TABLE = {"高": "High", "中": "Medium", "低": "Low"}
URGENCY_TO_CN = {value: key for key, value in URGENCY_TO_TABLE.items()}
PAPER_ANALYSIS_VERSION = 2


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 正文里常见的栏目抬头噪音，如「智东西 作者 | ZeR0 编辑 | 漠影」
_BYLINE_RE = re.compile(r"(?:作者|编译|编辑|撰文|责编|来源|文)\s*[|｜/:：]\s*\S{1,20}\s*")
_BYLINE_HEAD = 120
_EDGE_TOPIC_RE = re.compile(
    r"(?:端侧|端上|设备端|端云|"
    r"(?:手机|移动|车载|设备)端(?:模型|AI|智能|推理|部署|运行)|"
    r"边缘(?:AI|人工智能|模型|推理|计算|设备|平台)|"
    r"(?:模型|AI|智能体).{0,10}(?:手机|移动设备)|"
    r"(?:手机|移动设备).{0,10}(?:模型|AI|智能体|推理)|"
    r"on[- ]device|device[- ]side|"
    r"edge[- ](?:ai|inference|computing|device)|"
    r"(?:手机|移动|车载|端侧).{0,12}\bNPU\b|\bNPU\b.{0,12}(?:手机|移动|车载|端侧))",
    re.I,
)


def is_edge_signal(fields: dict[str, Any]) -> bool:
    """确定性识别端侧信号，避免完全依赖 LLM 自由选标签。"""
    text = "\n".join(
        str(scalar(fields.get(name)) or "")
        # 不扫整篇原文：聚合页、相关文章和泛泛提及会制造大量假阳性。
        # 标题与摘要足以表达这是否是一条以端侧为主题的信号。
        for name in ("标题", "中文标题", "中文摘要")
    )[:12000]
    return bool(_EDGE_TOPIC_RE.search(text))


def is_policy_signal(fields: dict[str, Any]) -> bool:
    source_id = str(scalar(fields.get("source_id")) or "").lower()
    return source_id.startswith("whitehouse-tech-")


def normalize_topics(fields: dict[str, Any], topics: list[Any]) -> list[str]:
    """过滤主题并强制补齐确定性专题；最多保留四项。"""
    result: list[str] = []
    for topic in topics:
        value = str(scalar(topic) or "")
        if value in TOPIC_OPTIONS and value not in result:
            result.append(value)
    if is_edge_signal(fields) and "端侧" not in result:
        result = [topic for topic in result if topic != "其他"]
        result.append("端侧")
    if is_policy_signal(fields) and "监管" not in result:
        result = [topic for topic in result if topic != "其他"]
        result.append("监管")
    if len(result) > 4:
        pinned = [topic for topic in ("端侧", "监管") if topic in result]
        result = [topic for topic in result if topic not in pinned][: 4 - len(pinned)] + pinned
    return result or ["其他"]


def analysis_requirement(
    *,
    is_paper: bool,
    is_social: bool,
    is_policy: bool = False,
    preserve_structure: bool = False,
) -> str:
    if is_paper:
        return """deep_analysis_cn 写 1000-1800 字；以学术证据密度为优先，不用行业新闻套话凑字数。
deep_analysis_cn 必须使用以下论文专用结构，每个标题独占一行，标题与正文之间换行，各节之间空一行：
【研究问题与核心结论】
说明论文试图解决什么问题、为何现有方法不足，并用可核验的一段话概括作者的核心结论。不要复述榜单介绍。

【方法与创新】
解释模型、算法、数据或系统设计；明确相对基线真正新增了什么，并区分架构创新、训练技巧和工程优化。

【实验设计与关键结果】
列出数据集、基线、评价指标、消融或对照设置以及最关键的数值结果。引用图表时写明 Figure/Table 编号或 PDF 页码，不得只写“显著提升”。

【证据强度与局限】
评价实验是否足以支持结论，检查样本范围、统计显著性、公平比较、数据泄漏、计算成本、失败案例和复现信息；未披露的内容明确写“论文未披露”。

【研究意义与后续问题】
说明这项工作改变了什么认识、对后续研究或工程验证有什么启示，并提出仍未解决的研究问题。不要输出“行动建议”、采购建议或泛化的业务待办。"""
    if is_social:
        return """deep_analysis_cn 写 250-500 字，使用以下结构：
【帖子信号】
说明账号明确披露了什么，保留数字、产品名和链接线索。

【价值与边界】
解释其潜在影响，并明确区分已披露事实与尚待验证的信息。

【跟进行动】
给出1-3条具体核验或跟进建议。"""
    if is_policy:
        return """deep_analysis_cn 写 900-1500 字；以政策原文和官方附件为依据，不用政治口号或新闻套话凑字数。
deep_analysis_cn 必须使用以下政策专用结构，每个标题独占一行，标题与正文之间换行，各节之间空一行：
【政策性质与效力】
明确这是报告建议、事实清单、预算指引、行政命令还是总统备忘录；说明哪些内容已经生效，哪些仍需机构后续执行。不得把建议性报告写成法律。

【核心措施与责任主体】
提取政策工具、资金或资源承诺、负责机构、适用对象和关键数字；同一发布页含多个附件时分别标明依据。

【时间表与执行机制】
列出明确期限、预算周期、行动计划、监督或验收方式；原文未给时间表时明确写“原文未披露”。

【对 AI 与产业的影响】
分析对模型、算力、数据、科研基础设施、企业和研究机构的直接与间接影响，并区分原文事实与分析判断。

【不确定性与跟踪点】
指出仍待预算、机构规则、招标、拨款或实施方案确认的部分，给出可核验的后续观察节点，不输出泛化行动清单。"""
    if preserve_structure:
        return """deep_analysis_cn 是对高质量原文的结构化精编，目标 700-1200 个中文字符，硬上限 1400 字。
必须沿用原文已有的小标题、论证顺序和核心观点，不得套用“核心内容 / 关键细节 / 价值与影响 / 行动建议”等统一模板。
只保留3-6个最有信息量的原文小节；允许整节删除重复、宣传、旁枝和低信息内容，但不得改变保留章节的先后顺序或原意。
每节压缩为1-2个短段，约120-220字；不要逐段复述，不要保留仅用于行文过渡的句子。
每个保留的小标题单独一行，写成“## 中文小标题 || 原文小标题”，下一行直接写精编正文；各节之间空一行。
中文小标题是对原文小标题的准确翻译或等价表达，不要改写成自己的概括；原文小标题本身就是中文时，直接写“## 原文小标题”，不要重复两遍。
原文中的事实与作者判断要保持可区分；有明显事实疑点时可在对应小节末尾用一句话克制标注，不另建固定的风险或行动章节。"""
    return """deep_analysis_cn 写 800-1400 字；短原文可缩至500字，但不要用空话凑字数。
deep_analysis_cn 必须使用以下结构，每个标题独占一行，标题与正文之间换行，各节之间空一行：
【核心内容】
交代事件或成果本身，保留关键数字、主体、时间、产品/模型名称。

【关键细节】
展开原理、功能、方法、实验结果、商业条款或落地方式；只写原文有依据的内容。

【价值与影响】
评价它相对现状带来的变化，对开发者、企业或行业意味着什么。

【局限与风险】
指出原文披露不足、适用边界、成本、安全、可复现性或营销偏差；无法确认时明确写“原文未披露”。

【行动建议】
给出2-4条具体、克制、可执行的验证或跟进建议。"""


def cjk_ratio(text: str) -> float:
    text = str(text or "").strip()
    if not text:
        return 0.0
    return len(_CJK_RE.findall(text)) / len(text)


def is_chinese_text(text: str) -> bool:
    return cjk_ratio(text) >= 0.15


_WP_TAIL_RE = re.compile(r"(?is)\n*The post\b.*?\bappeared first on\b[^\n]*$")


def clean_body(text: str, source: str = "") -> str:
    """去掉栏目抬头噪音并规整段落，供前端直接分段渲染。"""
    # 存量条目里残留着 &nbsp;/&#8217; 之类实体，展示前统一解码
    body = unescape(str(text or "")).strip()
    if not body:
        return ""
    # 抬头是中文媒体的写法且总在第一句之前，只在这段内清理，
    # 避免误删英文正文或后文里的「来源：」等正常表述
    first_stop = re.search(r"[。！？]", body[:_BYLINE_HEAD])
    cut = first_stop.start() if first_stop else min(len(body), _BYLINE_HEAD)
    head, tail = body[:cut], body[cut:]
    if cjk_ratio(head) >= 0.3:
        head = _BYLINE_RE.sub("", head)
        name = str(source or "").strip()
        if name and head.lstrip().startswith(name):
            head = head.lstrip()[len(name):]
    body = f"{head.lstrip()}{tail}"
    body = re.sub(r"[ \t\u00a0\u3000]+", " ", body)
    body = re.sub(r" *\n *", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    # 存量条目的结尾还留着 WordPress 的版权尾巴，采集端已不再写入，展示前兜底去掉
    return _WP_TAIL_RE.sub("", body).strip()


_TRANSLATE_PROMPT = """把下面的文章正文忠实翻译成简体中文。
要求：
1. 逐段翻译，保留原文段落划分，段落之间用空行分隔；
2. 只翻译，不要概括、不要删减、不要添加任何评论或总结；
3. 公司名、产品名、模型名、论文名等专有名词保留英文原名；
4. 形如「单元格 | 单元格」的行是原文表格，逐行翻译并原样保留「|」分隔与换行，不要合并成段落；
5. 形如「## 小标题」的行必须保留“## ”标记和原有层级位置；
6. 只输出严格 JSON：{{"body_cn": "翻译后的正文"}}

正文：
{snippet}"""

TRANSLATED_CHARS_FIELD = "译文覆盖字数"
# 早先入库的条目没有覆盖字数记录，按当时的默认上限回推，避免整表无谓重译
_LEGACY_TRANSLATE_LIMIT = 3000


def split_for_translation(text: str, chunk: int | None = None) -> list[str]:
    """按段落边界切成适合单次调用的片段；单段超长时退回句子边界。"""
    size = max(1, chunk or config.BODY_TRANSLATE_CHUNK)
    parts: list[str] = []
    buffer = ""
    for para in str(text or "").split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if not buffer:
            buffer = para
        elif len(buffer) + 2 + len(para) <= size:
            buffer = f"{buffer}\n\n{para}"
        else:
            parts.append(buffer)
            buffer = para
        while len(buffer) > size:
            head = scrape.cut_on_boundary(buffer, size)
            if not head or head == buffer:
                head = buffer[:size]
            parts.append(head)
            buffer = buffer[len(head) :].strip()
    if buffer:
        parts.append(buffer)
    return parts


def _translate_chunk(snippet: str) -> str:
    try:
        raw = report._llm_json(_TRANSLATE_PROMPT.format(snippet=snippet))
    except Exception as exc:  # noqa: BLE001
        log.warning("正文翻译失败，回退原文：%s", exc)
        return ""
    return str(raw.get("body_cn") or "").strip()


def translate_body(text: str, limit: int | None = None) -> tuple[str, int]:
    """把英文正文忠实译成中文（只译不改写），返回 (译文, 已覆盖的原文字符数)。

    必须分段：一次塞进上万字符，模型会自行压缩甚至半途收尾，
    读者看到的就是断在句子中间的正文。某段失败时只保留已成功的前缀，
    宁可短一截，也不能让正文中间出现空洞。
    """
    cap = limit or config.BODY_TRANSLATE_LIMIT
    snippet = str(text or "").strip()[:cap]
    chunks = split_for_translation(snippet)
    if not chunks:
        return "", 0
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
        pieces = list(pool.map(_translate_chunk, chunks))
    done: list[str] = []
    for piece in pieces:
        if not piece:
            break
        done.append(piece)
    if not done:
        return "", 0
    covered = (
        len(snippet) if len(done) == len(chunks) else sum(len(c) for c in chunks[: len(done)])
    )
    return clean_body("\n\n".join(done)), covered


def translate_limit_for(priority: str = "P2", impact: Any = 0) -> int:
    """P0 来源与高影响分条目走全译档，其余用默认档控制翻译成本。"""
    try:
        score = float(impact or 0)
    except (TypeError, ValueError):
        score = 0.0
    if str(priority or "").upper() == "P0" or score >= config.BODY_TRANSLATE_FULL_IMPACT:
        return config.BODY_TRANSLATE_LIMIT_FULL
    return config.BODY_TRANSLATE_LIMIT


def _translated_chars(fields: dict[str, Any]) -> int:
    """这条译文覆盖了原文前多少字符。"""
    try:
        chars = int(float(scalar(fields.get(TRANSLATED_CHARS_FIELD)) or 0))
    except (TypeError, ValueError):
        chars = 0
    if chars:
        return chars
    return _LEGACY_TRANSLATE_LIMIT if str(scalar(fields.get("中文正文")) or "").strip() else 0


def display_body(fields: dict[str, Any]) -> dict[str, Any]:
    """给前端的正文：中文源直接用原文，英文源用缓存译文。"""
    source = str(scalar(fields.get("来源")) or "")
    raw = clean_body(str(scalar(fields.get("原文")) or ""), source)
    translated = clean_body(str(scalar(fields.get("中文正文")) or ""), source)
    if translated:
        # 译文按上限截断过，原文更长时告诉前端还有后续内容
        return {"body": translated, "bodyTruncated": len(raw) > _translated_chars(fields)}
    return {"body": raw, "bodyTruncated": False}


def brief_bullet_title(text: str, suggested: str = "") -> str:
    """确保简报标题表达具体结论，而不是“要点 1”一类占位文案。"""
    title = suggested.strip()
    if title and not re.fullmatch(r"要点\s*\d*", title):
        return title
    for separator in ("：", ":", "，", "。", "；", ";"):
        if separator in text:
            return text.split(separator, 1)[0].strip()[:28]
    return text.strip()[:28]


def referenced_signal_title(signals: list[dict[str, Any]], refs: list[int]) -> str:
    """取首条引用信号自身的标题。

    核心要点若另起一个二次总结的标题，同一条新闻在「全部信号」里会显示成另一个
    说法，读者会以为是两条不同的新闻。
    """
    for ref in refs:
        if not 1 <= ref <= len(signals):
            continue
        signal = signals[ref - 1]
        title = str(signal.get("titleCn") or signal.get("title") or "").strip()
        if title:
            return title
    return ""


def scalar(value: Any) -> Any:
    if isinstance(value, list):
        if not value:
            return ""
        return scalar(value[0])
    if isinstance(value, dict):
        return value.get("text") or value.get("link") or value.get("name") or ""
    return value if value is not None else ""


def link(value: Any) -> str:
    if isinstance(value, list):
        return link(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "")
    return str(value or "")


def media_assets(value: Any) -> dict[str, Any]:
    raw = scalar(value)
    if not raw:
        return {"images": [], "videos": []}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {"images": [], "videos": []}
    except (TypeError, ValueError):
        return {"images": [], "videos": []}


def content_type(fields: dict[str, Any]) -> str:
    """从来源字段与链接识别需要显式展示的内容载体。"""
    source = str(scalar(fields.get("来源")) or "")
    source_type = str(scalar(fields.get("来源类型")) or "")
    url = link(fields.get("链接"))
    text = f"{source} {source_type} {url}".lower()
    # 来源类型已是载体分类时直接沿用（社交媒体帖子保持前端文案）
    if source_type in ("论文", "视频", "公众号", "播客", "Github热榜"):
        return source_type
    if source_type in ("社交媒体", "Social"):
        return "社交媒体帖子"
    if source_type.lower() in ("github", "github-trending") or "github.com" in text:
        return "Github热榜"
    if any(token in text for token in ("mp.weixin.qq.com", "weixin.qq.com", "微信公众号", "公众号")):
        return "公众号"
    if any(token in text for token in ("youtube.com", "youtu.be", "bilibili.com", "vimeo.com", "视频")):
        return "视频"
    if any(token in text for token in ("arxiv.org", "openreview.net", "doi.org", "学术论文", "论文")):
        return "论文"
    if source_type.lower() == "social" or any(
        token in text for token in ("x.com/", "twitter.com/", "weibo.com/", "linkedin.com/", "社交媒体")
    ):
        return "社交媒体帖子"
    # 普通新闻/博客也要给出显式载体，否则前端拿不到类型标签
    return "文章"


_EDITORIAL_HEADING_RE = re.compile(r"(?m)^##\s+([^\n]{2,80})\s*$")
_EDITORIAL_NOISE_HEADING_RE = re.compile(
    r"^(?:相关文章|推荐阅读|热门|标签|关于作者|联系我们|相关阅读|更多内容)$"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def editorial_headings_need_cn(text: str) -> bool:
    """沿用原文结构时，英文小标题必须配中文；缺中文的存量解读要重新生成。"""
    return any(
        not _CJK_RE.search(match.group(1))
        for match in _EDITORIAL_HEADING_RE.finditer(text or "")
    )


def editorial_structure_mode(fields: dict[str, Any], text: str = "") -> bool:
    """判断原文是否已具备值得沿用的长文结构，而不是按来源类型一刀切。"""
    if content_type(fields) not in {"文章", "公众号"} or is_policy_signal(fields):
        return False
    body = clean_body(
        text or str(scalar(fields.get("原文")) or ""),
        str(scalar(fields.get("来源")) or ""),
    )
    if len(body) < 1800:
        return False
    headings = [
        title.strip()
        for title in _EDITORIAL_HEADING_RE.findall(body)
        if not _EDITORIAL_NOISE_HEADING_RE.match(title.strip())
    ]
    if len(set(headings)) < 3:
        return False
    # 至少三个章节应有实质正文，避免把相关推荐/导航列表误认成高质量结构。
    sections = re.split(r"(?m)^##\s+[^\n]+\s*$", body)[1:]
    substantive = sum(len(section.strip()) >= 180 for section in sections)
    return substantive >= 3


def compact_editorial_analysis(text: str, max_chars: int = 1400) -> str:
    """为原结构精编增加硬长度收口，避免“精编”退化为搬运全文。"""
    raw = clean_body(text)
    all_matches = list(_EDITORIAL_HEADING_RE.finditer(raw))
    if len(all_matches) < 3 or len(raw) <= max_chars:
        return raw
    matches = all_matches[:6]
    heading_chars = sum(len(match.group(1)) + 5 for match in matches)
    section_budget = max(120, (max_chars - heading_chars) // len(matches))
    compacted: list[str] = []
    for index, match in enumerate(matches):
        end = all_matches[index + 1].start() if index + 1 < len(all_matches) else len(raw)
        body = raw[match.end() : end].strip()
        body = scrape.cut_on_boundary(body, section_budget)
        if body:
            compacted.append(f"## {match.group(1).strip()}\n{body}")
    return "\n\n".join(compacted)[:max_chars].rstrip()


def date_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp() * 1000)


def today_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def _priority_map(param_records: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in param_records:
        fields = record.get("fields") or {}
        source_id = str(sources.cell(fields.get("source_id")) or "")
        if source_id:
            result[source_id] = str(sources.cell(fields.get("priority")) or "P2")
    return result


def analysis_failure_is_systemic(failed: int, attempted: int) -> bool:
    """判断本轮分析失败是「个别抽风」还是「那头整个挂了」。

    个别失败跳过即可，丢一条信号远好过丢一整天的推送；但过半失败通常是配额、
    鉴权或网关问题，这时推出去的简报是残缺的，宁可让流水线红掉让人来看。
    """
    return bool(attempted) and failed > attempted / 2


def _active_source_ids(param_records: list[dict[str, Any]]) -> set[str]:
    # 简报候选来源白名单：active 的正式采集源。
    # 让抓取型来源也能进入每日简报，而不只是 RSS。
    return {
        str(sources.cell((record.get("fields") or {}).get("source_id")) or "")
        for record in param_records
        if sources.cell((record.get("fields") or {}).get("status")) == "active"
        and sources.cell((record.get("fields") or {}).get("fetch_method")) in {
            "RSS",
            "Scrape",
            "Media",
            "Social",
            "Podcast",
        }
    } - {""}


def _lookback_hours_map(param_records: list[dict[str, Any]]) -> dict[str, int]:
    """读取每个源自己的时间窗；简报端也要执行，不能只依赖采集时的一次过滤。"""
    result: dict[str, int] = {}
    for record in param_records:
        fields = record.get("fields") or {}
        source_id = str(sources.cell(fields.get("source_id")) or "")
        if source_id:
            result[source_id] = sources.parse_lookback_hours(
                sources.cell(fields.get("lookback_window"))
            )
    return result


def select_candidates(
    records: list[dict[str, Any]],
    priorities: dict[str, str],
    allowed_source_ids: set[str] | None = None,
    lookback_hours: dict[str, int] | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """取仍在来源时间窗内的近七日信号；官方优先，并限制各内容类型占比。"""
    now = now or datetime.now(timezone.utc)
    future_limit_ms = int((now + timedelta(days=2)).timestamp() * 1000)
    candidates = []
    for record in records:
        fields = record.get("fields") or {}
        source_id = str(scalar(fields.get("source_id")) or "")
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            continue
        # 简报时效只能依据真实发布时间，采集时间不能替代，否则旧页面会伪装成新内容。
        stamp = int(float(scalar(fields.get("发布时间")) or 0))
        source_hours = max(1, int((lookback_hours or {}).get(source_id, 7 * 24)))
        effective_hours = min(7 * 24, source_hours)
        cutoff_ms = int((now - timedelta(hours=effective_hours)).timestamp() * 1000)
        if stamp < cutoff_ms or stamp > future_limit_ms:
            continue
        if process.is_whitehouse_source(source_id) and not process.is_ai_policy_text(
            scalar(fields.get("标题")),
            scalar(fields.get("原文")),
            scalar(fields.get("中文摘要")),
        ):
            continue
        candidates.append(
            {
                "record_id": record.get("record_id"),
                "fields": fields,
                "source_id": source_id,
                "priority": priorities.get(source_id, "P2"),
                "stamp": stamp,
            }
        )
    def _is_arxiv(item: dict[str, Any]) -> bool:
        return item["source_id"].startswith("arxiv-") or "arxiv.org/" in link(
            item["fields"].get("链接")
        )

    def _eff_quality(item: dict[str, Any]) -> float:
        q = float(scalar(item["fields"].get("质量分")) or 0)
        return q * config.ARXIV_QUALITY_WEIGHT if _is_arxiv(item) else q

    candidates.sort(
        key=lambda item: (
            {"P0": 0, "P1": 1, "P2": 2}.get(item["priority"], 3),
            -_eff_quality(item),
            -item["stamp"],
        )
    )
    total_limit = limit or config.DAILY_CANDIDATE_LIMIT
    # P0 源足够多时会独占整个候选池，先为非 P0 源留出名额，
    # 否则中文媒体、实验室等来源永远排不到。
    non_p0_available = sum(1 for item in candidates if item["priority"] != "P0")
    p0_limit = max(0, total_limit - min(config.DAILY_MIN_NON_P0, non_p0_available))
    selected: list[dict[str, Any]] = []
    arxiv_count = 0
    paper_count = 0
    video_count = 0
    podcast_count = 0
    github_count = 0
    p0_count = 0
    per_source: Counter[str] = Counter()
    for item in candidates:
        is_arxiv = _is_arxiv(item)
        item_type = content_type(item["fields"])
        is_paper = item_type == "论文"
        is_video = item_type == "视频"
        is_podcast = item_type == "播客"
        is_github = item_type == "Github热榜"
        is_p0 = item["priority"] == "P0"
        # 论文总数硬上限：避免论文挤占「快速读新闻」的名额
        if is_paper and paper_count >= config.DAILY_MAX_PAPERS:
            continue
        if is_video and video_count >= config.DAILY_MAX_VIDEOS:
            continue
        if is_podcast and podcast_count >= config.DAILY_MAX_PODCASTS:
            continue
        if is_github and github_count >= config.DAILY_MAX_GITHUB:
            continue
        if is_arxiv and arxiv_count >= config.MAX_ARXIV_ITEMS:
            continue
        if per_source[item["source_id"]] >= config.DAILY_MAX_PER_SOURCE:
            continue
        if is_p0 and p0_count >= p0_limit:
            continue
        selected.append(item)
        arxiv_count += int(is_arxiv)
        paper_count += int(is_paper)
        video_count += int(is_video)
        podcast_count += int(is_podcast)
        github_count += int(is_github)
        p0_count += int(is_p0)
        per_source[item["source_id"]] += 1
        if len(selected) >= total_limit:
            break
    return selected


def analyze_signal(fields: dict[str, Any]) -> dict[str, Any]:
    is_paper = content_type(fields) == "论文"
    is_social = content_type(fields) == "社交媒体帖子"
    is_policy = is_policy_signal(fields)
    preserve_structure = editorial_structure_mode(fields)
    paper_extra = ""
    policy_extra = ""
    image_urls: list[str] = []
    analysis_text = clean_body(
        str(scalar(fields.get("原文")) or ""),
        str(scalar(fields.get("来源")) or ""),
    )[:12000]
    paper_metrics: dict[str, Any] = {}
    paper_full_text: dict[str, Any] = {}
    if is_paper:
        try:
            paper_metrics = json.loads(str(scalar(fields.get("论文指标")) or "{}"))
        except (TypeError, ValueError):
            paper_metrics = {}
        if not isinstance(paper_metrics, dict):
            paper_metrics = {}
        full_text = paper_metrics.get("full_text") or {}
        paper_full_text = full_text if isinstance(full_text, dict) else {}
        evidence = paper_fulltext.evidence_text(paper_full_text)
        if evidence:
            analysis_text = evidence
        if paper_full_text.get("source") == "pdf":
            image_urls = paper_fulltext.render_visual_pages(
                str(paper_full_text.get("pdf_url") or ""),
                list(paper_full_text.get("visual_pages") or []),
            )
        paper_extra = (
            "额外字段：rigor/novelty_paper/relevance（0-100整数，分别表示方法严谨度、学术新颖度、"
            "与产业/工程实践的相关度）。\n"
            "论文分析必须以 PDF 分章节证据为主；若附带图表页图像，必须读取坐标轴、图例、"
            "表格数值和图注，并在【实验设计与关键结果】中明确写出可核验的图表结论及 PDF 页码。"
            "不得把 HF/PwC 的介绍文案当作论文实验结果。\n"
        )
    policy_documents = [
        document
        for document in media_assets(fields.get("媒体资源")).get("documents") or []
        if isinstance(document, dict)
    ]
    if is_policy:
        for document in policy_documents:
            if document.get("fullTextSource") != "pdf":
                continue
            try:
                image_urls.extend(
                    policy_document.render_visual_pages(
                        str(document.get("url") or ""),
                        list(document.get("visualPages") or []),
                        limit=max(0, 4 - len(image_urls)),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单个附件图表失败不退回网页总结
                log.warning("政策 PDF 图表读取失败 %s: %s", document.get("url"), exc)
            if len(image_urls) >= 4:
                break
        policy_extra = (
            "政策分析必须以官方 PDF 正文为主，发布页仅用于确认发布时间和文件性质。"
            "若附带图表图像，必须读取坐标轴、图例、数值和图注，并在对应章节写明 PDF 页码；"
            "不得只复述发布页摘要。\n"
        )
    analysis_format = analysis_requirement(
        is_paper=is_paper,
        is_social=is_social,
        is_policy=is_policy,
        preserve_structure=preserve_structure,
    )
    pdf_document_count = sum(
        document.get("fullTextSource") == "pdf" for document in policy_documents
    )
    policy_basis = (
        f"{pdf_document_count} 个 White House 官方 PDF 的正文及图表"
        if is_policy and pdf_document_count
        else "White House 官方发布页正文"
        if is_policy
        else "网页正文或摘要"
    )
    prompt = f"""你是资深 AI 行业分析师。只依据给定原文输出严格 JSON，不得虚构。
字段：title_cn（准确简洁的中文标题）、summary_cn（中文1-2句）、why（中文1句）、impact/novelty/actionability（0-100整数）、
urgency（高/中/低）、topics（从 AI、LLM、Agent、RAG、推理、多模态、开源、硬件、端侧、监管、融资、产品、其他中选2-4个；
手机/设备本地运行、NPU、边缘推理、on-device 模型必须包含“端侧”）、
deep_analysis_cn（中文分析）。
论文的 actionability 表示可复现、可验证和可转化价值，不要求正文给出行动清单。
{analysis_format}
{paper_extra}{policy_extra}标题：{scalar(fields.get("标题"))}
来源：{scalar(fields.get("来源"))}
分类：{sources.normalize_category(scalar(fields.get("分类")) or "其他")}
分析依据：{"论文 PDF 全文及图表页" if is_paper and paper_full_text.get("source") == "pdf" else policy_basis}
原文/论文证据：{analysis_text}"""
    raw = report._llm_json(prompt, image_urls=image_urls)
    topics = normalize_topics(fields, list(raw.get("topics") or []))
    deep_analysis = str(raw.get("deep_analysis_cn") or "").strip()
    if preserve_structure:
        deep_analysis = compact_editorial_analysis(deep_analysis)
    result = {
        "title_cn": str(raw.get("title_cn") or scalar(fields.get("标题"))).strip(),
        "summary_cn": str(raw.get("summary_cn") or "").strip(),
        "deep_analysis_cn": deep_analysis,
        "why": str(raw.get("why") or "").strip(),
        "impact": max(0, min(100, int(raw.get("impact") or 0))),
        "novelty": max(0, min(100, int(raw.get("novelty") or 0))),
        "actionability": max(0, min(100, int(raw.get("actionability") or 0))),
        "urgency": str(raw.get("urgency")) if raw.get("urgency") in URGENCY_TO_TABLE else "中",
        "topics": topics or ["其他"],
    }
    if preserve_structure:
        result["editorial_structure"] = "source"
    if is_paper:
        rigor = max(0, min(100, int(raw.get("rigor") or raw.get("novelty") or 0)))
        novelty_paper = max(0, min(100, int(raw.get("novelty_paper") or raw.get("novelty") or 0)))
        relevance = max(0, min(100, int(raw.get("relevance") or raw.get("actionability") or 0)))
        result["rigor"] = rigor
        result["novelty_paper"] = novelty_paper
        result["relevance"] = relevance
        result["llm_quality"] = round((rigor + novelty_paper + relevance) / 3, 1)
        result["evidence_version"] = int(
            paper_full_text.get("version") or 0
        )
        result["analysis_version"] = PAPER_ANALYSIS_VERSION
    return result


def _signal_from_fields(record_id: str, fields: dict[str, Any], analysis: dict[str, Any], *, priority: str = "P2", tier: str = "") -> dict[str, Any]:
    published = int(float(scalar(fields.get("发布时间")) or 0))
    media = media_assets(fields.get("媒体资源"))
    try:
        paper_metrics = json.loads(str(scalar(fields.get("论文指标")) or "{}"))
    except (TypeError, ValueError):
        paper_metrics = {}
    if not isinstance(paper_metrics, dict):
        paper_metrics = {}
    full_text = paper_metrics.get("full_text") or {}
    if not isinstance(full_text, dict):
        full_text = {}
    signal = {
        "recordId": record_id,
        "sourceId": str(scalar(fields.get("source_id")) or ""),
        "title": str(scalar(fields.get("标题"))),
        "titleCn": analysis["title_cn"],
        "source": str(scalar(fields.get("来源"))),
        "url": link(fields.get("链接")),
        "category": sources.normalize_category(scalar(fields.get("分类")) or "其他"),
        "contentType": content_type(fields),
        "tier": tier or str(scalar(fields.get("层级")) or ""),
        "priority": priority,
        "publishedDate": datetime.fromtimestamp(published / 1000, CN_TZ).strftime("%Y-%m-%d") if published else "",
        "summary": analysis["summary_cn"],
        "why": analysis["why"],
        "impact": analysis["impact"],
        "novelty": analysis["novelty"],
        "actionability": analysis["actionability"],
        "urgency": analysis["urgency"],
        "tags": analysis["topics"],
        "imageUrl": link(fields.get("图片链接")),
        "mediaAssets": media,
        "topComments": media.get("topComments") or [],
        "deepAnalysis": str(analysis.get("deep_analysis_cn") or "").strip(),
        "editorialStructure": (
            "source"
            if analysis.get("editorial_structure") == "source"
            or editorial_structure_mode(fields)
            else ""
        ),
    }
    if full_text:
        signal.update(
            {
                "pdfUrl": str(full_text.get("pdf_url") or ""),
                "paperFullTextSource": str(full_text.get("source") or ""),
                "paperPages": int(full_text.get("pages") or 0),
                "paperCaptions": list(full_text.get("captions") or [])[:24],
                "paperVisualPages": list(full_text.get("visual_pages") or [])[:4],
            }
        )
    return signal


def _ensure_body_cn(
    fields: dict[str, Any], *, priority: str = "P2", impact: Any = 0
) -> dict[str, Any]:
    """英文正文缺译文时翻译一次，写回 fields 并返回待落库字段；无需翻译返回空 dict。

    已有译文但只覆盖到更低档上限的条目（例如这次升进了全译档）会补译，
    否则读者永远停在上一次的截断处。
    """
    raw = clean_body(str(scalar(fields.get("原文")) or ""), str(scalar(fields.get("来源")) or ""))
    if len(raw) < 80 or is_chinese_text(raw):
        return {}
    limit = translate_limit_for(priority, impact)
    cached = str(scalar(fields.get("中文正文")) or "").strip()
    if cached and _translated_chars(fields) >= min(len(raw), limit):
        return {}
    translated, covered = translate_body(raw, limit)
    if not translated:
        return {}
    fields["中文正文"] = translated
    fields[TRANSLATED_CHARS_FIELD] = covered
    return {"中文正文": translated, TRANSLATED_CHARS_FIELD: covered}


def _ensure_deep_analysis(
    fields: dict[str, Any], analysis: dict[str, Any]
) -> dict[str, Any]:
    """为已分析的存量条目补齐详情页深度解读，新条目由 analyze_signal 一次生成。"""
    preserve_structure = editorial_structure_mode(fields)
    cached = str(
        analysis.get("deep_analysis_cn")
        or scalar(fields.get("AI深度解读"))
        or ""
    ).strip()
    if cached and (
        not preserve_structure
        or (
            _EDITORIAL_HEADING_RE.search(cached)
            and not editorial_headings_need_cn(cached)
        )
    ):
        analysis["deep_analysis_cn"] = cached
        if preserve_structure:
            analysis["editorial_structure"] = "source"
        return {}
    source = str(scalar(fields.get("来源")) or "")
    raw_text = clean_body(str(scalar(fields.get("原文")) or ""), source)
    is_paper = content_type(fields) == "论文"
    image_urls: list[str] = []
    if is_paper:
        try:
            metrics = json.loads(str(scalar(fields.get("论文指标")) or "{}"))
        except (TypeError, ValueError):
            metrics = {}
        full_text = metrics.get("full_text") if isinstance(metrics, dict) else {}
        full_text = full_text if isinstance(full_text, dict) else {}
        evidence = paper_fulltext.evidence_text(full_text)
        if evidence:
            raw_text = evidence
        if full_text.get("source") == "pdf":
            image_urls = paper_fulltext.render_visual_pages(
                str(full_text.get("pdf_url") or ""),
                list(full_text.get("visual_pages") or []),
            )
    elif is_policy_signal(fields):
        documents = [
            document
            for document in media_assets(fields.get("媒体资源")).get("documents") or []
            if isinstance(document, dict) and document.get("fullTextSource") == "pdf"
        ]
        for document in documents:
            try:
                image_urls.extend(
                    policy_document.render_visual_pages(
                        str(document.get("url") or ""),
                        list(document.get("visualPages") or []),
                        limit=max(0, 4 - len(image_urls)),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("存量政策 PDF 图表读取失败 %s: %s", document.get("url"), exc)
            if len(image_urls) >= 4:
                break
    if len(raw_text) < 80:
        return {}
    format_requirement = analysis_requirement(
        is_paper=is_paper,
        is_social=False,
        is_policy=is_policy_signal(fields),
        preserve_structure=preserve_structure,
    )
    prompt = f"""你是资深 AI 行业分析师。只依据给定原文撰写中文深度解读，输出严格 JSON：
{{"deep_analysis_cn":"..."}}。

要求：
1. 保留原文中的关键数字、技术机制、实验结果和适用条件；
2. 区分原文事实与分析判断，不得虚构；资料不足时明确写“原文未披露”或“论文未披露”；
3. 严格执行以下内容结构：
{format_requirement}

标题：{scalar(fields.get("标题"))}
来源：{source}
已有短摘要：{analysis.get("summary_cn") or ""}
为何重要：{analysis.get("why") or ""}
原文：
{raw_text[:12000]}"""
    try:
        result = report._llm_json(prompt, image_urls=image_urls)
    except Exception as exc:  # noqa: BLE001
        log.warning("深度解读生成失败：%s", exc)
        return {}
    deep = str(result.get("deep_analysis_cn") or "").strip()
    if not deep:
        return {}
    if preserve_structure:
        deep = compact_editorial_analysis(deep)
    analysis["deep_analysis_cn"] = deep
    if preserve_structure:
        analysis["editorial_structure"] = "source"
    fields["AI深度解读"] = deep
    return {"AI深度解读": deep}


def _existing_analysis(fields: dict[str, Any]) -> dict[str, Any] | None:
    if scalar(fields.get("状态")) != "已分析" or not scalar(fields.get("中文摘要")):
        return None
    try:
        metrics = json.loads(str(scalar(fields.get("论文指标")) or "{}"))
    except (TypeError, ValueError):
        metrics = {}
    if isinstance(metrics, dict):
        full_text = metrics.get("full_text") or {}
        llm = metrics.get("llm") or {}
        if (
            content_type(fields) == "论文"
            and int((llm if isinstance(llm, dict) else {}).get("analysis_version") or 0)
            < PAPER_ANALYSIS_VERSION
        ):
            return None
        if (
            isinstance(full_text, dict)
            and full_text.get("source") == "pdf"
            and int((llm if isinstance(llm, dict) else {}).get("evidence_version") or 0)
            < int(full_text.get("version") or 0)
        ):
            return None
    table_urgency = str(scalar(fields.get("紧迫度")) or "Medium")
    topics = fields.get("主题") if isinstance(fields.get("主题"), list) else []
    return {
        "title_cn": str(scalar(fields.get("中文标题")) or scalar(fields.get("标题"))),
        "summary_cn": str(scalar(fields.get("中文摘要"))),
        "deep_analysis_cn": str(scalar(fields.get("AI深度解读")) or ""),
        "why": str(scalar(fields.get("为何重要"))),
        "impact": int(float(scalar(fields.get("影响分")) or 0)),
        "novelty": int(float(scalar(fields.get("新颖度")) or 0)),
        "actionability": int(float(scalar(fields.get("可行动性")) or 0)),
        "urgency": URGENCY_TO_CN.get(table_urgency, "中"),
        "topics": [str(scalar(x)) for x in topics] or ["其他"],
    }


def _upsert_brief(token: str, table_id: str, payload: dict[str, Any]) -> str:
    brief_id = payload["date"]
    fields = {
        "简报ID": brief_id,
        "简报日期": date_ms(brief_id),
        "简报标题": payload["title"],
        "导语": payload["intro"],
        "关键要点": json.dumps(payload["bullets"], ensure_ascii=False),
        "信号记录ID": json.dumps([s["recordId"] for s in payload["signals"]], ensure_ascii=False),
        "状态": "已发布",
        "网页路径": f"/?date={brief_id}",
    }
    existing = feishu.read_all_records_with_ids(token, table_id, ["简报ID"])
    match = next((r for r in existing if str(scalar(r["fields"].get("简报ID"))) == brief_id), None)
    if match:
        feishu.update_record(token, table_id, match["record_id"], fields)
        return str(match["record_id"])
    fields["发送状态"] = "待发送"
    return str(feishu.create_record(token, table_id, fields).get("record_id") or "")


def balance_output_signals(ranked: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """保留综合排序，同时保证合格视频有最低曝光且不突破候选上限。"""
    selected = list(ranked[:limit])
    video_pool = [item for item in ranked if item.get("contentType") == "视频"]
    target = min(config.DAILY_MIN_VIDEOS, len(video_pool), limit)
    present = sum(item.get("contentType") == "视频" for item in selected)
    for video_item in video_pool:
        if present >= target:
            break
        if video_item in selected:
            continue
        replace_at = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if selected[index].get("contentType") != "视频"
            ),
            None,
        )
        if replace_at is None:
            break
        selected[replace_at] = video_item
        present += 1
    chosen = {str(item.get("recordId") or id(item)) for item in selected}
    return [item for item in ranked if str(item.get("recordId") or id(item)) in chosen][:limit]


def generate(day: str | None = None) -> dict[str, Any]:
    if not config.LLM_API_KEY:
        raise config.ConfigError("生成真实简报需要 LLM_API_KEY")
    token = feishu.get_tenant_access_token()
    feishu.ensure_entry_enrichment_fields(token)
    feishu.ensure_select_option(token, config.FEISHU_ENTRY_TABLE_ID, "主题", "端侧")
    params = feishu.read_param_records(token)
    entries = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    priorities = _priority_map(params)
    candidates = select_candidates(
        entries,
        priorities,
        _active_source_ids(params),
        _lookback_hours_map(params),
    )
    if not candidates:
        raise RuntimeError("近七日没有可用于简报的信号")

    # 同事件折叠：标题近似者只保留最优主条目进分析，其它源留给事件聚合
    candidates = cluster.collapse_for_brief(
        candidates,
        threshold=0.85,
        limit=config.DAILY_CANDIDATE_LIMIT,
    )
    log.info("同事件折叠后候选 %d 条", len(candidates))

    updates: list[dict[str, Any]] = []
    analyzed: list[dict[str, Any]] = []
    attempted = 0
    failed: list[str] = []
    for index, item in enumerate(candidates, 1):
        fields = item["fields"]
        analysis = _existing_analysis(fields)
        update_fields: dict[str, Any] = {}
        if analysis is None:
            log.info("分析 %d/%d: %s", index, len(candidates), scalar(fields.get("标题")))
            attempted += 1
            try:
                analysis = analyze_signal(fields)
            except Exception as exc:
                # 单条信号的 LLM 调用失败不该让整份简报作废：这一步跑在几十分钟的
                # 采集之后，为一条抓不到的解读丢掉当天全部推送，代价完全不成比例。
                title = str(scalar(fields.get("标题")) or item.get("record_id") or "")
                log.warning("分析失败，跳过该信号: %s (%s)", title, exc)
                failed.append(title)
                continue
            update_fields = {
                "中文标题": analysis["title_cn"],
                "中文摘要": analysis["summary_cn"],
                "AI深度解读": analysis.get("deep_analysis_cn") or "",
                "为何重要": analysis["why"],
                "影响分": analysis["impact"],
                "新颖度": analysis["novelty"],
                "可行动性": analysis["actionability"],
                "紧迫度": URGENCY_TO_TABLE[analysis["urgency"]],
                "主题": analysis["topics"],
                "状态": "已分析",
            }
            if analysis.get("llm_quality") is not None:
                base_q = float(scalar(fields.get("质量分")) or 0)
                final_q = round(0.6 * base_q + 0.4 * float(analysis["llm_quality"]), 1) if base_q else float(analysis["llm_quality"])
                update_fields["质量分"] = final_q
                analysis["final_quality"] = final_q
                try:
                    metrics = json.loads(str(scalar(fields.get("论文指标")) or "{}") or "{}")
                except (TypeError, ValueError):
                    metrics = {}
                if not isinstance(metrics, dict):
                    metrics = {}
                metrics["llm"] = {
                    "rigor": analysis.get("rigor"),
                    "novelty_paper": analysis.get("novelty_paper"),
                    "relevance": analysis.get("relevance"),
                    "llm_quality": analysis.get("llm_quality"),
                    "evidence_version": analysis.get("evidence_version") or 0,
                    "analysis_version": analysis.get("analysis_version") or 0,
                }
                metrics["quality_score_final"] = final_q
                update_fields["论文指标"] = json.dumps(metrics, ensure_ascii=False)
        normalized_topics = normalize_topics(fields, list(analysis.get("topics") or []))
        if normalized_topics != list(analysis.get("topics") or []):
            analysis["topics"] = normalized_topics
        stored_topics = [
            str(scalar(topic))
            for topic in (fields.get("主题") if isinstance(fields.get("主题"), list) else [])
        ]
        if normalized_topics != stored_topics:
            update_fields["主题"] = normalized_topics
        # 详情页展示深度解读而不是整篇译文；存量条目在首次入选时补齐。
        update_fields.update(_ensure_deep_analysis(fields, analysis))
        if update_fields:
            updates.append(
                {
                    "record_id": item["record_id"],
                    "fields": update_fields,
                }
            )
            if len(updates) >= 3:
                feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)
                updates.clear()
        signal = _signal_from_fields(
            str(item["record_id"]),
            fields,
            analysis,
            priority=str(item.get("priority") or priorities.get(item.get("source_id") or "", "P2")),
            tier=str(item.get("tier") or scalar(fields.get("层级")) or ""),
        )
        # 把折叠掉的同事件其它源转成可展示 peers
        peers = []
        for peer in item.get("eventPeers") or []:
            pf = peer.get("fields") or {}
            peer_analysis = _existing_analysis(pf) or {
                "title_cn": str(scalar(pf.get("中文标题")) or scalar(pf.get("标题")) or peer.get("titleCn") or peer.get("title") or ""),
                "summary_cn": str(scalar(pf.get("中文摘要")) or ""),
                "deep_analysis_cn": str(scalar(pf.get("AI深度解读")) or ""),
                "why": str(scalar(pf.get("为何重要")) or ""),
                "impact": int(float(scalar(pf.get("影响分")) or 0)),
                "novelty": int(float(scalar(pf.get("新颖度")) or 0)),
                "actionability": int(float(scalar(pf.get("可行动性")) or 0)),
                "urgency": "中",
                "topics": [str(scalar(x)) for x in (pf.get("主题") or [])] or ["其他"],
            }
            peers.append(
                _signal_from_fields(
                    str(peer.get("record_id") or ""),
                    pf,
                    peer_analysis,
                    priority=str(peer.get("priority") or "P2"),
                    tier=str(peer.get("tier") or scalar(pf.get("层级")) or ""),
                )
            )
        signal["eventPeers"] = peers
        signal["qualityScore"] = float(
            analysis.get("final_quality")
            or scalar(fields.get("质量分"))
            or 0
        )
        analyzed.append(signal)
    feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)

    if failed:
        log.warning("本轮 %d/%d 条分析失败：%s", len(failed), attempted, "；".join(failed[:5]))
    if analysis_failure_is_systemic(len(failed), attempted):
        raise RuntimeError(
            f"LLM 分析大面积失败（{len(failed)}/{attempted}），疑似配额或网关问题，本轮不发布"
        )
    if not analyzed:
        raise RuntimeError("没有可用于简报的已分析信号")

    def _display_quality(s: dict[str, Any]) -> float:
        q = float(s.get("qualityScore") or 0)
        if "arxiv.org/" in str(s.get("url") or ""):
            q *= config.ARXIV_QUALITY_WEIGHT
        return q

    def _content_weight(signal: dict[str, Any]) -> float:
        if signal.get("contentType") == "视频":
            return config.DAILY_VIDEO_WEIGHT
        if signal.get("contentType") == "播客":
            return config.DAILY_PODCAST_WEIGHT
        return 1.0

    analyzed.sort(
        key=lambda s: (
            _display_quality(s),
            s["impact"] * _content_weight(s),
            s["novelty"] * _content_weight(s),
            s["actionability"] * _content_weight(s),
        ),
        reverse=True,
    )
    signals = cluster.attach_aggregations(
        balance_output_signals(analyzed, config.DAILY_SIGNAL_LIMIT)
    )
    article_targets = [
        signal
        for signal in signals
        if signal.get("contentType") in {"文章", "公众号"}
        and str(signal.get("url") or "").startswith(("http://", "https://"))
        and "arxiv.org/" not in str(signal.get("url") or "")
    ]
    article_media: dict[str, dict[str, Any]] = {}
    if article_targets:
        with ThreadPoolExecutor(max_workers=min(8, len(article_targets))) as executor:
            fetched_media = executor.map(
                rss.fetch_article_media,
                [str(signal.get("url") or "") for signal in article_targets],
                [str(signal.get("title") or signal.get("titleCn") or "") for signal in article_targets],
            )
            article_media = {
                str(signal.get("recordId") or ""): bundle
                for signal, bundle in zip(article_targets, fetched_media)
                if bundle.get("cover") or bundle.get("images") or bundle.get("candidates")
            }
    image_updates = []
    seen_images: set[str] = set()
    for signal in signals:
        media = signal.get("mediaAssets") or {"images": [], "videos": []}
        original_media = media
        original_image = str(signal.get("imageUrl") or "").strip()
        if signal.get("contentType") in {"文章", "公众号", "视频", "播客"}:
            bundle = article_media.get(str(signal.get("recordId") or ""), {})
            media, curated_image = rss.select_pushed_article_images(
                signal,
                bundle,
            )
            signal["mediaAssets"] = media
            signal["imageUrl"] = curated_image
            if media != original_media or curated_image != original_image:
                fields: dict[str, Any] = {
                    "媒体资源": json.dumps(media, ensure_ascii=False),
                }
                if curated_image:
                    fields["图片链接"] = {"link": curated_image, "text": "原文封面"}
                image_updates.append({"record_id": signal["recordId"], "fields": fields})
        if "arxiv.org/" in str(signal.get("url") or "") and not media.get("images"):
            figures = rss.fetch_arxiv_figures(str(signal.get("url") or ""))
            if figures:
                media["images"] = figures
                signal["mediaAssets"] = media
                image_updates.append(
                    {
                        "record_id": signal["recordId"],
                        "fields": {"媒体资源": json.dumps(media, ensure_ascii=False)},
                    }
                )
        image_url = str(signal.get("imageUrl") or "").strip()
        image_key = image_url.split("?", 1)[0].split("#", 1)[0].lower()
        if image_key in seen_images:
            image_url = ""
        if not image_url:
            bundle = article_media.get(str(signal.get("recordId") or ""), {})
            candidate = rss.preferred_article_image(signal, bundle)
            candidate_key = candidate.split("?", 1)[0].split("#", 1)[0].lower()
            if candidate_key and candidate_key not in seen_images:
                image_url = candidate
                image_updates.append(
                    {
                        "record_id": signal["recordId"],
                        "fields": {"图片链接": {"link": image_url, "text": "原文配图"}},
                    }
                )
        signal["imageUrl"] = image_url
        if image_url:
            seen_images.add(image_url.split("?", 1)[0].split("#", 1)[0].lower())
    if image_updates:
        merged_updates: dict[str, dict[str, Any]] = {}
        for update in image_updates:
            record_id = str(update["record_id"])
            merged_updates.setdefault(record_id, {}).update(update["fields"])
        feishu.batch_update_records(
            token,
            config.FEISHU_ENTRY_TABLE_ID,
            [{"record_id": record_id, "fields": fields} for record_id, fields in merged_updates.items()],
        )

    numbered = "\n".join(f'[{i}] {s["title"]} — {s["summary"]}' for i, s in enumerate(signals, 1))
    synth = report._llm_json(
        "你是 AI 情报主编。只依据以下信号输出严格 JSON："
        "intro 为2句中文导语；bullets 为3-6个对象，每个必须含 title、text 和 refs（引用编号数组）。"
        "title 必须是概括具体结论的中文短标题，严禁使用“要点1”“要点2”等占位标题；"
        "text 不要重复 title。\n" + numbered
    )
    bullets = []
    for item in synth.get("bullets") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        refs = [int(x) for x in item.get("refs") or [] if str(x).isdigit()]
        bullets.append(
            {
                "title": referenced_signal_title(signals, refs)
                or brief_bullet_title(text, str(item.get("title") or "")),
                "text": text,
                "refs": refs,
            }
        )
        if len(bullets) == 6:
            break
    if not bullets:
        bullets = [
            {
                "title": referenced_signal_title(signals, [index])
                or brief_bullet_title(signal["summary"]),
                "text": signal["summary"],
                "refs": [index],
            }
            for index, signal in enumerate(signals[:3], 1)
        ]
    payload = {
        "date": day or today_cn(),
        "title": f"AI Signal 每日情报 · {day or today_cn()}",
        "intro": str(synth.get("intro") or "今日 AI 信号已完成采集与分析。"),
        "bullets": bullets,
        "signals": signals,
    }
    table_id = config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token)
    payload["briefRecordId"] = _upsert_brief(token, table_id, payload)
    payload["briefTableId"] = table_id
    return payload


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="简报日期，默认北京时间今天")
    parser.add_argument("--output", default="output/daily-brief.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    payload = generate(args.date)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已生成 %s，共 %d 条信号", payload["date"], len(payload["signals"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
