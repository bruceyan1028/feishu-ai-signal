from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import process, social, sources


def _feed(accounts: list[str] | None = None) -> dict:
    accounts = accounts or ["openai"]
    return {
        "id": "social-media",
        "name": "X 白名单账号",
        "fetch_method": "Social",
        "source_type": "社交媒体",
        "priority": "P0",
        "lookback_hours": 168,
        "min_content_chars": 30,
        "dedup_key": "x_post_id",
        "accounts": accounts,
        "account_tiers": {name: "P0" for name in accounts},
        "social_params": {
            "min_content_chars": 30,
            "direct_score": 70,
            "borderline_score": 45,
            "enable_llm_filter": True,
            "exclude_replies": True,
            "daily_caps": {"P0": 5, "P1": 2},
        },
    }


def _item(
    account: str = "openai",
    *,
    post_id: str = "1900000000000000001",
    text: str = "We released Model 2 with 30% better reasoning. https://github.com/example/model",
    refs: list[dict] | None = None,
    conversation_id: str | None = None,
) -> dict:
    return {
        "id": post_id,
        "text": text,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "conversation_id": conversation_id or post_id,
        "referenced_tweets": refs or [],
        "public_metrics": {
            "like_count": 100,
            "reply_count": 10,
            "retweet_count": 20,
            "quote_count": 5,
        },
        "edit_history_tweet_ids": [post_id],
    }


