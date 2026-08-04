"""把单条播客摘要 JSON 生成只含播客的本地前端预览站。"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import requests

from . import podcast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "output" / "podcast-preview.json"
DEFAULT_OUT = ROOT / "output" / "podcast-web-preview"
DEFAULT_METADATA_ANALYSIS = ROOT / "output" / "podcast-metadata-analysis.json"
APPLE_COLLECTIONS = {
    "Dwarkesh Podcast": 1516093381,
    "Latent Space": 1674008350,
    "No Priors": 1668002688,
    "十字路口 Crossing": 1729552193,
    "AI 炼金术": 1671502201,
    "硅谷 101": 1498541229,
}
METADATA_FALLBACKS: dict[str, dict[str, Any]] = {
    "Dwarkesh Podcast|Adam Brown – A deep but accessible introduction to general relativity": {
        "title_cn": "亚当·布朗：一场深入浅出的广义相对论导论",
        "summary_cn": "本期邀请理论物理学家亚当·布朗讲解广义相对论的核心直觉、黑洞及实验依据，并在节目后段讨论 AI 是否可能重新发现这类基础理论。该期主体是物理科普而非 AI，测试预览保留它是为了审核白名单节目中非 AI 单集的过滤效果。",
        "guest_intro_cn": "亚当·布朗是斯坦福理论物理学家，并领导 Google DeepMind 的 Blueshift 科学与推理团队。",
        "core_points": [
            {"title": "广义相对论的核心直觉", "text": "从惯性质量与引力质量相等出发，解释引力为何可以理解为弯曲时空中的运动。"},
            {"title": "黑洞与可验证证据", "text": "节目计划讨论事件视界、时间膨胀、引力波及黑洞成像等内容。"},
        ],
    },
    "Dwarkesh Podcast|The next big breakthrough will be AIs learning on the job": {
        "title_cn": "AI 的下一次重大突破：在工作中持续学习",
        "summary_cn": "Dwarkesh Patel 讨论实验室押注的大规模可验证强化学习是否足以通向 AGI，并提出“可反复训练”与“可验证”同样重要。节目进一步比较长上下文、持续学习、权重更新和部署经验，关注模型能否像员工一样在真实工作中积累能力。",
        "guest_intro_cn": "本期为主持人 Dwarkesh Patel 的个人论述，没有外部访谈嘉宾。",
        "core_points": [
            {"title": "RLVR 的能力边界", "text": "可验证任务未必都适合无限生成和反复训练，开放现实任务尤其困难。"},
            {"title": "持续学习的必要性", "text": "节目比较把经验留在上下文与把部署经验更新回模型权重两种路径。"},
        ],
    },
    "Latent Space|Inside the Model Factory — Eiso Kant, Poolside AI": {
        "title_cn": "走进模型工厂：Poolside AI 的 Eiso Kant",
        "summary_cn": "本期围绕 Poolside AI 如何从早期投入约 1200 万美元训练代码模型，发展出可持续生产新模型的“模型工厂”。节目计划讨论 Laguna S 2.1、合成数据、训练基础设施，以及本地化和主权 AI 需求对模型公司的影响。",
        "guest_intro_cn": "Eiso Kant 是 Poolside AI 联合创始人兼 CEO，长期从事面向软件开发的基础模型与训练系统建设。",
        "core_points": [
            {"title": "模型工厂", "text": "重点不是一次训练单个模型，而是建立能持续生产、评估和迭代模型的系统。"},
            {"title": "小模型竞争力", "text": "节目将以 Laguna S 2.1 为例讨论较小模型如何通过数据与训练体系取得竞争表现。"},
        ],
    },
    "Latent Space|🔬Causal Models Need Causal Data - Xaira’s X-Cell model for Drug Discovery (Bo Wang & Ci Chu, Chief Discovery Officer & Chief AI Scientist)": {
        "title_cn": "因果模型需要因果数据：Xaira 的药物发现模型 X-Cell",
        "summary_cn": "本期讨论 Xaira 如何为细胞状态和药物发现构建 X-Cell 模型。官方简介强调，当验证损失在约 15 亿参数后停滞时，瓶颈不是参数或算力，而是数据的信息量；团队通过更丰富的因果实验数据探索可继续扩展的路线。",
        "guest_intro_cn": "Bo Wang 与 Ci Chu 分别担任 Xaira 的首席发现官和首席 AI 科学家，负责药物发现与机器学习研究。",
        "core_points": [
            {"title": "信息量而非参数量", "text": "训练损失继续下降而验证损失停滞，说明现有数据无法支持更大模型泛化。"},
            {"title": "因果实验数据", "text": "团队通过提高数据的信息密度，尝试恢复模型随参数规模增长的性能趋势。"},
        ],
    },
    "Latent Space|🔬 The Lab of the Future Should Feel Like a Data Center — Andy Beam & Rafa Gómez-Bombarelli, Lila Sciences": {
        "title_cn": "未来实验室应像数据中心：Lila Sciences 的自动化科学工厂",
        "summary_cn": "本期介绍 Lila Sciences 对自动化实验室的设想：让 AI 调度机器人和仪器全天运行实验，持续产生科学数据。节目计划讨论实验设备编排、视觉语言模型控制旧系统，以及自动实验如何形成面向科学智能的数据闭环。",
        "guest_intro_cn": "Andy Beam 与 Rafa Gómez-Bombarelli 来自 Lila Sciences，工作重点涉及机器学习、自动化实验和材料科学。",
        "core_points": [
            {"title": "实验室数据中心化", "text": "把实验设备组织成可编排、可并行、全天运行的自动化基础设施。"},
            {"title": "AI 与机器人闭环", "text": "通过模型选择实验、控制设备并吸收结果，积累用于科学发现的新数据。"},
        ],
    },
    "No Priors|Building an Autonomous Delivery Experience with DoorDash Co-Founders Andy Fang and Stanley Tang": {
        "title_cn": "DoorDash 联合创始人：构建自主配送体验",
        "summary_cn": "DoorDash 联合创始人 Andy Fang 与 Stanley Tang 讨论 AI、机器人和自动驾驶如何改变配送与本地商业。官方简介提到自然语言入口 Ask DoorDash、内部配送机器人 Dot，以及把硬件系统部署到真实城市环境时面临的运营挑战。",
        "guest_intro_cn": "Andy Fang 与 Stanley Tang 是 DoorDash 联合创始人，长期负责公司产品、技术及配送网络建设。",
        "core_points": [
            {"title": "自然语言商业入口", "text": "Ask DoorDash 用对话方式帮助用户发现餐厅和完成更复杂的购物需求。"},
            {"title": "配送机器人落地", "text": "Dot 在凤凰城长期运行，用于验证机器人硬件与配送运营协同。"},
        ],
    },
    "No Priors|Travel Through the Lens of AI with with Booking.com CEO Glenn Fogel": {
        "title_cn": "Booking.com CEO Glenn Fogel：从 AI 视角重看旅行",
        "summary_cn": "Booking Holdings CEO Glenn Fogel 回顾公司从互联网泡沫时期成长为全球旅行平台的过程，并讨论 AI 如何改变旅行搜索、推荐、客服和平台运营。节目也会涉及全球市场扩张及大型在线旅行平台在 AI 时代的竞争优势。",
        "guest_intro_cn": "Glenn Fogel 是 Booking Holdings CEO，2000 年加入 Priceline，长期参与公司全球扩张与业务整合。",
        "core_points": [
            {"title": "平台规模与 AI", "text": "节目关注全球旅行交易和用户数据如何支持个性化搜索、推荐与服务。"},
            {"title": "从危机到全球扩张", "text": "嘉宾将回顾互联网泡沫后公司恢复并扩大为全球旅行集团的经历。"},
        ],
    },
    "No Priors|How Nuclear Will Unlock Energy Abundance with Valar Atomics Founder Isaiah Taylor": {
        "title_cn": "Valar Atomics 创始人 Isaiah Taylor：核能如何释放能源丰裕",
        "summary_cn": "Valar Atomics 创始人 Isaiah Taylor 讨论公司如何通过快速硬件迭代推进小型核反应堆，并介绍用在运反应堆为 NVIDIA Blackwell 芯片供电的演示。节目计划分析美国核工业停滞的原因、工程验证方式及核能对 AI 算力供电的潜在价值。",
        "guest_intro_cn": "Isaiah Taylor 是 Valar Atomics 创始人兼 CEO，专注小型核反应堆的工程开发与商业部署。",
        "core_points": [
            {"title": "从纸面设计到硬件迭代", "text": "公司强调通过实际建造和测试反应堆来缩短核能工程反馈周期。"},
            {"title": "核能与 AI 算力", "text": "节目以 Blackwell 芯片供电演示讨论稳定能源对数据中心扩张的意义。"},
        ],
    },
}


def build_brief(preview: dict[str, Any]) -> dict[str, Any]:
    analysis = preview.get("analysis") or {}
    # 当前核验样本的公开播放元数据来自 Apple Podcasts lookup。
    audio_url = str(preview.get("audio_url") or "")
    audio_embed_url = str(preview.get("audio_embed_url") or "")
    artwork_url = str(preview.get("artwork_url") or "")
    duration_sec = int(preview.get("duration_sec") or 0)
    signal = {
        "recordId": f"podcast-preview-{preview.get('source_id') or 'episode'}",
        "title": str(preview.get("title") or ""),
        "titleCn": str(analysis.get("title_cn") or preview.get("title") or ""),
        "source": str(preview.get("show") or "播客"),
        "url": str(preview.get("url") or ""),
        "category": "技术研究开源",
        "contentType": "播客",
        "tier": "L4",
        "priority": "P2",
        "publishedDate": str(preview.get("published_date") or "2026-07-10"),
        "summary": str(analysis.get("summary_cn") or ""),
        "deepAnalysis": str(analysis.get("deep_analysis_cn") or ""),
        "why": str(analysis.get("why") or ""),
        "impact": int(analysis.get("impact") or 0),
        "novelty": int(analysis.get("novelty") or 0),
        "actionability": int(analysis.get("actionability") or 0),
        "urgency": str(analysis.get("urgency") or "中"),
        "tags": list(analysis.get("topics") or ["播客"]),
        "imageUrl": artwork_url,
        "transcriptEvidence": str(preview.get("evidence_cn") or ""),
        "podcastMetrics": {
            "transcript_source": str(preview.get("transcript_source") or ""),
            "transcript_chars": int(preview.get("transcript_chars") or 0),
            "duration_sec": duration_sec,
            "quality_score": float(preview.get("quality_score") or 0),
        },
        "mediaAssets": {
            "images": [],
            "videos": [],
            "audio": {
                "url": audio_url,
                "embedUrl": audio_embed_url,
                "type": "audio/mpeg",
                "durationSec": duration_sec,
                "title": str(preview.get("title") or ""),
            },
        },
    }
    return {
        "date": signal["publishedDate"],
        "title": "播客完整摘要 · 本地预览",
        "intro": "仅展示播客内容：边听原音频，边查看完整中文摘要、深度解读与时间戳证据稿。",
        "bullets": [
            {
                "title": "逐字稿到完整摘要",
                "text": (
                    f"本期使用 {signal['podcastMetrics']['transcript_chars']:,} 字符逐字稿，"
                    "经分段归纳后生成中文摘要和五节深度解读。"
                ),
                "refs": [1],
            },
            {
                "title": "证据可追溯",
                "text": "详情页可展开时间戳证据稿，并可直接播放原节目音频进行核对。",
                "refs": [1],
            },
        ],
        "signals": [signal],
    }


def _plain_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def analyze_official_description(show: str, title: str, description: str) -> dict[str, Any]:
    """把节目简介整理成可审核的结构，不冒充完整逐字稿分析。"""
    raw = podcast.summarize_official_description(title, show, description)
    return {
        "title_cn": str(raw.get("title_cn") or title).strip(),
        "summary_cn": str(raw.get("summary_cn") or "").strip(),
        "guest_intro_cn": str(
            raw.get("guest_intro_cn") or "官方简介未提供足够的嘉宾身份信息。"
        ).strip(),
        "core_points": list(raw.get("core_points") or []),
        "why": str(raw.get("why") or "").strip(),
    }


def _apple_episodes(
    show: str,
    collection_id: int,
    limit: int,
    metadata_analysis: dict[str, dict[str, Any]],
    *,
    analyze_missing: bool = False,
) -> list[dict[str, Any]]:
    response = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": collection_id, "entity": "podcastEpisode", "limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    episodes = [
        item
        for item in response.json().get("results") or []
        if item.get("wrapperType") == "podcastEpisode"
    ]
    result: list[dict[str, Any]] = []
    for index, episode in enumerate(episodes[:limit], 1):
        title = str(episode.get("trackName") or "未命名节目")
        page_url = str(episode.get("trackViewUrl") or "")
        embed_url = page_url.replace("https://podcasts.apple.com/", "https://embed.podcasts.apple.com/")
        duration_sec = int((episode.get("trackTimeMillis") or 0) / 1000)
        cache_key = f"{show}|{title}"
        description = _plain_text(
            episode.get("description") or episode.get("shortDescription"),
            5000,
        )
        cached = metadata_analysis.get(cache_key) or METADATA_FALLBACKS.get(cache_key) or {}
        if analyze_missing and not cached and description:
            cached = analyze_official_description(show, title, description)
            metadata_analysis[cache_key] = cached
        core_points = [
            f"【简介要点{index + 1}：{str(point.get('title') or '节目简介要点')}】\n"
            f"{str(point.get('text') or '').strip()}"
            for index, point in enumerate(cached.get("core_points") or [])
            if isinstance(point, dict) and str(point.get("text") or "").strip()
        ]
        guest_intro = str(cached.get("guest_intro_cn") or "").strip()
        has_guest = bool(
            guest_intro
            and not any(
                token in guest_intro
                for token in ("未提供足够", "未披露嘉宾", "未说明嘉宾")
            )
        )
        detail_sections = []
        if has_guest:
            detail_sections.append("【嘉宾与背景】\n" + guest_intro)
        detail_sections.extend(core_points)
        boundary = (
            "本卡片只依据节目官方简介提炼，尚未取得完整逐字稿；"
            "因此不对简介之外的论证过程、分歧和结论作推断。"
        )
        if not has_guest:
            boundary += " 官方简介未披露可核验的嘉宾身份。"
        deep_analysis = "\n\n".join(
            [
                *detail_sections,
                "【信息边界】\n" + boundary,
            ]
        )
        result.append(
            {
                "recordId": f"podcast-catalog-{collection_id}-{index}",
                "title": title,
                "titleCn": str(cached.get("title_cn") or title),
                "source": show,
                "url": page_url,
                "category": "其他",
                "contentType": "播客",
                "tier": "L4",
                "priority": "P2",
                "publishedDate": str(episode.get("releaseDate") or "")[:10],
                "summary": str(cached.get("summary_cn") or "")
                or description[:600]
                or "测试预览已取得节目元数据；完整摘要将在转录后生成。",
                "deepAnalysis": deep_analysis,
                "why": str(cached.get("why") or "")
                or "测试阶段用于审核该播客源的选题质量与节目可播放性。",
                "impact": 0,
                "novelty": 0,
                "actionability": 0,
                "urgency": "中",
                "tags": ["播客", "待审核"],
                "imageUrl": str(episode.get("artworkUrl600") or episode.get("artworkUrl100") or ""),
                "podcastMetrics": {
                    "transcript_source": "official_description",
                    "transcript_chars": 0,
                    "duration_sec": duration_sec,
                    "quality_score": 0,
                },
                "mediaAssets": {
                    "images": [],
                    "videos": [],
                    "audio": {
                        "url": str(episode.get("episodeUrl") or ""),
                        "embedUrl": embed_url,
                        "type": str(episode.get("episodeContentType") or "audio/mpeg"),
                        "durationSec": duration_sec,
                        "title": title,
                    },
                },
            }
        )
    return result


def build_catalog_brief(
    preview: dict[str, Any],
    items_per_source: int,
    metadata_analysis: dict[str, dict[str, Any]],
    *,
    analyze_missing: bool = False,
) -> dict[str, Any]:
    enriched = build_brief(preview)["signals"][0]
    signals: list[dict[str, Any]] = []
    reports: list[str] = []
    for show, collection_id in APPLE_COLLECTIONS.items():
        try:
            episodes = _apple_episodes(
                show,
                collection_id,
                items_per_source,
                metadata_analysis,
                analyze_missing=analyze_missing,
            )
        except requests.RequestException as exc:
            reports.append(f"{show} 抓取失败：{exc}")
            continue
        for index, episode in enumerate(episodes):
            if show == enriched["source"] and episode["title"] == enriched["title"]:
                episodes[index] = enriched
        signals.extend(episodes)
        reports.append(f"{show} {len(episodes)} 期")

    return {
        "date": str(preview.get("published_date") or ""),
        "title": "播客源审核 · 本地预览",
        "intro": (
            f"测试模式临时跳过 7 天窗口和 AI 主题过滤；正式规则未改变。"
            f"当前展示 {len(APPLE_COLLECTIONS)} 个源、共 {len(signals)} 期节目。"
        ),
        "bullets": [
            {
                "title": "每源展示最近节目",
                "text": "；".join(reports),
                "refs": list(range(1, len(signals) + 1)),
            },
            {
                "title": "完整摘要样本",
                "text": "Grant Sanderson 访谈已完成逐字稿分析，其余卡片仅展示真实元数据，不用简介冒充完整总结。",
                "refs": [
                    index + 1
                    for index, signal in enumerate(signals)
                    if signal.get("transcriptEvidence")
                ],
            },
        ],
        "signals": signals,
    }


def write_preview(brief: dict[str, Any], out_dir: Path) -> Path:
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "index.html", out_dir / "index.html")
    payload = json.dumps(brief, ensure_ascii=False, indent=2)
    (data_dir / "brief-latest.json").write_text(payload, encoding="utf-8")
    (data_dir / f"brief-{brief['date']}.json").write_text(payload, encoding="utf-8")
    return out_dir / "index.html"


def run() -> int:
    parser = argparse.ArgumentParser(description="生成只含播客的本地前端预览")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="播客摘要 JSON")
    parser.add_argument(
        "--metadata-analysis",
        default=str(DEFAULT_METADATA_ANALYSIS),
        help="官方简介中文提炼缓存",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT), help="静态站输出目录")
    parser.add_argument("--items-per-source", type=int, default=3, help="每个源展示节目数")
    parser.add_argument(
        "--analyze-metadata",
        action="store_true",
        help="用 LLM 清理并结构化尚未缓存的官方简介",
    )
    parser.add_argument("--serve", action="store_true", help="生成后启动本地 HTTP 服务")
    parser.add_argument("--port", type=int, default=4178, help="HTTP 服务端口")
    args = parser.parse_args()

    input_path = Path(args.input)
    preview = json.loads(input_path.read_text(encoding="utf-8"))
    analysis_path = Path(args.metadata_analysis)
    metadata_analysis = (
        json.loads(analysis_path.read_text(encoding="utf-8")) if analysis_path.is_file() else {}
    )
    brief = build_catalog_brief(
        preview,
        max(2, min(3, args.items_per_source)),
        metadata_analysis,
        analyze_missing=args.analyze_metadata,
    )
    if args.analyze_metadata:
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text(
            json.dumps(metadata_analysis, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    page = write_preview(brief, Path(args.out_dir))
    print(f"播客前端预览已生成：{page}")
    if args.serve:
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        os.chdir(page.parent)
        print(f"本地预览：http://127.0.0.1:{args.port}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), SimpleHTTPRequestHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
