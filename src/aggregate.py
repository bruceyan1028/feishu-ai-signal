"""把已打标的 JSONL 聚合成话题 × 周热力图。

    python -m src.aggregate
    python -m src.aggregate --seed-mock
    python -m src.aggregate --window 12 --output site/data/heatmap.json

`--seed-mock` 会写出 4 周模拟条目、打标结果和 heatmap.json，前端可以直接预览。
真实数据接上后同一条命令只读 data/tagged/，不必改页面。
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import heatmap

log = logging.getLogger(__name__)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="聚合话题热力图")
    parser.add_argument("--seed-mock", action="store_true", help="生成 4 周模拟数据并聚合")
    parser.add_argument("--window", type=int, default=heatmap.WINDOW_WEEKS)
    parser.add_argument("--output", type=Path, default=heatmap.SITE_HEATMAP_PATH)
    args = parser.parse_args()

    if args.seed_mock:
        payload = heatmap.seed_mock()
        print(f"mock weeks={payload['weeks']}")
    else:
        rows = heatmap.load_all_tagged()
        if not rows:
            raise SystemExit("data/tagged/ 是空的。先 python -m src.tag_topics，或 --seed-mock 预览。")
        payload = heatmap.build_heatmap(rows, window=args.window)
        heatmap.write_json(heatmap.HEATMAP_PATH, payload)
        heatmap.write_json(args.output, payload)

    print(f"heatmap -> {args.output} ({len(payload['weeks'])} weeks, {len(payload['topics'])} topics)")


if __name__ == "__main__":
    run()
