"""给每日条目打固定话题标签。

    python -m src.tag_topics --date 2026-08-27
    python -m src.tag_topics --ingest-brief site/data/brief-2026-08-27.json
    python -m src.tag_topics --all

已打标的日期默认跳过。标签必须落在 heatmap.TOPICS 里，否则整批重打。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import config, heatmap
from .report import _llm_json

log = logging.getLogger(__name__)

PROMPT = """你是 AI 行业情报标注员。下面有一批内容，请为每条选 1 或 2 个话题。
只能从这份枚举里选，都不沾边就标 other：
- agent：智能体 / Agent 工作流 / MCP / 工具调用
- reasoning：推理模型 / 思维链 / 测试时计算
- multimodal：多模态 / 视觉 / 语音 / 视频生成
- open-source-model：开源模型权重发布与生态
- rag-search：RAG / 检索 / 知识库
- infra：推理优化 / 训练框架 / 算力 / 芯片
- embodied：具身智能 / 机器人
- safety-policy：安全 / 对齐 / 监管政策 / 版权
- product：AI 应用产品 / 商业化
- funding：融资 / 并购 / 人事变动
- other：以上都不沾边

返回严格 JSON 对象：
{{"results":[{{"id":"条目id","topics":["agent"]}}]}}

条目：
{batch}
"""


class TagError(RuntimeError):
    pass


def _batch_lines(rows: list[dict[str, Any]]) -> str:
    lines = []
    for row in rows:
        summary = str(row.get("summary") or "").replace("\n", " ")[:280]
        lines.append(
            f'- id={row["id"]} | source={row.get("source","")} | title={row.get("title","")} | summary={summary}'
        )
    return "\n".join(lines)


def _index_results(payload: Any) -> dict[str, list[str]]:
    if isinstance(payload, dict):
        results = payload.get("results") or payload.get("tags") or payload.get("items") or []
    elif isinstance(payload, list):
        results = payload
    else:
        results = []
    indexed: dict[str, list[str]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id") or "").strip()
        if not ident:
            continue
        parsed = heatmap.parse_topics_strict(item.get("topics"))
        if parsed is None:
            continue
        indexed[ident] = parsed
    return indexed


def tag_batch(rows: list[dict[str, Any]], *, retries: int = 3) -> dict[str, list[str]]:
    """一次塞最多 20 条。返回的标签不在枚举内就整批重打，而不是悄悄丢掉。"""
    if not rows:
        return {}
    pending = list(rows)
    accepted: dict[str, list[str]] = {}
    last_error = "empty response"
    for attempt in range(retries):
        try:
            payload = _llm_json(PROMPT.format(batch=_batch_lines(pending)))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            last_error = str(error)
            time.sleep(2**attempt)
            continue
        indexed = _index_results(payload)
        still: list[dict[str, Any]] = []
        for row in pending:
            topics = indexed.get(row["id"])
            if not topics:
                still.append(row)
                continue
            accepted[row["id"]] = topics
        if not still:
            return accepted
        pending = still
        last_error = f"missing {len(still)} ids"
        time.sleep(2**attempt)
    raise TagError(f"打标校验失败（{last_error}），未写入，避免脏标签进热力图")


def tag_date(day: str, *, force: bool = False) -> Path:
    out = heatmap.TAGGED_DIR / f"{day}.jsonl"
    if out.is_file() and not force:
        log.info("skip %s: already tagged", day)
        return out
    src = heatmap.ITEMS_DIR / f"{day}.jsonl"
    if not src.is_file():
        raise FileNotFoundError(f"找不到 {src}，先准备 JSONL 或 --ingest-brief")
    if not config.LLM_API_KEY:
        raise config.ConfigError("打标需要 LLM_API_KEY")
    rows = heatmap.read_jsonl(src)
    tagged: list[dict[str, Any]] = []
    for start in range(0, len(rows), heatmap.BATCH_SIZE):
        chunk = rows[start : start + heatmap.BATCH_SIZE]
        labels = tag_batch(chunk)
        for row in chunk:
            tagged.append({**row, "topics": labels[row["id"]]})
    heatmap.write_jsonl(out, tagged)
    return out


def ingest_brief(path: Path) -> Path:
    brief = json.loads(path.read_text(encoding="utf-8"))
    day = str(brief.get("date") or path.stem.replace("brief-", ""))
    items = heatmap.brief_to_items(brief)
    dest = heatmap.ITEMS_DIR / f"{day}.jsonl"
    heatmap.write_jsonl(dest, items)
    return dest


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="给每日条目打话题标签")
    parser.add_argument("--date", help="YYYY-MM-DD，读取 data/items/{date}.jsonl")
    parser.add_argument("--all", action="store_true", help="处理 items 目录下全部未打标日期")
    parser.add_argument("--ingest-brief", type=Path, help="从现有简报 JSON 生成 items JSONL")
    parser.add_argument("--force", action="store_true", help="已打标的日期也重打")
    args = parser.parse_args()

    if args.ingest_brief:
        dest = ingest_brief(args.ingest_brief)
        print(f"已写入 {dest}")
        if not args.date:
            args.date = dest.stem

    if args.all:
        days = sorted(path.stem for path in heatmap.ITEMS_DIR.glob("*.jsonl"))
    elif args.date:
        days = [args.date]
    else:
        parser.error("需要 --date、--all 或 --ingest-brief")

    for day in days:
        out = tag_date(day, force=args.force)
        print(f"tagged {day} -> {out}")


if __name__ == "__main__":
    run()
