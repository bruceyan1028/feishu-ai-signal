from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import config, cover_image, publish


class GeneratedCoverTest(unittest.TestCase):
    def test_generation_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            result = cover_image.generate_cover(
                {"contentType": "文章", "titleCn": "测试新闻"}, Path(directory)
            )
        self.assertIsNone(result)

    @mock.patch.object(cover_image, "_validate_generated_cover", return_value=(True, ""))
    @mock.patch.object(cover_image.requests, "post")
    def test_generated_cover_is_cached_and_marked(self, post, _validate):
        response = mock.Mock()
        response.json.return_value = {
            "data": [{"b64_json": base64.b64encode(b"png-data").decode("ascii")}]
        }
        response.raise_for_status.return_value = None
        post.return_value = response
        brief = {
            "signals": [
                {
                    "contentType": "文章",
                    "titleCn": "没有原图的新闻",
                    "summary": "摘要",
                    "imageUrl": "",
                    "mediaAssets": {"images": []},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "IMAGE_GENERATION_ENABLED", True
        ), mock.patch.object(config, "IMAGE_GENERATION_API_KEY", "image-key"):
            cover_image.fill_missing_covers([brief], Path(directory))
            generated = brief["signals"][0]
            self.assertTrue((Path(directory) / Path(generated["imageUrl"]).name).exists())
        self.assertEqual(generated["mediaAssets"]["coverKind"], "generated-cover")
        self.assertEqual(generated["mediaAssets"]["curatedBy"], "generated")

    @mock.patch.object(cover_image, "_validate_generated_cover", return_value=(True, ""))
    @mock.patch.object(cover_image.requests, "post")
    def test_gemini_chat_image_response_is_supported(self, post, _validate):
        response = mock.Mock()
        encoded = base64.b64encode(b"gemini-png").decode("ascii")
        response.json.return_value = {
            "choices": [
                {"message": {"content": f"![image](data:image/png;base64,{encoded})"}}
            ]
        }
        response.raise_for_status.return_value = None
        post.return_value = response
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "IMAGE_GENERATION_ENABLED", True
        ), mock.patch.object(
            config, "IMAGE_GENERATION_API_KEY", "image-key"
        ), mock.patch.object(
            config, "IMAGE_GENERATION_MODEL", "gemini-3.1-flash-image"
        ), mock.patch.object(config, "IMAGE_GENERATION_ENDPOINT", "auto"):
            result = cover_image.generate_cover(
                {"contentType": "文章", "titleCn": "云端模型发布"}, Path(directory)
            )
            self.assertIsNotNone(result)
            self.assertEqual((Path(directory) / Path(result["url"]).name).read_bytes(), b"gemini-png")
        self.assertTrue(post.call_args.args[0].endswith("/chat/completions"))
        self.assertEqual(post.call_args.kwargs["json"]["modalities"], ["text", "image"])

    @mock.patch.object(
        cover_image, "_validate_generated_cover", side_effect=[(False, "contains text"), (True, "")]
    )
    @mock.patch.object(cover_image.requests, "post")
    def test_failed_visual_review_retries_generation(self, post, validate):
        response = mock.Mock()
        response.json.return_value = {
            "data": [{"b64_json": base64.b64encode(b"png-data").decode("ascii")}]
        }
        response.raise_for_status.return_value = None
        post.return_value = response
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "IMAGE_GENERATION_ENABLED", True
        ), mock.patch.object(config, "IMAGE_GENERATION_API_KEY", "image-key"):
            result = cover_image.generate_cover(
                {"contentType": "文章", "titleCn": "测试新闻"}, Path(directory)
            )
        self.assertIsNotNone(result)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(validate.call_count, 2)
        payload = post.call_args.kwargs["json"]
        retry_prompt = payload.get("prompt") or payload["messages"][0]["content"]
        self.assertIn("contains text", retry_prompt)


class PaperCoverTest(unittest.TestCase):
    @mock.patch.object(publish.paper_fulltext, "write_visual_page_images")
    def test_first_rendered_pdf_figure_becomes_card_cover(self, render):
        render.return_value = [{"filename": "paper-p2-f1.png", "alt": "实验结果"}]
        brief = {
            "date": "2026-09-10",
            "signals": [],
            "paperSignals": [
                {
                    "recordId": "paper-1",
                    "contentType": "论文",
                    "imageUrl": "",
                    "mediaAssets": {"images": []},
                    "pdfUrl": "https://example.com/paper.pdf",
                    "paperFullTextSource": "pdf",
                    "paperVisualPages": [2],
                    "paperCaptions": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            site = publish.build_site([brief], directory)
            payload = json.loads(
                (site / "data" / "brief-latest.json").read_text(encoding="utf-8")
            )
        paper = payload["paperSignals"][0]
        self.assertEqual(paper["imageUrl"], "media/papers/paper-p2-f1.png")
        self.assertEqual(paper["mediaAssets"]["cover"], paper["imageUrl"])
