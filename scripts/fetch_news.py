#!/usr/bin/env python3
"""
Fetch half-hourly news from Pakistani English-language & international RSS feeds,
extract full editorial content, build a Canonical Story Summary + Event Fingerprint,
maintain a 72-hour Rolling Story Memory Database, detect duplicates & story updates,
and deliver only NEW STORY or SIGNIFICANT UPDATE alerts (always with clickable links)
to ntfy.sh.

Usage:
    NTFY_TOPIC=NewsPakistan_xxxxx  python3 scripts/fetch_news.py

Environment variables:
    NTFY_TOPIC      - ntfy topic to post to  (required)
    NTFY_SERVER     - ntfy server URL         (default: https://ntfy.sh)
    GEMINI_API_KEY  - optional Gemini API key for LLM-assisted fingerprinting
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import feedparser
import requests

# ─── Configuration ────────────────────────────────────────────────────────────

NTFY_TOPIC = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
STATE_FILE = Path(__file__).resolve().parent.parent / "seen_articles.json"

if not NTFY_TOPIC:
    print("ERROR: NTFY_TOPIC environment variable is not set.", file=sys.stderr)
    print("Set it before running:", file=sys.stderr)
    print("  NTFY_TOPIC=your_topic_name python3 scripts/fetch_news.py", file=sys.stderr)
    sys.exit(1)

PKT = timezone(timedelta(hours=5))

WINDOW_MINUTES = 6 * 60       # 360 minutes (6 hours) for incoming feed items
MAX_PER_RUN = 12              # hard cap per cron run
FETCH_TIMEOUT = 12            # seconds per feed — skip slow/hanging feeds
ARTICLE_TIMEOUT = 6           # seconds per article page
ARTICLE_TEXT_LIMIT = 15_000   # bound memory and state size for extracted pages
MAX_ARTICLE_ENRICH = 20       # fetch full article content for candidate items
RUN_DEADLINE = 170            # hard cap for the whole script (s)

# Step 3: 72-Hour Rolling Story Memory retention period
SEEN_SCAN_HOURS = 72          # Keep records for 72 hours; auto-prune older entries

# Step 5: Similarity Thresholds
DUPLICATE_HIGH_THRESHOLD = 0.85   # >= 85% semantic similarity -> DUPLICATE
DUPLICATE_LOW_THRESHOLD = 0.70    # 70-84% similarity -> check entities & facts

# Legacy token similarity settings preserved for helper compatibility
COSINE_THRESHOLD = 0.40
MIN_SHARED_TOKENS = 3
FINGERPRINT_JACCARD = 0.60
FINGERPRINT_SIZE = 5

# Source priority — lower number wins when two articles describe the same story
SOURCE_PRIORITY = {
    "Dawn": 1,
    "Business Recorder": 2,
    "GNews Pakistan": 3,
    "GNews Pakistan Intl": 4,
    "GNews Imran Intl": 5,
}

# ─── RSS feeds ────────────────────────────────────────────────────────────────

RSS_FEEDS = [
    {"name": "Dawn", "url": "https://www.dawn.com/feeds/home", "scope": "national"},
    {
        "name": "Business Recorder",
        "url": "https://news.google.com/rss/search?q=site:brecorder.com+Pakistan&hl=en-PK&gl=PK&ceid=PK:en",
        "scope": "national",
    },
    {
        "name": "GNews Pakistan",
        "url": "https://news.google.com/rss/search?q=Pakistan+news+today&hl=en-PK&gl=PK&ceid=PK:en",
        "scope": "national",
    },
    {
        "name": "GNews Pakistan Intl",
        "url": "https://news.google.com/rss/search?q=Pakistan+world+news&hl=en-PK&gl=PK&ceid=PK:en",
        "scope": "international",
    },
    {
        "name": "GNews Imran Intl",
        "url": "https://news.google.com/rss/search?q=Imran+Khan&hl=en-US&gl=US&ceid=US:en",
        "fallback_url": "https://news.google.com/rss/search?q=%22Imran+Khan%22+Pakistan&hl=en-GB&gl=GB&ceid=GB:en",
        "scope": "international",
    },
]

# ─── Priority / importance keywords ───────────────────────────────────────────

HIGH_PRIORITY_KEYWORDS = [
    "pakistan", "pakistani", "islamabad", "karachi", "lahore", "peshawar",
    "quetta", "gilgit", "muzaffarabad", "kashmir", "balochistan", "sindh",
    "punjab", "kpk", "pak army", "pakistan army", "isi", "supreme court",
    "imran khan", "imran", "shehbaz", "zardari", "bhutto", "nawaz", "cpec",
    "election", "parliament", "senate", "national assembly", "nuclear",
    "telenor", "onic", "ufone", "e&", "ptcl", "etisalat", "jazz", "zong",
    "lahore high court", "lahore h.c.", "lhc",
    "islamabad high court", "islamabad h.c.", "ihc",
    "constitutional court", "constitution court", "federal constitutional court",
    "constitution bench",
    "fuel price", "petrol price", "diesel price", "petroleum",
    "petrol", "diesel", "fuel relief", "fuel rate",
    "petrol rate", "fuel hike", "petrol hike", "gasoline",
    "electricity", "gas price", "gas tariff", "power tariff", "nepra", "ogra",
    "road closure", "protest", "dharna", "motorway", "red zone",
    "jemima", "qasim khan", "sulaiman khan", "bushra bibi",
    "pti", "pakistan tehreek-e-insaf",
    "india", "afghan", "iran", "china", "united states", "nato", "oil",
    "gaza", "ukraine", "russia", "un security council", "imf", "world bank",
]


def _build_keyword_regex(keywords: list[str]) -> re.Pattern:
    parts = []
    for kw in keywords:
        if re.fullmatch(r"[\w ]+", kw):
            parts.append(r"\b" + re.escape(kw) + r"\b")
        else:
            parts.append(r"(?:^|\W)" + re.escape(kw) + r"(?:\W|$)")
    return re.compile("|".join(parts), re.IGNORECASE)


KEYWORD_RE = _build_keyword_regex(HIGH_PRIORITY_KEYWORDS)

IMRAN_KHAN_RE = re.compile(r"\bImran\s+Khan\b", re.IGNORECASE)
IMRAN_PAKISTAN_IDENTITY_RE = re.compile(
    r"\b(?:pti\b|pakistan\s+tehreek[- ]e[- ]insaf|"
    r"(?:pakistan(?:i|['’]s)?\s+)?(?:former\s+|ex[- ]|jailed\s+|imprisoned\s+)?"
    r"(?:prime\s+minister|premier|cricketer|cricket\s+captain|politician)|"
    r"(?:former|ex[- ])\s*(?:pakistan(?:i)?\s+)?(?:prime\s+minister|premier|"
    r"cricketer|cricket\s+captain|captain\s+of\s+pakistan|politician)|"
    r"pakistan['’]s\s+(?:ousted|jailed|imprisoned)\s+(?:premier|prime\s+minister)|"
    r"former\s+captain\s+of\s+pakistan)\b",
    re.IGNORECASE,
)
IMRAN_INDIAN_ACTOR_RE = re.compile(
    r"\b(?:bollywood|indian\s+actor|actor\s+from\s+india|film\s+actor)\b",
    re.IGNORECASE,
)

# ─── Tokenisation, Synonym Canonicalisation & Entity/Event Extraction ─────────

STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
    "be", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "it", "its", "he",
    "she", "they", "we", "you", "i", "me", "my", "his", "her", "their",
    "our", "your", "this", "that", "these", "those", "not", "no", "nor",
    "if", "then", "else", "when", "up", "out", "about", "into", "over",
    "after", "before", "between", "under", "same", "than", "too", "very",
    "just", "also", "says", "said", "say", "new", "one", "two", "three",
    "first", "per", "via", "set", "back", "more", "other", "some",
    "href", "https", "http", "www", "com", "net", "org", "html", "nbsp",
    "font", "color", "div", "span", "rss", "articles", "oc", "target",
    "title", "article", "content", "category", "feed", "google", "news",
    "told", "reuters", "us", "pakistani", "pakistan", "cent", "pc",
    "week", "today", "year", "month", "day", "getty", "afp", "ap",
    "file", "photo", "image", "latest", "live", "update", "read",
    "related", "against", "following", "ahead", "amid", "why", "how",
    "what", "where", "when", "who", "whom", "which", "while", "during",
    "through", "across", "around", "within", "without", "upon", "towards",
    "toward", "regarding", "concerning", "despite", "whether", "watch",
    "info",
}

CANONICAL_SYNONYMS = {
    "plea": "appeal",
    "pleas": "appeal",
    "petition": "appeal",
    "petitions": "appeal",
    "petitioner": "appeal",
    "appeals": "appeal",
    "case": "appeal",
    "cases": "appeal",
    "suit": "appeal",
    "suits": "appeal",
    "skipper": "captain",
    "skippers": "captain",
    "captains": "captain",
    "greats": "captain",
    "leaders": "leader",
    "premier": "minister",
    "pm": "minister",
    "jailed": "detention",
    "imprisoned": "detention",
    "incarcerated": "detention",
    "custody": "detention",
    "prison": "detention",
    "jail": "detention",
    "detained": "detention",
    "detain": "detention",
    "detains": "detention",
    "arrest": "detention",
    "arrests": "detention",
    "arrested": "detention",
    "compassion": "humanitarian",
    "mercy": "humanitarian",
    "treatment": "humanitarian",
    "urges": "appeal",
    "urge": "appeal",
    "urged": "appeal",
    "calls": "appeal",
    "called": "appeal",
    "asks": "appeal",
    "asked": "appeal",
    "dismisses": "reject",
    "dismissed": "reject",
    "rejects": "reject",
    "rejected": "reject",
    "denies": "reject",
    "denied": "reject",
    "turns": "reject",
    "quashes": "reject",
    "quashed": "reject",
    "bars": "bar",
    "barred": "bar",
    "barring": "bar",
    "restrains": "bar",
    "restrained": "bar",
    "restrain": "bar",
    "halts": "bar",
    "halted": "bar",
    "halt": "bar",
    "halting": "bar",
    "postpone": "bar",
    "postpones": "bar",
    "postponed": "bar",
    "postponing": "bar",
    "delays": "bar",
    "delayed": "bar",
    "delay": "bar",
    "suspends": "bar",
    "suspended": "bar",
    "suspend": "bar",
    "stays": "bar",
    "stayed": "bar",
    "blocks": "bar",
    "blocked": "bar",
    "stops": "bar",
    "stopped": "bar",
    "hearing": "hear",
    "hearings": "hear",
    "hears": "hear",
    "heard": "hear",
    "polls": "election",
    "poll": "election",
    "polling": "election",
    "elections": "election",
    "vote": "election",
    "voting": "election",
    "ballot": "election",
    "fixes": "schedule",
    "fixed": "schedule",
    "sets": "schedule",
    "schedules": "schedule",
    "scheduled": "schedule",
    "announces": "schedule",
    "announced": "schedule",
    "approves": "approve",
    "approved": "approve",
    "clears": "approve",
    "cleared": "approve",
    "grants": "approve",
    "granted": "approve",
    "passes": "approve",
    "passed": "approve",
    "hike": "price",
    "hikes": "price",
    "increase": "price",
    "increases": "price",
    "raised": "price",
    "raises": "price",
    "slashed": "price",
    "cut": "price",
    "cuts": "price",
    "rates": "price",
    "rate": "price",
    "prices": "price",
    "petrol": "fuel",
    "petroleum": "fuel",
    "gasoline": "fuel",
    "diesel": "fuel",
    "layoffs": "layoff",
    "job": "layoff",
    "jobs": "layoff",
    "acquisition": "acquire",
    "acquires": "acquire",
    "acquired": "acquire",
    "buyout": "acquire",
    "buys": "acquire",
    "bought": "acquire",
    "merger": "acquire",
    "quake": "earthquake",
    "tremor": "earthquake",
    "tremors": "earthquake",
    "seismic": "earthquake",
    "blast": "explosion",
    "blasts": "explosion",
    "bombing": "explosion",
    "january": "jan",
    "february": "feb",
    "march": "mar",
    "april": "apr",
    "june": "jun",
    "july": "jul",
    "august": "aug",
    "september": "sep",
    "sept": "sep",
    "october": "oct",
    "november": "nov",
    "novembar": "nov",
    "december": "dec",
    "clash": "match",
    "broadcast": "match",
    "streaming": "match",
}

PHRASE_NORMALIZATIONS = [
    (re.compile(r"\bfederal\s+constitutional\s+court\b", re.I), "fcc"),
    (re.compile(r"\bconstitutional\s+court\b", re.I), "fcc"),
    (re.compile(r"\bconstitution\s+court\b", re.I), "fcc"),
    (re.compile(r"\bpeshawar\s+high\s+court\b", re.I), "phc"),
    (re.compile(r"\bislamabad\s+high\s+court\b", re.I), "ihc"),
    (re.compile(r"\blahore\s+high\s+court\b", re.I), "lhc"),
    (re.compile(r"\bsindh\s+high\s+court\b", re.I), "shc"),
    (re.compile(r"\bbalochistan\s+high\s+court\b", re.I), "bhc"),
    (re.compile(r"\bsupreme\s+court(?:\s+of\s+pakistan)?\b", re.I), "sc"),
    (re.compile(r"\belection\s+commission(?:\s+of\s+pakistan)?\b", re.I), "ecp"),
    (re.compile(r"\bmaintenance\s+of\s+public\s+order(?:\s+ordinance)?\b", re.I), "mpo"),
    (re.compile(r"\b3[-\s]?mpo\b", re.I), "mpo"),
    (re.compile(r"\bpakistan\s+tehreek[-\s]e[-\s]insaf\b", re.I), "pti"),
    (re.compile(r"\btehreek[-\s]e[-\s]insaf\b", re.I), "pti"),
    (re.compile(r"\bkhyber\s+pakhtunkhwa\b", re.I), "kp"),
    (re.compile(r"\bkpk\b", re.I), "kp"),
    (re.compile(r"\blocal\s+(?:government|govt)\b", re.I), "localgov"),
    (re.compile(r"\blocal\s+(?:elections?|polls?)\b", re.I), "localgov election"),
    (re.compile(r"\b(?:india\s+(?:vs?\.?\s+)?pakistan|pakistan\s+(?:vs?\.?\s+)?india|ind\s+vs?\.?\s+pak|pak\s+vs?\.?\s+ind)\b", re.I), "indiapakistan match"),
    (re.compile(r"\bhead[-\s]to[-\s]head\b", re.I), "match"),
]

_GNEWS_SUFFIX_RE = re.compile(r"\s+-\s*[^-|]+$")
_GNEWS_FONT_RE = re.compile(r"<font\b[^>]*>.*?</font>", re.IGNORECASE | re.DOTALL)
_SITE_BANNER_RE = re.compile(
    r"(?:\s*-\s*(?:World|Pakistan|Business|Sport|Opinion|Editorial|Tech)\s*-\s*DAWN\.COM|"
    r"Get the latest news and updates from Dawn\s*)",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    cleaned = _GNEWS_FONT_RE.sub(" ", raw)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = (
        cleaned.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
    )
    cleaned = _SITE_BANNER_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _normalize_phrases(text: str) -> str:
    normalized = text
    for pattern, replacement in PHRASE_NORMALIZATIONS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def _tokenize(text: str) -> list[str]:
    cleaned = _normalize_phrases(_strip_html(text))
    words = re.findall(r"\b[a-z]{2,}\b", cleaned.lower())
    tokens = []
    for word in words:
        if word in CANONICAL_SYNONYMS:
            word = CANONICAL_SYNONYMS[word]
        elif len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 4 and word.endswith("es") and not word.endswith(("ses", "xes", "zes", "ches", "shes")):
            stem = word[:-1]
            if stem in CANONICAL_SYNONYMS:
                word = CANONICAL_SYNONYMS[stem]
            elif len(word) > 5:
                word = word[:-1]
        elif len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
        if word in CANONICAL_SYNONYMS:
            word = CANONICAL_SYNONYMS[word]
        if word not in STOP_WORDS:
            tokens.append(word)
    return tokens


def _sim_text(entry: dict) -> str:
    """Title only, URL + Google News source suffix stripped."""
    t = re.sub(r"https?://\S+", "", entry.get("title") or "")
    return _GNEWS_SUFFIX_RE.sub("", t).strip()


def _cosine_sim(s1: set, s2: set) -> float:
    if not s1 or not s2:
        return 0.0
    shared = s1 & s2
    return len(shared) / math.sqrt(len(s1) * len(s2))


def _canonical_url(url: str) -> str:
    """Normalize article URLs, removing fragments and common tracking params."""
    if not url:
        return ""
    try:
        parts = urlsplit(unquote(url.strip()))
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return ""
        host = parts.netloc.lower().split(":", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower.startswith("utm_") or key_lower in {
                "fbclid", "gclid", "dclid", "mc_cid", "mc_eid",
                "oc", "ceid", "hl", "gl",
            }:
                continue
            query.append((key, value))
        path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
        return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))
    except Exception:
        return ""


def _entry_urls(entry: dict) -> set[str]:
    values = [entry.get("link", "")]
    values.extend(
        item.get("href", "")
        for item in entry.get("links", [])
        if isinstance(item, dict)
    )
    return {normalized for value in values if (normalized := _canonical_url(value))}


def _story_tokens(entry: dict) -> set[str]:
    """Content-bearing title, RSS summary, and fetched-page terms."""
    title = _sim_text(entry)
    summary = _GNEWS_SUFFIX_RE.sub("", _strip_html(entry.get("summary") or "")).strip()
    page = entry.get("article_text") or ""
    return set(_tokenize(f"{title} {summary} {page}"))


# ─── STEP 1: Fetch Full Content & Strip Boilerplate ───────────────────────────

class _ArticlePageParser(HTMLParser):
    """Dependency-free extractor for clean editorial article text."""

    _SKIP = {
        "script", "style", "noscript", "svg", "nav", "footer",
        "header", "aside", "form", "iframe", "button", "figure", "figcaption",
    }
    _BOILERPLATE_MARKERS = (
        "all rights reserved",
        "copyright",
        "subscribe to our",
        "sign up for our",
        "follow us on",
        "read more:",
        "also read:",
        "related stories",
        "advertisement",
        "sponsored content",
        "cookie policy",
        "privacy policy",
        "get the latest news and updates from dawn",
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._title = []
        self._meta = []
        self._paragraphs = []
        self._current = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title" and not self._skip:
            self._in_title = True
        elif tag == "meta" and not self._skip:
            key = (attrs_dict.get("name") or attrs_dict.get("property") or "").lower()
            if key in {"description", "og:description", "twitter:description"}:
                content = (attrs_dict.get("content") or "").strip()
                if content and not any(m in content.lower() for m in self._BOILERPLATE_MARKERS):
                    self._meta.append(content)
        elif tag == "p" and not self._skip:
            self._current = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "p" and self._current and not self._skip:
            text = " ".join("".join(self._current).split())
            lower_text = text.lower()
            if len(text) >= 40 and not any(m in lower_text for m in self._BOILERPLATE_MARKERS):
                self._paragraphs.append(text)
            self._current = []

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self._title.append(data)
        elif self._current is not None:
            self._current.append(data)

    def text(self) -> str:
        clean_title = _SITE_BANNER_RE.sub("", " ".join("".join(self._title).split())).strip()
        parts = []
        if clean_title:
            parts.append(clean_title)
        parts.extend(self._paragraphs[:100])
        if not self._paragraphs and self._meta:
            parts.extend(self._meta)
        return " ".join(p.strip() for p in parts if p.strip())[:ARTICLE_TEXT_LIMIT]


def _is_google_news_boilerplate(text: str) -> bool:
    """Detect the generic Google News wrapper returned for RSS article URLs."""
    normalized = " ".join((text or "").lower().split())
    marker = "comprehensive up-to-date news coverage, aggregated from sources all over the world by google news"
    return normalized.count(marker) >= 1


def _extractive_summary(text: str, limit: int = 280) -> str:
    """Return a short page-derived summary without requiring an LLM/API key."""
    if _is_google_news_boilerplate(text):
        return ""
    cleaned_text = _SITE_BANNER_RE.sub(" ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", cleaned_text)
    chosen = []
    seen_norm = set()
    for sentence in sentences:
        sentence = " ".join(sentence.split())
        norm = re.sub(r"\W+", "", sentence.lower())[:50]
        if len(sentence) >= 40 and norm not in seen_norm:
            seen_norm.add(norm)
            chosen.append(sentence)
        if len(" ".join(chosen)) >= limit:
            break
    summary = " ".join(chosen)
    return summary[:limit].rstrip() + ("…" if len(summary) > limit else "")


def _canonical_summary_100_150_words(title: str, rss_summary: str, article_text: str) -> str:
    """Build a 100-150 word canonical summary from the full editorial content."""
    clean_title = _GNEWS_SUFFIX_RE.sub("", _strip_html(title or "")).strip()
    clean_rss = _GNEWS_SUFFIX_RE.sub("", _strip_html(rss_summary or "")).strip()
    clean_body = _strip_html(article_text or "").strip()
    if _is_google_news_boilerplate(clean_body):
        clean_body = ""

    # Remove title repetition from the start of body
    if clean_body.lower().startswith(clean_title.lower()):
        clean_body = clean_body[len(clean_title):].lstrip(" .,-—:")

    raw_source = f"{clean_body} {clean_rss}".strip() or clean_title
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw_source) if len(s.strip()) >= 30]

    title_norm = re.sub(r"\W+", "", clean_title.lower())[:45]
    seen_sent = {title_norm} if title_norm else set()
    selected = []
    word_count = 0

    for sent in sentences:
        norm = re.sub(r"\W+", "", sent.lower())[:50]
        if norm in seen_sent:
            continue
        seen_sent.add(norm)
        words = sent.split()
        selected.append(sent)
        word_count += len(words)
        if word_count >= 135:
            break

    summary_text = " ".join(selected).strip()
    if not summary_text:
        summary_text = clean_rss if clean_rss and clean_rss != clean_title else clean_title

    words = summary_text.split()
    if len(words) > 150:
        summary_text = " ".join(words[:148]).rstrip(".,;:") + "."
    return summary_text


def _fetch_article_page(url: str) -> tuple[str, str]:
    """Step 1: Open article URL and extract full editorial text + summary."""
    canonical = _canonical_url(url)
    if not canonical:
        return "", ""
    try:
        response = requests.get(
            canonical,
            timeout=(3, ARTICLE_TIMEOUT),
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        parser = _ArticlePageParser()
        parser.feed(response.text)
        text = parser.text()
        if _is_google_news_boilerplate(text):
            print(
                f"    ⚠  Google News wrapper detected; keeping RSS data only: {canonical}",
                file=sys.stderr,
            )
            return "", ""
        return text, _extractive_summary(text)
    except Exception as exc:
        print(f"    ⚠  Article page unavailable ({canonical}): {exc}", file=sys.stderr)
        return "", ""


# ─── STEP 2: Canonical Entities, Key Facts, Event Fingerprint & Embedding ─────

KNOWN_PEOPLE = {
    "imran khan": "Imran Khan",
    "shehbaz sharif": "Shehbaz Sharif",
    "shehbaz": "Shehbaz Sharif",
    "nawaz sharif": "Nawaz Sharif",
    "asif ali zardari": "Asif Ali Zardari",
    "zardari": "Asif Ali Zardari",
    "bilawal bhutto": "Bilawal Bhutto",
    "maryam nawaz": "Maryam Nawaz",
    "asim munir": "Asim Munir",
    "bushra bibi": "Bushra Bibi",
    "jemima goldsmith": "Jemima Goldsmith",
    "jemima": "Jemima Goldsmith",
    "qasim khan": "Qasim Khan",
    "sulaiman khan": "Sulaiman Khan",
    "allan border": "Allan Border",
    "steve waugh": "Steve Waugh",
    "donald trump": "Donald Trump",
    "joe biden": "Joe Biden",
    "vladimir putin": "Vladimir Putin",
    "volodymyr zelensky": "Volodymyr Zelensky",
    "benjamin netanyahu": "Benjamin Netanyahu",
    "xi jinping": "Xi Jinping",
    "narendra modi": "Narendra Modi",
}

KNOWN_ORGS = {
    "pti": "PTI",
    "pakistan tehreek-e-insaf": "PTI",
    "pml-n": "PML-N",
    "pmln": "PML-N",
    "ppp": "PPP",
    "supreme court": "Supreme Court",
    "islamabad high court": "Islamabad High Court",
    "ihc": "Islamabad High Court",
    "lahore high court": "Lahore High Court",
    "lhc": "Lahore High Court",
    "election commission": "ECP",
    "ecp": "ECP",
    "imf": "IMF",
    "world bank": "World Bank",
    "un security council": "UN Security Council",
    "nato": "NATO",
    "pakistan army": "Pakistan Army",
    "pak army": "Pakistan Army",
    "isi": "ISI",
    "nepra": "NEPRA",
    "ogra": "OGRA",
    "fbr": "FBR",
    "state bank": "State Bank of Pakistan",
    "sbp": "State Bank of Pakistan",
    "ptcl": "PTCL",
    "telenor": "Telenor",
    "jazz": "Jazz",
    "ufone": "Ufone",
    "zong": "Zong",
    "microsoft": "Microsoft",
    "reuters": "Reuters",
    "bloomberg": "Bloomberg",
}

KNOWN_LOCATIONS = {
    "pakistan": "Pakistan",
    "islamabad": "Islamabad",
    "karachi": "Karachi",
    "lahore": "Lahore",
    "peshawar": "Peshawar",
    "quetta": "Quetta",
    "rawalpindi": "Rawalpindi",
    "adiala": "Adiala Jail",
    "punjab": "Punjab",
    "sindh": "Sindh",
    "balochistan": "Balochistan",
    "kpk": "Khyber Pakhtunkhwa",
    "khyber pakhtunkhwa": "Khyber Pakhtunkhwa",
    "kashmir": "Kashmir",
    "gilgit": "Gilgit-Baltistan",
    "australia": "Australia",
    "india": "India",
    "afghanistan": "Afghanistan",
    "iran": "Iran",
    "china": "China",
    "united states": "United States",
    "uk": "United Kingdom",
    "gaza": "Gaza",
    "israel": "Israel",
    "ukraine": "Ukraine",
    "russia": "Russia",
}

KNOWN_PRODUCTS = {
    "petrol": "Petrol",
    "diesel": "Diesel",
    "petroleum": "Petroleum",
    "gasoline": "Gasoline",
    "crude oil": "Crude Oil",
    "electricity": "Electricity",
    "natural gas": "Natural Gas",
    "lng": "LNG",
    "5g": "5G Spectrum",
}

KNOWN_EVENT_TYPES = {
    "bail": "Bail / Court Ruling",
    "appeal": "Appeal / Plea",
    "plea": "Appeal / Plea",
    "petition": "Appeal / Plea",
    "verdict": "Court Verdict",
    "hearing": "Court Hearing",
    "detention": "Detention / Custody",
    "arrest": "Arrest",
    "budget": "Budget / Legislation",
    "election": "Election",
    "protest": "Protest / Demonstration",
    "dharna": "Protest / Demonstration",
    "rally": "Protest / Demonstration",
    "price": "Price Adjustment",
    "hike": "Price Adjustment",
    "tariff": "Tariff Adjustment",
    "earthquake": "Earthquake",
    "flood": "Flood / Weather Emergency",
    "explosion": "Attack / Explosion",
    "blast": "Attack / Explosion",
    "attack": "Attack / Security Incident",
    "layoff": "Corporate Layoffs",
    "layoffs": "Corporate Layoffs",
    "acquisition": "M&A / Acquisition",
}


def extract_story_entities(text: str) -> dict[str, list[str]]:
    """Extract canonical People, Organizations, Countries/Locations, Products, and Events."""
    clean = _strip_html(text)
    lower = clean.lower()

    people = set()
    for pat, canon in KNOWN_PEOPLE.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", lower):
            people.add(canon)

    orgs = set()
    for pat, canon in KNOWN_ORGS.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", lower):
            orgs.add(canon)

    countries = set()
    for pat, canon in KNOWN_LOCATIONS.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", lower):
            countries.add(canon)

    products = set()
    for pat, canon in KNOWN_PRODUCTS.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", lower):
            products.add(canon)

    events = set()
    for pat, canon in KNOWN_EVENT_TYPES.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", lower):
            events.add(canon)

    for match in re.findall(r"\b([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})\b", clean):
        m_low = match.lower()
        if (
            m_low not in STOP_WORDS
            and not any(w in STOP_WORDS for w in m_low.split())
            and match not in people
            and match not in orgs
            and match not in countries
        ):
            if len(people) < 8:
                people.add(match)

    return {
        "people": sorted(people),
        "organizations": sorted(orgs),
        "countries": sorted(countries),
        "products": sorted(products),
        "events": sorted(events),
    }


def extract_key_facts(title: str, summary: str, article_text: str, entities: dict) -> dict:
    """Extract What happened, Where, When, Why it matters, and 3 key fact bullets."""
    clean_title = _GNEWS_SUFFIX_RE.sub("", _strip_html(title or "")).strip()
    clean_body = _strip_html(article_text or summary or "").strip()
    if _is_google_news_boilerplate(clean_body):
        clean_body = _GNEWS_SUFFIX_RE.sub("", _strip_html(summary or "")).strip()

    if clean_body.lower().startswith(clean_title.lower()):
        clean_body = clean_body[len(clean_title):].lstrip(" .,-—:")

    sentences = [
        " ".join(s.split())
        for s in re.split(r"(?<=[.!?])\s+", clean_body)
        if len(s.strip()) >= 35
    ]

    bullets = []
    seen_norm = set()
    for candidate in [clean_title, *sentences]:
        if not candidate:
            continue
        norm = re.sub(r"\W+", "", candidate.lower())[:50]
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        bullets.append(candidate if candidate.endswith((".", "!", "?")) else candidate + ".")
        if len(bullets) >= 3:
            break

    while len(bullets) < 3:
        if len(bullets) == 1 and entities.get("organizations"):
            bullets.append(f"Key organizations involved: {', '.join(entities['organizations'][:4])}.")
        elif len(bullets) == 2 and entities.get("countries"):
            bullets.append(f"Primary location/jurisdiction: {', '.join(entities['countries'][:4])}.")
        else:
            break

    where_val = ", ".join(entities.get("countries") or ["Pakistan"])
    what_val = bullets[0] if bullets else clean_title
    why_val = bullets[1] if len(bullets) > 1 else "Significant national or geopolitical development."

    return {
        "what_happened": what_val,
        "where_it_happened": where_val,
        "when_it_happened": datetime.now(PKT).strftime("%Y-%m-%d"),
        "why_it_matters": why_val,
        "bullets": bullets[:3],
    }


def generate_event_fingerprint(title: str, summary: str, article_text: str, entities: dict) -> str:
    """Create a one-sentence canonical Event Fingerprint describing the underlying real-world event."""
    combined = f"{_sim_text({'title': title})} {_strip_html(summary)} {_strip_html(article_text)}"
    tokens = _tokenize(combined)
    freq = Counter(tokens)

    for group in entities.values():
        for item in group:
            for t in _tokenize(item):
                freq[t] += 3

    top_terms = [w for w, _ in freq.most_common(10)]
    actors = ", ".join((entities.get("people") or [])[:2] + (entities.get("organizations") or [])[:2])
    locs = ", ".join((entities.get("countries") or ["Pakistan"])[:2])
    evts = ", ".join((entities.get("events") or ["Development"])[:2])

    if actors:
        return f"Event[{evts}] involving {actors} in {locs}: {' '.join(top_terms[:8])}"
    return f"Event[{evts}] in {locs}: {' '.join(top_terms[:10])}"


def compute_semantic_embedding(text: str, entities: dict | None = None, dim: int = 128) -> list[float]:
    """Compute a normalized 128-dim semantic vector representation of the story."""
    vec = [0.0] * dim
    tokens = _tokenize(text)
    freq = Counter(tokens)

    if entities:
        for group in entities.values():
            for ent in group:
                for tok in _tokenize(ent):
                    freq[tok] += 2

    for tok, weight in freq.items():
        digest = hashlib.md5(tok.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if (digest[2] & 1) == 0 else -1.0
        vec[idx] += sign * (1.0 + math.log1p(weight))

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [round(v / norm, 5) for v in vec]
    return vec


def vector_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 == 0.0 or n2 == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (n1 * n2)))


def build_canonical_story(entry: dict, source: str = "") -> dict:
    """Step 2: Build the full Canonical Story record for an incoming article."""
    title = _sim_text(entry) or (entry.get("title") or "Untitled").strip()
    raw_summary = _GNEWS_SUFFIX_RE.sub("", _strip_html(entry.get("summary") or "")).strip()
    article_text = entry.get("article_text") or ""

    full_text_for_analysis = f"{title}. {raw_summary}. {article_text}".strip()
    entities = extract_story_entities(full_text_for_analysis)
    canonical_summary = _canonical_summary_100_150_words(title, raw_summary, article_text)
    key_facts = extract_key_facts(title, raw_summary, article_text, entities)
    event_fingerprint = generate_event_fingerprint(title, raw_summary, article_text, entities)
    embedding = compute_semantic_embedding(
        f"{event_fingerprint} {title} {canonical_summary}", entities
    )

    return {
        "headline": title,
        "canonical_summary": canonical_summary,
        "entities": entities,
        "key_facts": key_facts,
        "event_fingerprint": event_fingerprint,
        "embedding": embedding,
        "source": source or entry.get("source_name", "Unknown"),
    }


# ─── STEP 4, 5 & 6: Duplicate Detection, Thresholds & Story Evolution ────────

def _extract_numbers(text: str) -> set[str]:
    """Extract significant numeric figures (e.g. 9000, 250, 14.5) to detect story updates."""
    cleaned = _strip_html(text)
    matches = re.findall(r"\b\d+(?:,\d{3})*(?:\.\d+)?\b", cleaned)
    return {m.replace(",", "") for m in matches if len(m.replace(",", "")) >= 2}


UPDATE_SIGNAL_WORDS = {
    "confirms", "confirmed", "confirmation", "restructuring",
    "verdict", "sentenced", "casualties", "toll",
}


def is_significant_update(new_entry: dict, stored_record: dict) -> bool:
    """Step 6: Determine if a newer article about an already-stored event contains
    substantially new confirmed figures, outcomes, or developments.
    Never triggers on legacy cache records or identical URLs/headlines."""
    if not isinstance(stored_record, dict):
        return False

    old_headline = (stored_record.get("original_headline") or "").strip()
    old_summary = (stored_record.get("canonical_summary") or "").strip()
    # If the stored record is a legacy entry without original_headline, never re-send as update
    if not old_headline:
        return False

    new_headline = (new_entry.get("title") or "").strip()
    if title_key(new_headline) == title_key(old_headline):
        return False

    # Same URL is never a new update
    new_urls = _entry_urls(new_entry)
    old_urls = set(stored_record.get("urls", []))
    if new_urls and old_urls and (new_urls & old_urls):
        return False

    new_text = f"{new_headline} {new_entry.get('summary', '')} {new_entry.get('article_text', '')}"
    old_text = f"{old_headline} {old_summary}"

    new_headline_nums = _extract_numbers(new_headline)
    old_all_nums = _extract_numbers(old_text)
    added_headline_nums = new_headline_nums - old_all_nums

    new_words = set(re.findall(r"\b[a-z]{3,}\b", new_text.lower()))
    old_words = set(re.findall(r"\b[a-z]{3,}\b", old_text.lower()))
    novel_words = new_words - old_words

    has_explicit_update_signal = bool(novel_words & UPDATE_SIGNAL_WORDS)

    # Require both a new confirmed figure in the headline AND an explicit update/confirmation signal
    if added_headline_nums and has_explicit_update_signal and len(novel_words) >= 3:
        return True
    return False


def _semantic_match(
    left_title: set[str],
    left_story: set[str],
    right_title: set[str],
    right_story: set[str],
) -> bool:
    """Multi-tier semantic event similarity check across title and full body tokens."""
    title_shared = len(left_title & right_title)
    if title_shared >= MIN_SHARED_TOKENS and _cosine_sim(left_title, right_title) >= COSINE_THRESHOLD:
        return True

    # Asymmetric title containment: when a concise headline is a subset of a detailed headline
    min_title_len = min(len(left_title), len(right_title))
    if min_title_len >= 3 and title_shared >= 3 and (title_shared / min_title_len) >= 0.65:
        return True

    # Title-to-story containment: when a short RSS headline without scraped body matches a full article body
    if len(left_title) >= 4 and title_shared >= 2 and (len(left_title & right_story) / len(left_title)) >= 0.75:
        return True
    if len(right_title) >= 4 and title_shared >= 2 and (len(right_title & left_story) / len(right_title)) >= 0.75:
        return True

    story_shared = len(left_story & right_story)
    if title_shared >= 2 and story_shared >= 5 and _cosine_sim(left_story, right_story) >= 0.38:
        return True
    return (
        (title_shared >= 1 and story_shared >= 7 and _cosine_sim(left_story, right_story) >= 0.30)
        or (story_shared >= 9 and _cosine_sim(left_story, right_story) >= 0.50)
    )


def _record_tokens(record: dict) -> tuple[set[str], set[str]]:
    """Extract and re-tokenize stored record headlines/summaries so cached entries use latest rules."""
    right_title = set(record.get("tt", "").split(",")) - {""}
    for field in ("original_headline", "canonical_headline"):
        if record.get(field):
            right_title |= set(_tokenize(_sim_text({"title": record[field]})))

    right_story = set(record.get("st", "").split(",")) - {""}
    right_story |= right_title
    if record.get("canonical_summary"):
        right_story |= set(_tokenize(record["canonical_summary"]))
    return right_title, right_story


def compute_story_similarity(left_entry: dict, right_entry_or_record: dict) -> float:
    """Compute a normalized 0.0 - 1.0 event similarity score for Step 5 thresholds (70% / 85%)."""
    if _entry_urls(left_entry) & _entry_urls(right_entry_or_record):
        return 1.0

    left_title = set(_tokenize(_sim_text(left_entry)))
    left_story = _story_tokens(left_entry)

    if (
        "tt" in right_entry_or_record
        or "canonical_summary" in right_entry_or_record
        or "original_headline" in right_entry_or_record
    ):
        right_title, right_story = _record_tokens(right_entry_or_record)
    else:
        right_title = set(_tokenize(_sim_text(right_entry_or_record)))
        right_story = _story_tokens(right_entry_or_record)

    if _semantic_match(left_title, left_story, right_title, right_story):
        raw_cos = max(_cosine_sim(left_title, right_title), _cosine_sim(left_story, right_story))
        return max(0.86, min(1.0, raw_cos + 0.35))

    title_cos = _cosine_sim(left_title, right_title)
    story_cos = _cosine_sim(left_story, right_story)
    shared_story = len(left_story & right_story)

    if shared_story >= 4 and max(title_cos, story_cos) >= 0.32:
        return max(0.72, min(0.84, max(title_cos, story_cos) + 0.35))

    return max(title_cos, story_cos)


def _same_article_entries(left: dict, right: dict) -> bool:
    """Match stable URL identity first, then canonical event/fact/semantic similarity."""
    if _entry_urls(left) & _entry_urls(right):
        return True
    left_title = set(_tokenize(_sim_text(left)))
    left_story = _story_tokens(left)
    right_title = set(_tokenize(_sim_text(right)))
    right_story = _story_tokens(right)
    if _semantic_match(left_title, left_story, right_title, right_story):
        return True

    left_canon = build_canonical_story(left)
    right_canon = build_canonical_story(right)
    fp_left = set(_tokenize(left_canon["event_fingerprint"]))
    fp_right = set(_tokenize(right_canon["event_fingerprint"]))
    if len(fp_left & fp_right) >= 5 and _cosine_sim(fp_left, fp_right) >= 0.62:
        return True
    return False


# ─── Step 3: 72-Hour Rolling Story Memory Persistence ─────────────────────────

def _prune_seen(seen: dict, hours: int = SEEN_SCAN_HOURS) -> dict:
    """Keep records for 72 hours. Automatically remove entries older than 72 hours."""
    cutoff = time.time() - hours * 3_600
    return {
        k: v
        for k, v in seen.items()
        if (v.get("ts", 0) if isinstance(v, dict) else 0) >= cutoff
    }


_FETCH_WORKER_SRC = (
    "import sys, requests; "
    "r = requests.get(sys.argv[1], timeout=(6, 10), "
    'headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}); '
    "r.raise_for_status(); "
    "sys.stdout.buffer.write(r.content)"
)


def _fetch_feed(url: str):
    """Fetch a feed in a child process under a hard wall-clock kill."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _FETCH_WORKER_SRC, url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        raise TimeoutError(f"feed timed out after {FETCH_TIMEOUT}s: {url}")
    if proc.returncode != 0:
        err_txt = err.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"feed download failed: {(err_txt or ['?'])[-1][:200]}")
    return feedparser.parse(out)


