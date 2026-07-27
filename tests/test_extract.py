"""正文抽取：整页择块与 README 清洗。"""
from __future__ import annotations

import unittest

from src import rss, scrape

_PAGE = """
<html><body>
<nav><a href="/research">Research</a><a href="/news">News</a></nav>
<a href="#main">Skip to main content</a>
<article>
  <h1>Introducing Claude Opus 5</h1>
  <p>Jul 24, 2026</p>
  <p>Claude Opus 5 is available today. It is a thoughtful and proactive model that comes
  close to the frontier intelligence of the previous generation at half the price.</p>
  <p>On coding and knowledge work evaluations, Opus 5 is the new state of the art, though
  it remains behind on cybersecurity tasks according to the published benchmark tables.</p>
</article>
<footer>Skip to footer</footer>
</body></html>
"""


class ParseArticleHtmlTest(unittest.TestCase):
    def test_drops_navigation_and_starts_at_real_content(self):
        text = rss.parse_article_html(_PAGE, "https://example.com/a", "Introducing Claude Opus 5")["text"]
        self.assertTrue(text.startswith("Claude Opus 5 is available today"), text[:80])
        for noise in ("Skip to main content", "Skip to footer", "Research", "Jul 24, 2026"):
            self.assertNotIn(noise, text)

    def test_keeps_paragraph_breaks(self):
        text = rss.parse_article_html(_PAGE, "https://example.com/a", "")["text"]
        self.assertIn("\n\n", text)


class ReadmeToTextTest(unittest.TestCase):
    def test_html_in_readme_leaves_no_tag_fragments(self):
        raw = (
            '<p align="center">\n<a href="https://ollama.com">\n'
            '<img src="https://example.com/logo.png" alt="ollama" width="200"/>\n</a>\n</p>\n\n'
            "# Ollama\n\nGet up and running with large language models.\n"
        )
        text = scrape.readme_to_text(raw)
        for fragment in ("<p", "<a", "<img", "</a", "</p", 'align="center"'):
            self.assertNotIn(fragment, text)
        self.assertIn("Get up and running with large language models.", text)

    def test_keeps_paragraphs_and_table_pipes(self):
        raw = "First paragraph.\n\nSecond paragraph.\n\n| Model | Size |\n| --- | --- |\n| a | 1B |\n"
        text = scrape.readme_to_text(raw)
        self.assertIn("First paragraph.\n\nSecond paragraph.", text)
        self.assertIn("| Model | Size |", text)

    def test_strips_markdown_links_and_headings(self):
        text = scrape.readme_to_text("## Title\n\nSee [the docs](https://example.com/docs) now.\n")
        self.assertNotIn("](", text)
        self.assertNotIn("##", text)
        self.assertIn("See the docs now.", text)


if __name__ == "__main__":
    unittest.main()
