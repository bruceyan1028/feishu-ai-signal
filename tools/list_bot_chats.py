"""列出当前飞书机器人已加入的群，用来取 chat_id。

    python -m tools.list_bot_chats
"""
from __future__ import annotations

import json

from src import feishu


def run() -> int:
    chats = feishu.list_bot_chats(feishu.get_tenant_access_token())
    print(
        json.dumps(
            [
                {
                    "name": item.get("name"),
                    "chat_id": item.get("chat_id"),
                    "chat_status": item.get("chat_status"),
                }
                for item in chats
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