def _sync_seen_from_ntfy(seen: dict) -> dict:
    """Backfill 72-hour rolling memory from recent ntfy topic history so no already-sent
    story can ever be resent even if the GitHub Actions cache misses."""
    if not NTFY_TOPIC or NTFY_TOPIC == "dedup-test-topic":
        return seen
    poll_url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}/json?poll=1&since={SEEN_SCAN_HOURS}h"
    try:
        resp = requests.get(poll_url, timeout=(3, 6))
        if resp.status_code != 200:
            return seen
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("event") != "message":
                continue
            msg = item.get("message") or ""
            msg_lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
            headline = (item.get("title") or "").strip()
            if not headline or headline == "Pakistan & World News":
                headline = msg_lines[0] if msg_lines else ""
            if not headline:
                continue

            summary_val = ""
            link_val = (item.get("click") or "").strip()
            for ln in msg_lines:
                if ln.startswith("Summary:"):
                    summary_val = ln[len("Summary:"):].strip()
                elif ln.startswith("Link:") and not link_val:
                    link_val = ln[len("Link:"):].strip()

            tk = title_key(headline)
            msg_ts = int(item.get("time") or time.time())
            synthetic = {"title": headline, "summary": summary_val, "link": link_val}
            if tk not in seen:
                rec = build_memory_record(synthetic, "ntfy-history", tk)
                rec["ts"] = msg_ts
                seen[tk] = rec
            else:
                existing = seen[tk]
                if isinstance(existing, dict):
                    rt, rs = _record_tokens(existing)
                    rt |= set(_tokenize(_sim_text(synthetic)))
                    rs |= _story_tokens(synthetic)
                    existing["tt"] = ",".join(sorted(rt))
                    existing["st"] = ",".join(sorted(rs)[:140])
                    if link_val:
                        canon_u = _canonical_url(link_val)
                        if canon_u:
                            urls = set(existing.get("urls", []))
                            urls.add(canon_u)
                            existing["urls"] = sorted(urls)
    except Exception as exc:
        print(f"  ⚠  ntfy history sync skipped: {exc}", file=sys.stderr)
    return seen


