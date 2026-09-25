import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("NTFY_TOPIC", "dedup-test-topic")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_news as news


class DuplicateDetectionTests(unittest.TestCase):
    def test_url_normalization_removes_tracking_and_fragments(self):
        first = news._canonical_url(
            "http://www.example.com/story/123/?utm_source=rss#comments")
        second = news._canonical_url("https://example.com/story/123")
        self.assertEqual(first, second)

    def test_same_article_with_different_links_is_duplicate_by_paraphrase(self):
        first = {
            "title": "Seven former Australia captains appeal for Imran Khan",
            "link": "https://outlet-one.example/story-a",
            "summary": "Former Australia cricket leaders made a humanitarian appeal for Imran Khan.",
        }
        second = {
            "title": "Allan Border, Steve Waugh among ex-Australia captains to make humanitarian plea for Imran Khan",
            "link": "https://outlet-two.example/story-b?utm_campaign=rss",
            "summary": "Former Australia cricket leaders made a humanitarian appeal for Imran Khan.",
        }
        self.assertTrue(news._same_article_entries(first, second))

    def test_same_canonical_link_is_duplicate_even_if_headline_changes(self):
        first = {"title": "Court announces new hearing date", "link": "https://example.com/story?utm_source=rss"}
        second = {"title": "Judges set date for next hearing", "link": "https://example.com/story#top"}
        self.assertTrue(news._same_article_entries(first, second))

    def test_unrelated_stories_about_same_person_are_not_duplicates(self):
        first = {
            "title": "Pakistan court rejects Imran Khan bail petition",
            "summary": "The court rejected the petition during a hearing in Islamabad.",
        }
        second = {
            "title": "Pakistan parliament approves annual budget after debate",
            "summary": "Lawmakers passed the national budget following debate in the assembly.",
        }
        self.assertFalse(news._same_article_entries(first, second))

    def test_persistent_cache_blocks_urls_and_paraphrased_stories(self):
        entry = {
            "title": "Border and Waugh join former Australia captains' appeal for Imran Khan",
            "link": "https://another.example/report",
            "summary": "Former Australia cricket leaders made a humanitarian appeal for Imran Khan.",
        }
        seen = {
            "old-id": {
                "ts": 2_000_000_000,
                "tt": "australia,captain,imran,khan,former,appeal",
                "st": "australia,captain,imran,khan,former,humanitarian,cricket,leader,appeal",
                "urls": ["https://old.example/report"],
            }
        }
        self.assertTrue(news.seen_story_match(entry, seen))
        self.assertFalse(news.seen_url_match(entry, seen))
        seen["old-id"]["urls"].append("https://another.example/report")
        self.assertTrue(news.seen_url_match(entry, seen))


if __name__ == "__main__":
    unittest.main()
