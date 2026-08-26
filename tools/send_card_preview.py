"""把当前排版的每日卡片发给指定 open_id 验收。

只调发消息接口，不碰简报表的发送状态，因此不会影响当天的正式推送。

    python -m tools.send_card_preview --open-id ou_xxx
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src import config, feishu, notify


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="site/data/brief-latest.json")
    parser.add_argument("--open-id", required=True)
    parser.add_argument("--base-url", default=config.PUBLIC_BASE_URL or "https://bruceyan1028.github.io/feishu-ai-signal")
    args = parser.parse_args()

    brief = json.loads(Path(args.input).read_text(encoding="utf-8"))
    card = notify.build_card(brief, notify.detail_url(args.base_url, str(brief["date"])))
    card["elements"].append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "排版预览，非当日正式推送"}],
        }
    )
    message_id = feishu.send_interactive_message(
        feishu.get_tenant_access_token(), args.open_id, card
    )
    print(f"已发送 message_id={message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
