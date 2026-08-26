"""把当前排版的每日卡片发给指定群或个人验收。

只调发消息接口，不碰简报表的发送状态，因此不会影响当天的正式推送。

    python -m tools.send_card_preview --chat-id oc_xxx
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
    parser.add_argument("--open-id")
    parser.add_argument("--chat-id")
    parser.add_argument("--base-url", default=config.PUBLIC_BASE_URL or "https://bruceyan1028.github.io/feishu-ai-signal")
    args = parser.parse_args()
    if bool(args.chat_id) == bool(args.open_id):
        parser.error("请指定 --chat-id 或 --open-id 之一")

    brief = json.loads(Path(args.input).read_text(encoding="utf-8"))
    card = notify.build_card(brief, notify.detail_url(args.base_url, str(brief["date"])))
    card["elements"].append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "排版预览，非当日正式推送"}],
        }
    )
    if args.chat_id:
        receive_id, receive_id_type = args.chat_id, "chat_id"
    else:
        receive_id, receive_id_type = args.open_id, "open_id"
    message_id = feishu.send_interactive_message(
        feishu.get_tenant_access_token(),
        receive_id,
        card,
        receive_id_type=receive_id_type,
    )
    print(f"已发送 message_id={message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