def load_seen() -> dict:
    seen = {}
    if STATE_FILE.exists():
        try:
            seen = _prune_seen(json.loads(STATE_FILE.read_text()))
        except Exception:
            seen = {}
    seen = _sync_seen_from_ntfy(seen)
    return _prune_seen(seen)


def save_seen(seen: dict) -> None:
    seen = _prune_seen(seen)
    if len(seen) > 10_000:
        def _ts(v):
            return v.get("ts", 0) if isinstance(v, dict) else int(v)
        keys = sorted(seen, key=_ts, reverse=True)[:5_000]
        seen = {k: seen[k] for k in keys}
    STATE_FILE.write_text(json.dumps(seen, indent=2))


def title_key(title: str) -> str:
    t = " ".join(_tokenize(_GNEWS_SUFFIX_RE.sub("", title or "")))
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def extract_keywords(text: str, n: int = FINGERPRINT_SIZE) -> list[str]:
    """Top-N content words by frequency — used as the cross-run fingerprint."""
    freq = Counter(_tokenize(text))
    return [w for w, _ in freq.most_common(n)]


def fingerprint_matches(text: str, seen: dict) -> bool:
    """True if article keywords overlap >= threshold with any seen article."""
    kw = set(extract_keywords(text))
    if not kw:
        return False
    for val in seen.values():
        if isinstance(val, dict) and val.get("kw"):
            seen_kw = set(val["kw"].split(","))
            if seen_kw and len(kw & seen_kw) / len(kw | seen_kw) >= FINGERPRINT_JACCARD:
                return True
    return False


