import os
from pathlib import Path
import sys
import time
import unittest

os.environ.setdefault("NTFY_TOPIC", "dedup-test-topic")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_news as news


class DuplicateDetectionTests(unittest.TestCase):
    def test_indian_actor_named_imran_khan_is_rejected(self):
        entry = {
            "title": "Indian actor Imran Khan returns to films after long break",
            "summary": "The Bollywood actor from India discussed upcoming film projects.",
        }
        self.assertFalse(news.is_important(entry, "international"))
        self.assertFalse(news.is_important(entry, "national"))

    def test_pakistani_politician_and_cricketer_imran_khan_is_kept(self):
        entries = [
            {"title": "Former Pakistan prime minister Imran Khan's appeal rejected"},
            {"title": "PTI says Imran Khan's detention extended"},
            {"title": "Former Pakistan cricket captain Imran Khan speaks out"},
            {"title": "Imran Khan, jailed former Pakistani premier, appears in court"},
        ]
        for entry in entries:
            with self.subTest(title=entry["title"]):
                self.assertTrue(news.is_important(entry, "international"))

    def test_ambiguous_imran_khan_mention_without_identity_cue_is_rejected(self):
        entry = {
            "title": "Imran Khan comments on latest developments",
            "summary": "The interview was published by an international outlet.",
        }
        self.assertFalse(news.is_important(entry, "international"))

    def test_explicit_actor_coverage_is_rejected_even_if_summary_mentions_politics(self):
        entry = {
            "title": "Bollywood actor Imran Khan on his career",
            "summary": "The actor briefly commented on Pakistan's former prime minister.",
        }
        self.assertFalse(news.is_important(entry, "international"))

    def test_url_normalization_removes_tracking_and_fragments(self):
        first = news._canonical_url(
            "http://www.example.com/story/123/?utm_source=rss#comments"
        )
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
        first = {
            "title": "Court announces new hearing date",
            "link": "https://example.com/story?utm_source=rss",
        }
        second = {
            "title": "Judges set date for next hearing",
            "link": "https://example.com/story#top",
        }
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

    def test_fetched_article_body_catches_different_headlines(self):
        first = {
            "title": "Australia cricket greats make plea for Imran Khan",
            "summary": "Former players call for humanitarian treatment.",
            "article_text": (
                "Allan Border and Steve Waugh joined other former Australia captains "
                "in a humanitarian appeal asking authorities to show compassion to "
                "Imran Khan during his detention."
            ),
            "link": "https://one.example/a",
        }
        second = {
            "title": "Ex-skipper group urges compassion for jailed Pakistani leader",
            "summary": "A group of cricket leaders issued an appeal.",
            "article_text": (
                "Former Australia captains Allan Border and Steve Waugh made a "
                "humanitarian appeal asking authorities to show compassion to "
                "Imran Khan during his detention."
            ),
            "link": "https://two.example/b",
        }
        self.assertTrue(news._same_article_entries(first, second))

    def test_page_extractor_provides_summary_without_html_boilerplate(self):
        parser = news._ArticlePageParser()
        parser.feed(
            "<html><head><title>Story title</title></head><body>"
            "<nav>Ignore navigation</nav><p>This is the first meaningful "
            "article paragraph with enough detail to summarize.</p>"
            "<script>ignore me</script></body></html>"
        )
        self.assertIn("Story title", parser.text())
        self.assertIn("first meaningful", parser.text())
        self.assertNotIn("ignore me", parser.text())

    def test_google_news_wrapper_is_not_used_as_article_summary(self):
        wrapper = (
            "Google News Comprehensive up-to-date news coverage, aggregated from "
            "sources all over the world by Google News. "
            "Comprehensive up-to-date news coverage, aggregated from sources all "
            "over the world by Google News."
        )
        self.assertTrue(news._is_google_news_boilerplate(wrapper))
        self.assertEqual(news._extractive_summary(wrapper), "")

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

    def test_72_hour_rolling_story_memory_retention(self):
        self.assertEqual(news.SEEN_SCAN_HOURS, 72)
        now = time.time()
        seen = {
            "within-48h": {"ts": int(now - 48 * 3600), "tt": "imran,khan"},
            "older-than-72h": {"ts": int(now - 75 * 3600), "tt": "old,story"},
        }
        pruned = news._prune_seen(seen)
        self.assertIn("within-48h", pruned)
        self.assertNotIn("older-than-72h", pruned)

    def test_story_evolution_detects_significant_update_vs_duplicate(self):
        initial_entry = {
            "title": "Microsoft announces layoffs across global divisions",
            "summary": "Microsoft announced plans for workforce layoffs as part of a review.",
            "link": "https://reuters.example/msft-layoffs",
        }
        stored_record = news.build_memory_record(
            initial_entry, "Reuters", "story-msft-1", "NEW STORY"
        )
        seen = {"story-msft-1": stored_record}

        # Duplicate report from another source with no new figures
        bloomberg_dup = {
            "title": "Microsoft plans job cuts across global divisions",
            "summary": "Microsoft announced plans for workforce layoffs as part of a review.",
            "link": "https://bloomberg.example/msft-cuts",
        }
        classification_dup, _, _ = news.evaluate_entry_against_memory(
            bloomberg_dup, seen, "Bloomberg"
        )
        self.assertEqual(classification_dup, "DUPLICATE")

        # Significant update with confirmed 9,000 figure and restructuring details
        update_entry = {
            "title": "Microsoft confirms 9,000 layoffs and publishes restructuring details",
            "summary": (
                "Microsoft officially confirmed 9,000 layoffs across global divisions "
                "and published full restructuring details and severance packages."
            ),
            "link": "https://reuters.example/msft-9000-confirmed",
        }
        classification_upd, matched_key, _ = news.evaluate_entry_against_memory(
            update_entry, seen, "Reuters"
        )
        self.assertEqual(classification_upd, "SIGNIFICANT UPDATE")
        self.assertEqual(matched_key, "story-msft-1")

    def test_legacy_cache_entries_never_retrigger_as_significant_updates(self):
        legacy_seen = {
            "legacy-1": {
                "ts": int(time.time()),
                "tt": "govt,price,fuel,per,litre,high,speed",
                "st": "govt,price,fuel,per,litre,high,speed,wednesday",
                "urls": ["https://www.dawn.com/news/2041886"],
            }
        }
        incoming = {
            "title": "Govt cuts petrol price by Rs5.45 per litre, high-speed diesel by Rs3.68",
            "summary": "Petrol will retail at Rs369.06 per litre on Wednesday.",
            "link": "https://another.example/petrol-cut",
        }
        classification, _, _ = news.evaluate_entry_against_memory(incoming, legacy_seen, "Dawn")
        self.assertEqual(classification, "DUPLICATE")

    def test_step_7_delivery_format_contains_required_fields_and_link(self):
        entry = {
            "title": "Islamabad High Court hears Imran Khan bail petition",
            "summary": "The Islamabad High Court heard arguments in the bail petition in Islamabad.",
            "link": "https://www.dawn.com/news/123456",
        }
        msg = news.format_message(entry, "Dawn", "national", story_type="NEW STORY")
        self.assertIn("Summary:", msg)
        self.assertIn("Key Facts: •", msg)
        self.assertIn("Source: Dawn", msg)
        self.assertIn("Published:", msg)
        self.assertIn("Story Type: NEW STORY", msg)
        self.assertIn("Link: https://www.dawn.com/news/123456", msg)
        self.assertFalse(news.send_to_ntfy(msg, title="Test", link=""))


if __name__ == "__main__":
    unittest.main()
