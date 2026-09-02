"""RSS 拉取：UA / 超时 / 重试（不打外网）。"""
from __future__ import annotations

import unittest
from unittest import mock

import requests

from src import config, rss

_MINI_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Demo</title>
  <item>
    <title>Hello</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 01 Sep 2026 00:00:00 GMT</pubDate>
    <description>body</description>
  </item>
</channel></rss>
"""


class RssFetchTest(unittest.TestCase):
    def test_fetch_feed_parsed_uses_ua_and_parses_bytes(self):
        response = mock.Mock()
        response.status_code = 200
        response.content = _MINI_FEED
        response.raise_for_status = mock.Mock()

        with mock.patch("src.rss.requests.get", return_value=response) as get:
            parsed = rss._fetch_feed_parsed("https://example.com/feed.xml")

        get.assert_called_once()
        _, kwargs = get.call_args
        self.assertEqual(kwargs["headers"]["User-Agent"], rss._RSS_UA)
        self.assertIn("application/rss+xml", kwargs["headers"]["Accept"])
        self.assertEqual(kwargs["timeout"], rss._RSS_TIMEOUT)
        self.assertEqual(len(parsed.entries), 1)
        self.assertEqual(parsed.entries[0].title, "Hello")

    def test_fetch_feed_parsed_retries_connection_reset(self):
        response = mock.Mock()
        response.status_code = 200
        response.content = _MINI_FEED
        response.raise_for_status = mock.Mock()
        side_effect = [
            requests.ConnectionError("Connection reset by peer"),
            requests.ConnectionError("Connection reset by peer"),
            response,
        ]

        with mock.patch.object(config, "HTTP_MAX_TRIES", 4), mock.patch.object(
            config, "HTTP_WAIT_SECONDS", 0
        ), mock.patch("src.rss.requests.get", side_effect=side_effect) as get:
            parsed = rss._fetch_feed_parsed("https://rss.arxiv.org/rss/cs.LG")

        self.assertEqual(get.call_count, 3)
        self.assertEqual(len(parsed.entries), 1)

    def test_fetch_feed_parsed_retries_transient_http(self):
        bad = mock.Mock()
        bad.status_code = 503
        bad.content = b""
        good = mock.Mock()
        good.status_code = 200
        good.content = _MINI_FEED
        good.raise_for_status = mock.Mock()

        with mock.patch.object(config, "HTTP_MAX_TRIES", 3), mock.patch.object(
            config, "HTTP_WAIT_SECONDS", 0
        ), mock.patch("src.rss.requests.get", side_effect=[bad, good]) as get:
            parsed = rss._fetch_feed_parsed("https://example.com/feed.xml")

        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(parsed.entries), 1)

    def test_fetch_feed_sources_marks_fetch_failed_after_retries(self):
        with mock.patch.object(config, "HTTP_MAX_TRIES", 2), mock.patch.object(
            config, "HTTP_WAIT_SECONDS", 0
        ), mock.patch(
            "src.rss.requests.get",
            side_effect=requests.ConnectionError("reset"),
        ):
            items, stats = rss.fetch_feed_sources_with_stats(
                [{"id": "arxiv-cs-lg", "url": "https://rss.arxiv.org/rss/cs.LG"}]
            )

        self.assertEqual(items, [])
        self.assertTrue(str(stats["arxiv-cs-lg"]["error"]).startswith("fetch_failed:"))

    def test_fetch_feed_sources_happy_path(self):
        response = mock.Mock()
        response.status_code = 200
        response.content = _MINI_FEED
        response.raise_for_status = mock.Mock()

        with mock.patch("src.rss.requests.get", return_value=response):
            items, stats = rss.fetch_feed_sources_with_stats(
                [{"id": "demo", "url": "https://example.com/feed.xml"}]
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Hello")
        self.assertEqual(stats["demo"]["entries"], 1)
        self.assertIsNone(stats["demo"]["error"])

    def test_empty_body_is_retried_then_fails(self):
        empty = mock.Mock()
        empty.status_code = 200
        empty.content = b"   "
        empty.raise_for_status = mock.Mock()

        with mock.patch.object(config, "HTTP_MAX_TRIES", 2), mock.patch.object(
            config, "HTTP_WAIT_SECONDS", 0
        ), mock.patch("src.rss.requests.get", return_value=empty) as get:
            with self.assertRaises(requests.RequestException):
                rss._fetch_feed_parsed("https://example.com/feed.xml")
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