def seen_semantic_match(
    tokens: set, seen: dict, cos_threshold: float, min_shared: int
) -> bool:
    """True if the article's title tokens are semantically similar to any seen article."""
    if not tokens:
        return False
    for val in seen.values():
        if not isinstance(val, dict):
            continue
        seen_tokens, _ = _record_tokens(val)
        if not seen_tokens:
            continue
        if len(tokens & seen_tokens) < min_shared:
            continue
        if _cosine_sim(tokens, seen_tokens) >= cos_threshold:
            return True
    return False


def seen_url_match(entry: dict, seen: dict) -> bool:
    """Check normalized URLs against the persistent sent-article cache."""
    urls = _entry_urls(entry)
    if not urls:
        return False
    for val in seen.values():
        if not isinstance(val, dict):
            continue
        stored = set(val.get("urls", []))
        if isinstance(val.get("url"), str) and val["url"]:
            stored.add(val["url"])
        if urls & stored:
            return True
    return False


def seen_story_match(entry: dict, seen: dict) -> bool:
    """Compare title, summary, Event Fingerprint, and semantic embedding against 72h memory."""
    title_tokens = set(_tokenize(_sim_text(entry)))
    story_tokens = _story_tokens(entry)
    candidate_canon = None

    for val in seen.values():
        if not isinstance(val, dict):
            continue
        seen_title, seen_story = _record_tokens(val)
        if not seen_story:
            seen_story = seen_title
        if _semantic_match(title_tokens, story_tokens, seen_title, seen_story):
            return True

        if val.get("event_fingerprint") or val.get("embedding"):
            if candidate_canon is None:
                candidate_canon = build_canonical_story(entry)
            if val.get("event_fingerprint"):
                fp_cand = set(_tokenize(candidate_canon["event_fingerprint"]))
                fp_seen = set(_tokenize(val["event_fingerprint"]))
                if len(fp_cand & fp_seen) >= 5 and _cosine_sim(fp_cand, fp_seen) >= 0.62:
                    return True
            if val.get("embedding") and candidate_canon.get("embedding"):
                if vector_cosine_similarity(candidate_canon["embedding"], val["embedding"]) >= DUPLICATE_HIGH_THRESHOLD:
                    return True
    return False


