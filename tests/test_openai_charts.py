from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import openai_charts, publish


def sample_spec() -> dict:
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "title": "Advanced Cybersecurity Completion Rate",
        "data": {
            "values": [
                {"model": "Sol", "value": 0.02},
                {"model": "Cyber", "value": 0.95},
            ]
        },
        "mark": "bar",
        "encoding": {
            "x": {
                "field": "model",
                "type": "nominal",
                "axis": {"labelAngle": {"expr": "dotcomContainerWidth < 600 ? -45 : 0"}},
            },
            "y": {"field": "value", "type": "quantitative"},
            "color": {
                "field": "model",
                "scale": {"range": ["theme1", "theme4"]},
            },
        },
    }


def next_html(spec: dict) -> str:
    flight = '7c:{"data":{"vegaLiteSpec":' + json.dumps(spec) + "}}"
    return (
        "<html><body><script>self.__next_f.push("
        + json.dumps([1, flight])
        + ")</script></body></html>"
    )


class OpenAiChartTest(unittest.TestCase):
    def test_extracts_vega_spec_from_next_flight_data(self):
        specs = openai_charts.extract_vega_specs(next_html(sample_spec()))
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["title"], "Advanced Cybersecurity Completion Rate")

    def test_renders_custom_dotcom_tokens_to_png(self):
        png = openai_charts.render_spec_png(sample_spec(), scale=1)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_writes_chart_files_with_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                openai_charts,
                "fetch_article_html",
                return_value=next_html(sample_spec()),
            ):
                files = openai_charts.write_article_charts(
                    "https://openai.com/index/demo",
                    directory,
                    "record/1",
                )
            self.assertEqual(
                files,
                [
                    {
                        "filename": "record-1-chart-1.png",
                        "alt": "Advanced Cybersecurity Completion Rate",
                    }
                ],
            )
            self.assertTrue((Path(directory) / files[0]["filename"]).exists())

    def test_static_site_adds_rendered_openai_charts(self):
        brief = {
            "date": "2026-08-11",
            "signals": [
                {
                    "recordId": "rec-daybreak",
                    "sourceId": "openai-news",
                    "source": "OpenAI News",
                    "contentType": "文章",
                    "url": "https://openai.com/index/daybreak",
                    "mediaAssets": {"images": [], "videos": []},
                    "imageUrl": "",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            def fake_write(_url, output_dir, prefix):
                path = Path(output_dir) / f"{prefix}-chart-1.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\x89PNG\r\n\x1a\n")
                return [{"filename": path.name, "alt": "评测完成率"}]

            with mock.patch.object(
                openai_charts,
                "write_article_charts",
                side_effect=fake_write,
            ):
                site = publish.build_site([brief], directory)
            payload = json.loads(
                (site / "data" / "brief-latest.json").read_text(encoding="utf-8")
            )
            signal = payload["signals"][0]
            self.assertEqual(
                signal["imageUrl"],
                "media/openai-charts/rec-daybreak-chart-1.png",
            )
            self.assertEqual(signal["mediaAssets"]["images"][0]["kind"], "openai-vega-chart")

    def test_static_site_mirrors_huxiu_images_to_avoid_hotlink_blocking(self):
        brief = {
            "signals": [
                {
                    "recordId": "rec-huxiu",
                    "source": "虎嗅",
                    "url": "https://www.huxiu.com/article/1.html",
                    "imageUrl": "https://img.huxiucdn.com/article/content/chart.png",
                    "mediaAssets": {
                        "images": [
                            {
                                "url": "https://img.huxiucdn.com/article/content/chart.png",
                                "alt": "模型成本对比",
                                "kind": "article-figure",
                            }
                        ],
                        "videos": [],
                    },
                }
            ]
        }
        response = mock.Mock()
        response.headers = {"content-type": "image/png"}
        response.content = b"\x89PNG\r\n\x1a\n"
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(publish.requests, "get", return_value=response):
                publish.mirror_huxiu_images([brief], directory)
            signal = brief["signals"][0]
            self.assertEqual(signal["imageUrl"], "media/huxiu/rec-huxiu-1.png")
            self.assertTrue((Path(directory) / "rec-huxiu-1.png").exists())

    @staticmethod
    def _social_brief() -> dict:
        return {
            "socialPosts": [
                {
                    "recordId": "rec-social",
                    "url": "https://x.com/dawnsongtweets/status/1",
                    "mediaAssets": {
                        "images": [],
                        "videos": [
                            {
                                "id": "16_1",
                                "playbackUrl": "https://video.twimg.com/tweet_video/a.mp4",
                                "thumbnailUrl": "https://pbs.twimg.com/tweet_video_thumb/a.jpg",
                            }
                        ],
                    },
                }
            ]
        }

    def test_static_site_mirrors_x_videos_to_avoid_hotlink_blocking(self):
        brief = self._social_brief()
        response = mock.Mock()
        response.headers = {"content-type": "video/mp4"}
        response.content = b"\x00\x00\x00\x18ftypmp42"
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(publish.requests, "get", return_value=response):
                publish.mirror_social_videos([brief], directory)
            video = brief["socialPosts"][0]["mediaAssets"]["videos"][0]
            self.assertEqual(video["playbackUrl"], "media/social/rec-social-1.mp4")
            self.assertTrue((Path(directory) / "rec-social-1.mp4").exists())

    def test_unreachable_x_video_drops_playback_url_instead_of_dead_player(self):
        brief = self._social_brief()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                publish.requests,
                "get",
                side_effect=publish.requests.RequestException("403"),
            ):
                publish.mirror_social_videos([brief], directory)
            video = brief["socialPosts"][0]["mediaAssets"]["videos"][0]
            self.assertNotIn("playbackUrl", video)
            self.assertEqual(
                video["thumbnailUrl"], "https://pbs.twimg.com/tweet_video_thumb/a.jpg"
            )


if __name__ == "__main__":
    unittest.main()
