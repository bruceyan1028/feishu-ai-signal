import unittest
from unittest import mock

from src import config, health, process


def _item(**kw):
    base = {
        "source_id": "s1",
        "fetch_method": "RSS",
        "title": "t",
        "raw_content": "",
        "min_content_chars": 100,
    }
    base.update(kw)
    return base


class DropTooShortTest(unittest.TestCase):
    def test_drops_when_title_plus_body_below_threshold(self):
        short = _item(raw_content="x" * 50)
        long_enough = _item(raw_content="x" * 200)
        kept, dropped = process.drop_too_short([short, long_enough])
        self.assertEqual(kept, [long_enough])
        self.assertEqual(dropped, [short])

    def test_social_is_never_judged_here(self):
        tweet = _item(fetch_method="Social", raw_content="short", min_content_chars=30)
        kept, dropped = process.drop_too_short([tweet])
        self.assertEqual(kept, [tweet])
        self.assertEqual(dropped, [])

    def test_media_threshold_is_enforced(self):
        video = _item(fetch_method="Media", title="", raw_content="", min_content_chars=20)
        kept, dropped = process.drop_too_short([video])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [video])

    def test_funnel_moves_count_from_kept_to_min_content_chars(self):
        funnel = health.Funnel()
        funnel.bump("s1", "kept")
        process.drop_too_short([_item(raw_content="x")], funnel)
        self.assertEqual(funnel.for_source("s1"), {"kept": 0, "min_content_chars": 1})


class CleanNoLongerJudgesLengthTest(unittest.TestCase):
    def test_short_arxiv_abstract_survives_clean_stage(self):
        feed = {
            "id": "arxiv-cs-ai",
            "fetch_method": "RSS",
            "min_content_chars": 200,
            "lookback_hours": 24 * 365 * 10,
            "keyword_regex": "model",
        }
        raw = {
            "title": "A tiny model",
            "url": "https://arxiv.org/abs/2609.00001",
            "body": "short abstract",
            "published_raw": "2026-09-01T00:00:00Z",
            "feed": feed,
        }
        with mock.patch.object(config, "PAPER_ENRICH_ENABLED", False):
            cleaned = process.process_and_clean([raw])
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["min_content_chars"], 200)
        self.assertNotIn("needs_fulltext", cleaned[0])


if __name__ == "__main__":
    unittest.main()