def evaluate_entry_against_memory(entry: dict, seen: dict, source: str = "") -> tuple[str, str | None, float]:
    """Step 4, 5 & 6: Compare incoming entry against all stories in 72h memory.
    Returns (classification, matched_key, similarity_score) where classification is:
    'NEW STORY', 'SIGNIFICANT UPDATE', or 'DUPLICATE'.
    """
    tk = title_key(entry.get("title", ""))
    if tk in seen or seen_url_match(entry, seen):
        return "DUPLICATE", tk if tk in seen else None, 1.0

    best_key = None
    best_record = None
    best_sim = 0.0

    for key, val in seen.items():
        if not isinstance(val, dict):
            continue
        sim = compute_story_similarity(entry, val)
        if sim > best_sim:
            best_sim = sim
            best_key = key
            best_record = val

    if best_record is None:
        return "NEW STORY", None, 0.0

    if best_sim >= DUPLICATE_HIGH_THRESHOLD or seen_story_match(entry, {best_key: best_record}):
        if is_significant_update(entry, best_record):
            return "SIGNIFICANT UPDATE", best_key, best_sim
        return "DUPLICATE", best_key, max(best_sim, 0.85)

    if best_sim >= DUPLICATE_LOW_THRESHOLD:
        cand_canon = build_canonical_story(entry, source)
        fp_cand = set(_tokenize(cand_canon["event_fingerprint"]))
        fp_seen = set(_tokenize(best_record.get("event_fingerprint", "")))
        if (fp_seen and _cosine_sim(fp_cand, fp_seen) >= 0.50) or fingerprint_matches(entry_text(entry), {best_key: best_record}):
            if is_significant_update(entry, best_record):
                return "SIGNIFICANT UPDATE", best_key, best_sim
            return "DUPLICATE", best_key, best_sim

    return "NEW STORY", None, best_sim