class SocialSourceTest(unittest.TestCase):
    @patch("src.social._SESSION.get")
    def test_payment_required_has_actionable_error(self, get: MagicMock) -> None:
        response = MagicMock(status_code=402, headers={})
        get.return_value = response
        with self.assertRaisesRegex(RuntimeError, "购买 credits"):
            social._api_get("users/by", bearer="token", params={})

    def test_map_social_source_requires_status_and_whitelist(self) -> None:
        records = [
            {
                "record_id": "rec1",
                "fields": {
                    "source_id": "social-media",
                    "name": "X",
                    "status": "experimental",
                    "fetch_method": "Social",
                    "来源类型": "社交媒体",
                    "lookback_window": "7d",
                    "采集游标": '{"openai":{"user_id":"1","since_id":"9"}}',
                },
            }
        ]
        params = {
            "social-media": {
                "account_whitelist": ["openai", "anthropicai"],
                "account_tiers": {"openai": "P0", "anthropicai": "P0"},
            }
        }
        self.assertEqual(sources.map_social_sources(records, params), [])
        feeds = sources.map_social_sources(records, params, allow_experimental=True)
        self.assertEqual(feeds[0]["accounts"], ["openai", "anthropicai"])
        self.assertEqual(feeds[0]["cursor_state"]["openai"]["since_id"], "9")
        self.assertEqual(feeds[0]["dedup_key"], "x_post_id")

    @patch("src.social._api_get")
    def test_timeline_paginates_from_since_id(self, api_get) -> None:
        api_get.side_effect = [
            {"data": [_item(post_id="11")], "meta": {"next_token": "next"}},
            {"data": [_item(post_id="12")], "meta": {}},
        ]
        posts, *_ = social._timeline("1", bearer="token", since_id="10")
        self.assertEqual([post["id"] for post in posts], ["11", "12"])
        self.assertEqual(api_get.call_args_list[0].kwargs["params"]["since_id"], "10")
        self.assertEqual(api_get.call_args_list[1].kwargs["params"]["pagination_token"], "next")

    def test_thread_is_merged_and_reply_is_not_mistaken_for_noise(self) -> None:
        root = _item(post_id="100", conversation_id="100")
        reply = _item(
            post_id="101",
            text="Technical details and benchmark table follow in this thread.",
            refs=[{"type": "replied_to", "id": "100"}],
            conversation_id="100",
        )
        items = social._build_account_items(
            [reply, root],
            {},
            feed=_feed(),
            username="openai",
            profile={"name": "OpenAI", "followers": 1000000},
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["metrics"]["thread_count"], 2)
        self.assertIn("Technical details", items[0]["body"])
        kept, _ = social.filter_social_items(items, classifier=lambda _: True)
        self.assertEqual(len(kept), 1)

    def test_repost_reply_noise_and_short_posts_are_hard_filtered(self) -> None:
        posts = [
            _item(post_id="1", refs=[{"type": "retweeted", "id": "0"}]),
            _item(post_id="2", refs=[{"type": "replied_to", "id": "0"}]),
            _item(post_id="3", text="We're hiring! Join us"),
            _item(post_id="4", text="nice"),
        ]
        items = social._build_account_items(
            posts,
            {},
            feed=_feed(),
            username="openai",
            profile={"followers": 100},
        )
        kept, stats = social.filter_social_items(items, classifier=lambda _: True)
        self.assertEqual(kept, [])
        self.assertEqual(stats["retweet"], 1)
        self.assertEqual(stats["reply"], 1)
        self.assertEqual(stats["noise"], 1)
        self.assertEqual(stats["short"], 1)

    def test_x_post_id_is_stable_dedup_key(self) -> None:
        feed = _feed()
        post = _item(post_id="1900000000000000099")
        post["edit_history_tweet_ids"] = ["1900000000000000001", "1900000000000000099"]
        raw = social._build_account_items(
            [post],
            {},
            feed=feed,
            username="openai",
            profile={"followers": 1000000},
        )
        raw, _ = social.filter_social_items(raw, classifier=lambda _: True)
        cleaned = process.process_and_clean(
            raw,
            {
                "social-media": {
                    "entity_type": "social",
                    "params": {"account_whitelist": ["openai"]},
                }
            },
        )
        self.assertEqual(cleaned[0]["duplicate_key"], "x:1900000000000000001")
        self.assertEqual(cleaned[0]["source_type"], "社交媒体")
        self.assertGreaterEqual(cleaned[0]["quality_score"], 70)

    def test_borderline_uses_classifier_and_daily_cap(self) -> None:
        feed = _feed(["researcher"])
        feed["priority"] = "P1"
        feed["account_tiers"] = {"researcher": "P1"}
        feed["social_params"]["daily_caps"] = {"P0": 5, "P1": 2}
        posts = [
            _item(
                account="researcher",
                post_id=str(200 + index),
                text=f"AI reasoning evaluation observation number {index} with useful context",
            )
            for index in range(4)
        ]
        items = social._build_account_items(
            posts,
            {},
            feed=feed,
            username="researcher",
            profile={"followers": 1000},
        )
        kept, stats = social.filter_social_items(items, classifier=lambda _: True)
        self.assertEqual(len(kept), 2)
        self.assertEqual(stats["daily_cap"], 2)

    def test_quote_with_thin_comment_is_dropped_long_comment_reaches_llm(self) -> None:
        thin = _item(post_id="11", text="This.", refs=[{"type": "quoted", "id": "1"}])
        long = _item(
            post_id="12",
            text=(
                "This launch changes the pricing floor: the new 7B weights are "
                "open and the $0.2/m token cut is real for inference teams."
            ),
            refs=[{"type": "quoted", "id": "2"}],
        )
        items = social._build_account_items(
            [thin, long],
            {},
            feed=_feed(),
            username="openai",
            profile={"followers": 100},
            referenced_by_id={
                "2": {"id": "2", "text": "We shipped a model today."},
            },
        )
        kept, stats = social.filter_social_items(items, classifier=lambda _: True)
        self.assertEqual(stats.get("quote_thin"), 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["metrics"]["post_id"], "12")
        self.assertEqual(kept[0]["metrics"]["gate"], "direct")

    def test_casual_model_talk_cannot_skip_fact_gate(self) -> None:
        post = _item(post_id="21", text="This model is so good, crazy times to be in AI.")
        items = social._build_account_items(
            [post], {}, feed=_feed(), username="openai", profile={"followers": 1000000}
        )
        kept, stats = social.filter_social_items(items, classifier=lambda _: False)
        self.assertEqual(kept, [])
        self.assertEqual(stats.get("direct", 0), 0)
        self.assertEqual(stats.get("llm_reject"), 1)

    def test_media_short_caption_reaches_fact_gate(self) -> None:
        post = _item(post_id="31", text="shipped")
        items = social._build_account_items(
            [post],
            {
                "m1": {
                    "media_key": "m1",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/example.jpg",
                }
            },
            feed=_feed(),
            username="openai",
            profile={"followers": 100},
        )
        # attachments must be on the post for media to attach
        self.assertEqual(items[0]["metrics"]["has_media"], False)
        post["attachments"] = {"media_keys": ["m1"]}
        items = social._build_account_items(
            [post],
            {
                "m1": {
                    "media_key": "m1",
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/example.jpg",
                }
            },
            feed=_feed(),
            username="openai",
            profile={"followers": 100},
        )
        self.assertTrue(items[0]["metrics"]["has_media"])
        kept, stats = social.filter_social_items(items, classifier=lambda _: True)
        self.assertEqual(stats.get("short", 0), 0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["metrics"]["gate"], "llm")

    @patch("src.social._api_get")
    def test_profile_resolves_avatar_and_verified_once(self, api_get) -> None:
        api_get.return_value = {
            "data": [
                {
                    "id": "9",
                    "username": "Karpathy",
                    "name": "Andrej Karpathy",
                    "public_metrics": {"followers_count": 1200000},
                    "profile_image_url": "https://pbs.twimg.com/profile_images/1/ak_normal.jpg",
                    "verified": True,
                    "verified_type": "blue",
                }
            ]
        }
        state: dict = {}
        resolved = social._resolve_accounts(["karpathy"], state, bearer="t")
        profile = resolved["karpathy"]
        self.assertEqual(profile["avatar"], "https://pbs.twimg.com/profile_images/1/ak_400x400.jpg")
        self.assertTrue(profile["verified"])
        self.assertIn("profile_image_url", api_get.call_args.kwargs["params"]["user.fields"])
        # 已解析过的账号不再重复请求
        api_get.reset_mock()
        social._resolve_accounts(["karpathy"], state, bearer="t")
        api_get.assert_not_called()

    @patch("src.social._api_get")
    def test_users_by_degrades_when_field_unsupported(self, api_get) -> None:
        api_get.side_effect = [
            RuntimeError("X API 400: Invalid field verified_type"),
            {"data": [{"id": "9", "username": "gdb", "name": "Greg Brockman"}]},
        ]
        resolved = social._resolve_accounts(["gdb"], {}, bearer="t")
        self.assertEqual(resolved["gdb"]["user_id"], "9")
        self.assertNotIn("verified_type", api_get.call_args.kwargs["params"]["user.fields"])

    def test_quoted_author_and_media_are_carried(self) -> None:
        post = _item(post_id="500", refs=[{"type": "quoted", "id": "400"}],
                     text="This ships the pricing change we flagged: 30% cheaper inference for everyone.")
        items = social._build_account_items(
            [post],
            {
                "mk1": {
                    "media_key": "mk1",
                    "type": "video",
                    "preview_image_url": "https://pbs.twimg.com/thumb.jpg",
                    "duration_ms": 1775000,
                }
            },
            feed=_feed(),
            username="openai",
            profile={
                "name": "OpenAI",
                "followers": 100,
                "avatar": "https://pbs.twimg.com/profile_images/1/oa_400x400.jpg",
                "verified": True,
            },
            referenced_by_id={
                "400": {
                    "id": "400",
                    "author_id": "77",
                    "text": "We are cutting inference prices today.",
                    "created_at": "2026-04-29T10:00:00.000Z",
                    "attachments": {"media_keys": ["mk1"]},
                }
            },
            users_by_id={
                "77": {
                    "id": "77",
                    "username": "stephzhan",
                    "name": "Stephanie Zhan",
                    "profile_image_url": "https://pbs.twimg.com/profile_images/2/sz_normal.jpg",
                    "verified": True,
                }
            },
        )
        metrics = items[0]["metrics"]
        self.assertEqual(metrics["account_avatar"], "https://pbs.twimg.com/profile_images/1/oa_400x400.jpg")
        self.assertTrue(metrics["account_verified"])
        quoted = metrics["quoted"]
        self.assertEqual(quoted["account"], "stephzhan")
        self.assertEqual(quoted["account_name"], "Stephanie Zhan")
        self.assertEqual(quoted["avatar"], "https://pbs.twimg.com/profile_images/2/sz_400x400.jpg")
        self.assertTrue(quoted["verified"])
        video = quoted["media_assets"]["videos"][0]
        self.assertEqual(video["thumbnailUrl"], "https://pbs.twimg.com/thumb.jpg")
        self.assertEqual(video["durationSec"], 1775.0)

    def test_seed_contains_personal_p0_accounts(self) -> None:
        seed = json.loads(
            (Path(__file__).resolve().parents[1] / "src" / "seed_default.json").read_text()
        )
        row = next(
            item
            for item in seed["二级参数-社媒"]
            if item.get("source_id") == "social-media"
        )
        accounts = [part.strip().lstrip("@").lower() for part in row["账号白名单"].split(",")]
        tiers = json.loads(row["账号分级"])
        self.assertEqual(
            accounts,
            ["gdb", "karpathy", "officiallogank", "_catwu", "dawnsongtweets", "steipete"],
        )
        self.assertEqual(set(tiers.values()), {"P0"})
        self.assertEqual(len(accounts), 6)

    def test_seven_day_ten_account_replay_precision(self) -> None:
        """合成回放先验证漏斗；真实 API 灰度由 experimental 诊断命令执行。"""
        accounts = [
            "openai",
            "anthropicai",
            "googledeepmind",
            "aiatmeta",
            "xai",
            "mistralai",
            "cohere",
            "huggingface",
            "nvidiaai",
            "stabilityai",
        ]
        feed = _feed(accounts)
        raw = []
        expected_ids = set()
        for index, account in enumerate(accounts):
            positive_id = str(3000 + index * 2)
            negative_id = str(3001 + index * 2)
            positive = _item(
                account,
                post_id=positive_id,
                text=f"We released AI Model {index} with 25% better benchmark results. https://github.com/{account}/model",
            )
            positive["created_at"] = (
                datetime.now(timezone.utc) - timedelta(days=index % 7)
            ).isoformat()
            negative = _item(account, post_id=negative_id, text="We're hiring! Join us")
            raw.extend(
                social._build_account_items(
                    [positive, negative],
                    {},
                    feed=feed,
                    username=account,
                    profile={"followers": 100000},
                )
            )
            expected_ids.add(positive_id)
        kept, _ = social.filter_social_items(raw, classifier=lambda _: False)
        actual_ids = {(item["metrics"] or {})["post_id"] for item in kept}
        true_positive = len(actual_ids & expected_ids)
        precision = true_positive / max(1, len(actual_ids))
        recall = true_positive / len(expected_ids)
        self.assertGreaterEqual(precision, 0.8)
        self.assertEqual(recall, 1.0)


if __name__ == "__main__":
    unittest.main()