def build_memory_record(
    entry: dict,
    source: str,
    story_id: str,
    story_type: str = "NEW STORY",
    canonical: dict | None = None,
) -> dict:
    """Step 3: Create a 72-hour rolling Story Memory record."""
    if canonical is None:
        canonical = build_canonical_story(entry, source)
    urls = sorted(_entry_urls(entry))
    title_tokens = sorted(set(_tokenize(_sim_text(entry))))
    story_tokens = sorted(_story_tokens(entry))[:140]

    return {
        "story_id": story_id,
        "original_headline": (entry.get("title") or "").strip(),
        "canonical_headline": canonical["headline"],
        "canonical_summary": canonical["canonical_summary"],
        "entities": canonical["entities"],
        "key_facts": canonical["key_facts"],
        "event_fingerprint": canonical["event_fingerprint"],
        "embedding": canonical["embedding"],
        "story_type": story_type,
        "source": source,
        "ts": int(time.time()),
        "kw": ",".join(extract_keywords(entry_text(entry))),
        "tt": ",".join(title_tokens),
        "st": ",".join(story_tokens),
        "urls": urls,
        "url": urls[0] if urls else (entry.get("link") or ""),
    }


def entry_text(entry: dict) -> str:
    """Title + summary (Google News suffix stripped) — for fingerprints."""
    title = entry.get("title") or ""
    summary = _GNEWS_SUFFIX_RE.sub("", _strip_html(entry.get("summary") or "")).strip()
    return f"{title} {summary}"


def is_recent(entry: dict, now: datetime) -> bool:
    published = None
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                published = datetime(*tp[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    if published is None:
        return True
    return (now - published) <= timedelta(minutes=WINDOW_MINUTES)


def is_important(entry: dict, scope: str) -> bool:
    text = (entry.get("title") or "") + " " + (entry.get("summary") or "")
    if IMRAN_KHAN_RE.search(text):
        if IMRAN_INDIAN_ACTOR_RE.search(text):
            return False
        if not IMRAN_PAKISTAN_IDENTITY_RE.search(text):
            return False
    if scope == "national":
        return True
    return bool(KEYWORD_RE.search(text))


# ─── STEP 7: User Delivery Format ─────────────────────────────────────────────

def format_message(
    entry: dict,
    source: str,
    scope: str,
    story_type: str = "NEW STORY",
    canonical: dict | None = None,
) -> str:
    """Step 7: Format notification strictly as NEW STORY or SIGNIFICANT UPDATE with
    Headline, 100-150w Summary, Source, Published timestamp, Story Type, and direct
    clickable Link (without Key Facts)."""
    if canonical is None:
        canonical = build_canonical_story(entry, source)

    headline = canonical.get("headline") or (entry.get("title") or "No title").strip()
    summary = entry.get("article_summary") or canonical.get("canonical_summary") or headline
    link = (entry.get("link") or "").strip()

    ts = datetime.now(PKT).strftime("%b %d, %Y %I:%M %p PKT")
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                dt = datetime(*tp[:6], tzinfo=timezone.utc)
                ts = dt.astimezone(PKT).strftime("%b %d, %Y %I:%M %p PKT")
            except Exception:
                pass
            break

    lines = [
        headline,
        "",
        f"Summary: {summary}",
        "",
        f"Source: {source}",
        "",
        f"Published: {ts}",
        "",
        f"Story Type: {story_type}",
        "",
        f"Link: {link}",
    ]
    return "\n".join(lines)


# ─── Collection + selection ───────────────────────────────────────────────────

def _collect_feed(feed_cfg: dict, seen: dict, now, run_started: float) -> list[dict]:
    """Fetch one feed, require a valid article URL, extract full article content,
    run 72h canonical memory check, and return candidate items."""
    name, url, scope = feed_cfg["name"], feed_cfg["url"], feed_cfg["scope"]
    print(f"  ▸ Fetching {name} …", flush=True)
    try:
        try:
            parsed = _fetch_feed(url)
        except Exception:
            fallback = feed_cfg.get("fallback_url")
            if not fallback:
                raise
            print(f"    ⚠  {name} primary feed failed — trying fallback URL.", file=sys.stderr)
            parsed = _fetch_feed(fallback)
    except Exception as exc:
        print(f"    ⚠  Failed to fetch {name}: {exc}", file=sys.stderr)
        return []

    raw: list[dict] = []
    enriched = 0
    for entry in parsed.entries[:40]:
        if time.time() - run_started > RUN_DEADLINE:
            print("  ⚠  Run deadline reached — stopping entry scan.", file=sys.stderr)
            break

        # Strictly require a valid http/https link so stories without links are never sent
        link = (entry.get("link") or "").strip()
        if not link or not link.startswith(("http://", "https://")):
            continue

        if not is_recent(entry, now):
            continue
        if not is_important(entry, scope):
            continue

        tk = title_key(entry.get("title", ""))
        if tk in seen:
            continue
        if seen_url_match(entry, seen):
            print(f"    ↯ DUPLICATE (URL match): {name} | {link}")
            continue

        candidate = dict(entry)
        # Step 1: Fetch Full Content for candidate articles
        if enriched < MAX_ARTICLE_ENRICH:
            page_text, page_summary = _fetch_article_page(link)
            candidate["article_text"] = page_text
            candidate["article_summary"] = page_summary
            enriched += 1

        # Step 2, 4, 5 & 6: Evaluate against 72-hour Story Memory
        classification, matched_key, sim_score = evaluate_entry_against_memory(
            candidate, seen, name
        )

        if classification == "DUPLICATE":
            print(
                f"    ↯ DUPLICATE ({int(sim_score * 100)}% sim): "
                f"source={name} url={link}"
            )
            continue

        final_id = matched_key if (classification == "SIGNIFICANT UPDATE" and matched_key) else tk
        canonical = build_canonical_story(candidate, name)
        title_tokens = set(_tokenize(_sim_text(candidate)))

        raw.append({
            "source": name,
            "scope": scope,
            "entry": candidate,
            "id": final_id,
            "tokens": title_tokens,
            "story_type": classification,
            "canonical": canonical,
        })

    dedup: set[str] = set()
    unique = [a for a in raw if not (a["id"] in dedup or dedup.add(a["id"]))]
    return _semantic_dedup(unique, COSINE_THRESHOLD, MIN_SHARED_TOKENS)


def _semantic_dedup(
    articles: list[dict], cos_threshold: float, min_shared: int
) -> list[dict]:
    """Drop near-duplicate stories within the same batch."""
    if len(articles) <= 1:
        return articles

    dropped: set[int] = set()

    for i in range(len(articles)):
        if i in dropped:
            continue
        for j in range(i + 1, len(articles)):
            if j in dropped:
                continue
            if not _same_article_entries(articles[i]["entry"], articles[j]["entry"]):
                continue
            pri_i = SOURCE_PRIORITY.get(articles[i]["source"], 99)
            pri_j = SOURCE_PRIORITY.get(articles[j]["source"], 99)
            if pri_i <= pri_j:
                dropped.add(j)
            else:
                dropped.add(i)
                break

    kept = [a for idx, a in enumerate(articles) if idx not in dropped]
    removed = len(articles) - len(kept)
    if removed:
        print(f"  ↯ semantic dedup: removed {removed} near-duplicate(s)")
    return kept


# ─── Main ─────────────────────────────────────────────────────────────────────

def send_to_ntfy(message: str, title: str = "Pakistan & World News", link: str = "") -> bool:
    if not link or not link.startswith(("http://", "https://")):
        return False
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    safe_title = title.encode("ascii", "ignore").decode("ascii").strip() or "Pakistan & World News"
    headers = {
        "Title": safe_title[:120],
        "Click": link,
        "Tags": "newspaper,news,pakistan",
        "Priority": "default",
    }
    try:
        r = requests.post(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        return r.status_code in (200, 201)
    except Exception as exc:
        print(f"  ⚠  ntfy POST failed: {exc}", file=sys.stderr)
        return False


def main() -> None:
    now_pk = datetime.now(PKT).strftime("%b %d, %I:%M %p PKT")
    print(
        f"═══ News fetcher  |  {now_pk}  |  topic={NTFY_TOPIC}  "
        f"|  retention={SEEN_SCAN_HOURS}h  |  cap={MAX_PER_RUN} ═══\n"
    )

    now = datetime.now(timezone.utc)
    seen = load_seen()
    sent: list[dict] = []
    source_used: Counter = Counter()
    run_started = time.time()

    for feed_cfg in RSS_FEEDS:
        if len(sent) >= MAX_PER_RUN:
            print("  ⚠  Per-run cap reached — stopping.", file=sys.stderr)
            break
        if time.time() - run_started > RUN_DEADLINE:
            print("  ⚠  Run deadline reached — stopping feed fetches.", file=sys.stderr)
            break
        cands = _collect_feed(feed_cfg, seen, now, run_started)
        if not cands:
            continue

        kept = []
        for c in cands:
            dup = False
            for prior in sent:
                if _same_article_entries(c["entry"], prior["entry"]):
                    dup = True
                    break
            if not dup:
                kept.append(c)
        if len(kept) < len(cands):
            print(f"  ↯ semantic dedup vs earlier sent: dropped {len(cands) - len(kept)}")

        for art in kept:
            if len(sent) >= MAX_PER_RUN:
                break
            if source_used[art["source"]] >= 5:
                continue
            link = (art["entry"].get("link") or "").strip()
            if not link or not link.startswith(("http://", "https://")):
                continue
            story_type = art.get("story_type", "NEW STORY")
            canonical = art.get("canonical") or build_canonical_story(art["entry"], art["source"])
            body = format_message(
                art["entry"],
                art["source"],
                art["scope"],
                story_type=story_type,
                canonical=canonical,
            )
            ok = send_to_ntfy(
                body,
                title=canonical.get("headline", "Pakistan & World News"),
                link=link,
            )
            if ok:
                sent.append(art)
                source_used[art["source"]] += 1
                seen[art["id"]] = build_memory_record(
                    art["entry"],
                    art["source"],
                    art["id"],
                    story_type=story_type,
                    canonical=canonical,
                )
                save_seen(seen)
            else:
                print(
                    f"  ⚠  Failed to send: {art['entry'].get('title', '?')[:60]}",
                    file=sys.stderr,
                )

    if not sent:
        print("Nothing new — done.")
        return
    print(f"\n  ✔ Sent {len(sent)} article(s) to ntfy topic: {NTFY_TOPIC}")
    print(f"  Queue: {dict(source_used)}")


if __name__ == "__main__":
    main()
