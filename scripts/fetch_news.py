#!/usr/bin/env python3
"""
Fetch half-hourly news from Pakistani English-language RSS feeds,
filter for important national & international stories, and deliver
them to a self-hosted or public ntfy.sh topic.

Usage:
    NTFY_TOPIC=NewsPakistan_xxxxx  python3 scripts/fetch_news.py

Environment variables:
    NTFY_TOPIC   - ntfy topic to post to  (required)
    NTFY_SERVER  - ntfy server URL         (default: https://ntfy.sh)
"""

import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import subprocess
import sys
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import feedparser
import requests

# ─── Configuration ────────────────────────────────────────────────────────────

NTFY_TOPIC  = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
STATE_FILE  = Path(__file__).resolve().parent.parent / "seen_articles.json"

if not NTFY_TOPIC:
    print("ERROR: NTFY_TOPIC environment variable is not set.", file=sys.stderr)
    print("Set it before running:", file=sys.stderr)
    print("  NTFY_TOPIC=your_topic_name python3 scripts/fetch_news.py", file=sys.stderr)
    sys.exit(1)

PKT = timezone(timedelta(hours=5))

WINDOW_MINUTES = 6 * 60   # 360 minutes (6 hours)
MAX_PER_RUN    = 12       # hard cap per cron run
FETCH_TIMEOUT  = 12           # seconds per feed — skip slow/hanging feeds
RUN_DEADLINE   = 170         # hard cap for the whole script (s)
SEEN_SCAN_HOURS = 12         # only scan dedup cache entries from last 12h

# Semantic dedup settings (tuned on live feed data)
COSINE_THRESHOLD    = 0.40     # same story, different wording (title based)
MIN_SHARED_TOKENS   = 3        # guard against short generic-title matches
FINGERPRINT_JACCARD = 0.60     # same story across runs (keyword overlap)
FINGERPRINT_SIZE    = 5        # keywords stored per seen article

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
    {"name": "Dawn",               "url": "https://www.dawn.com/feeds/home",     "scope": "national"},
    {"name": "Business Recorder",
     "url": "https://news.google.com/rss/search?q=site:brecorder.com+Pakistan&hl=en-PK&gl=PK&ceid=PK:en",
     "scope": "national"},
    {"name": "GNews Pakistan",
     "url": "https://news.google.com/rss/search?q=Pakistan+news+today&hl=en-PK&gl=PK&ceid=PK:en",
     "scope": "national"},
    {"name": "GNews Pakistan Intl",
     "url": "https://news.google.com/rss/search?q=Pakistan+world+news&hl=en-PK&gl=PK&ceid=PK:en",
     "scope": "international"},
    # Imran Khan from international outlets (US/UK/IN locales)
    {"name": "GNews Imran Intl",
     "url": "https://news.google.com/rss/search?q=Imran+Khan&hl=en-US&gl=US&ceid=US:en",
     "fallback_url": "https://news.google.com/rss/search?q=%22Imran+Khan%22+Pakistan&hl=en-GB&gl=GB&ceid=GB:en",
     "scope": "international"},
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

# ─── Tokenisation + similarity (hand-rolled, no dependencies) ─────────────────

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
    # html / feed boilerplate
    "href", "https", "http", "www", "com", "net", "org", "html", "nbsp",
    "font", "color", "div", "span", "rss", "articles", "oc", "target",
    "title", "article", "content", "category", "feed", "google", "news",
    # generic news filler
    "told", "reuters", "us", "pakistani", "pakistan", "cent", "pc",
    "week", "today", "year", "month", "day", "getty", "afp", "ap",
    "file", "photo", "image", "latest", "live", "update", "read",
}


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"\b[a-z]{2,}\b", text.lower())
    tokens = []
    for word in words:
        if len(word) > 5 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 5 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if word not in STOP_WORDS:
            tokens.append(word)
    return tokens


def _sim_text(entry: dict) -> str:
    """Title only, URL + Google News source suffix stripped — used for
    semantic comparison between different sources."""
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
            if (key_lower.startswith("utm_") or key_lower in {
                    "fbclid", "gclid", "dclid", "mc_cid", "mc_eid",
                    "oc", "ceid", "hl", "gl"}):
                continue
            query.append((key, value))
        path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
        return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))
    except Exception:
        return ""


def _entry_urls(entry: dict) -> set[str]:
    values = [entry.get("link", "")]
    values.extend(item.get("href", "") for item in entry.get("links", [])
                  if isinstance(item, dict))
    return {normalized for value in values
            if (normalized := _canonical_url(value))}


def _story_tokens(entry: dict) -> set[str]:
    """Content-bearing title and RSS summary terms for paraphrase checks."""
    title = _sim_text(entry)
    summary = _GNEWS_SUFFIX_RE.sub("", entry.get("summary") or "").strip()
    return set(_tokenize(f"{title} {summary}"))


def _semantic_match(left_title: set[str], left_story: set[str],
                    right_title: set[str], right_story: set[str]) -> bool:
    """Conservative title match, with a stricter title+summary fallback."""
    title_shared = len(left_title & right_title)
    if (title_shared >= MIN_SHARED_TOKENS
            and _cosine_sim(left_title, right_title) >= COSINE_THRESHOLD):
        return True
    story_shared = len(left_story & right_story)
    return (title_shared >= 2 and story_shared >= 5
            and _cosine_sim(left_story, right_story) >= 0.42)


def _same_article_entries(left: dict, right: dict) -> bool:
    """Match stable URL identity first, then title/summary semantics."""
    if _entry_urls(left) & _entry_urls(right):
        return True
    return _semantic_match(
        set(_tokenize(_sim_text(left))), _story_tokens(left),
        set(_tokenize(_sim_text(right))), _story_tokens(right))


# ─── Helpers ──────────────────────────────────────────────────────────────────

_GNEWS_SUFFIX_RE = re.compile(r"\s+-\s*\S.*$")


def _prune_seen(seen: dict, hours: int = SEEN_SCAN_HOURS) -> dict:
    """Keep only recent entries — candidates are at most 6h old, so a 12h
    scan window catches all possible duplicates while keeping scans fast."""
    cutoff = time.time() - hours * 3_600
    return {k: v for k, v in seen.items()
            if (v.get("ts", 0) if isinstance(v, dict) else 0) >= cutoff}


_FETCH_WORKER_SRC = (
    "import sys, requests; "
    "r = requests.get(sys.argv[1], timeout=(6, 10), "
    'headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}); '
    "r.raise_for_status(); "
    "sys.stdout.buffer.write(r.content)"
)


def _fetch_feed(url: str):
    """Fetch a feed in a child process under a hard wall-clock kill.

    The child runs in its own session so the runner can clean it up, and
    the kill+wait is grace-bounded: a child stuck in kernel D-state
    (uninterruptible sleep) can't be reaped, so we abandon it rather
    than block the run forever."""
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
            # Child stuck in uninterruptible sleep; SIGKILL is deferred.
            # It will be reaped by the runner when the job ends.
            pass
        raise TimeoutError(f"feed timed out after {FETCH_TIMEOUT}s: {url}")
    if proc.returncode != 0:
        err_txt = err.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"feed download failed: {(err_txt or ['?'])[-1][:200]}")
    return feedparser.parse(out)

    return feedparser.parse(proc.stdout)


def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return _prune_seen(json.loads(STATE_FILE.read_text()))
        except Exception:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    seen = _prune_seen(seen)
    if len(seen) > 10_000:
        # Keep the newest half (supports both ts-int and {ts,kw} formats)
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
    """True if article keywords overlap ≥ threshold with any seen article."""
    kw = set(extract_keywords(text))
    if not kw:
        return False
    for val in seen.values():
        if isinstance(val, dict) and val.get("kw"):
            seen_kw = set(val["kw"].split(","))
            if seen_kw and len(kw & seen_kw) / len(kw | seen_kw) >= FINGERPRINT_JACCARD:
                return True
    return False


def seen_semantic_match(tokens: set, seen: dict,
                            cos_threshold: float,
                            min_shared: int) -> bool:
    """True if the article's title tokens are semantically similar (cosine +
    shared-token rule) to any article already sent in previous runs."""
    if not tokens:
        return False
    for val in seen.values():
        if not isinstance(val, dict):
            continue
        tt = val.get("tt")
        if not tt:
            continue
        seen_tokens = set(tt.split(","))
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
    """Compare title and summary fingerprints against prior notifications."""
    title_tokens = set(_tokenize(_sim_text(entry)))
    story_tokens = _story_tokens(entry)
    for val in seen.values():
        if not isinstance(val, dict):
            continue
        seen_title = set(val.get("tt", "").split(",")) - {""}
        seen_story = set(val.get("st", "").split(",")) - {""}
        if not seen_story:
            seen_story = seen_title
        if _semantic_match(title_tokens, story_tokens, seen_title, seen_story):
            return True
    return False


def entry_text(entry: dict) -> str:
    """Title + summary (Google News suffix stripped) — for fingerprints."""
    title   = entry.get("title") or ""
    summary = _GNEWS_SUFFIX_RE.sub("", entry.get("summary") or "").strip()
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
    if scope == "national":
        return True
    text = (entry.get("title") or "") + " " + (entry.get("summary") or "")
    return bool(KEYWORD_RE.search(text))


def format_message(entry: dict, source: str, scope: str) -> str:
    title = (entry.get("title") or "No title").strip()
    link  = entry.get("link", "")
    tag   = "🇳🇵" if scope == "national" else "🌍"
    ts = ""
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                dt = datetime(*tp[:6], tzinfo=timezone.utc)
                ts = dt.astimezone(PKT).strftime("%b %d, %I:%M %p PKT")
            except Exception:
                pass
            break
    lines = [
        f"{tag} [{source}] {title}",
        "",
        f"📰 {scope.upper()} — {ts}" if ts else f"📰 {scope.upper()}",
        f"🔗 {link}",
    ]
    return "\n".join(lines)


# ─── Collection + selection ───────────────────────────────────────────────────

def _collect_feed(feed_cfg: dict, seen: dict, now, run_started: float) -> list[dict]:
    """Fetch one feed, check it against already-sent articles, and return
    its candidate stories.  Called per feed so each feed's deliveries are
    independent — a later feed freezing can't lose earlier feeds' news."""
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
    for entry in parsed.entries[:40]:
        if time.time() - run_started > RUN_DEADLINE:
            print("  ⚠  Run deadline reached — stopping entry scan.", file=sys.stderr)
            break
        tk = title_key(entry.get("title", ""))
        if tk in seen:
            continue
        if seen_url_match(entry, seen):
            print(f"    ↯ URL match (already sent): {entry.get('title','')[:50]}")
            continue
        title_tokens = set(_tokenize(_sim_text(entry)))
        if seen_story_match(entry, seen):
            print(f"    ↯ story match (already sent): {entry.get('title','')[:50]}")
            continue
        if fingerprint_matches(entry_text(entry), seen):
            print(f"    ↯ fingerprint match (already sent): {entry.get('title','')[:50]}")
            continue
        if seen_semantic_match(title_tokens, seen, COSINE_THRESHOLD, MIN_SHARED_TOKENS):
            print(f"    ↯ semantic match (already sent): {entry.get('title','')[:50]}")
            continue
        if not is_recent(entry, now):
            continue
        if not is_important(entry, scope):
            continue
        raw.append({
            "source":  name,
            "scope":   scope,
            "entry":   entry,
            "id":      tk,
            "tokens":  title_tokens,
        })

    # Exact-title dedup within this feed (identical aggregator headlines)
    dedup: set[str] = set()
    unique = [a for a in raw if not (a["id"] in dedup or dedup.add(a["id"]))]
    return _semantic_dedup(unique, COSINE_THRESHOLD, MIN_SHARED_TOKENS)




def _semantic_dedup(articles: list[dict],
                    cos_threshold: float,
                    min_shared: int) -> list[dict]:
    """Drop near-duplicate stories reported with different wording.

    Uses binary token cosine similarity on cleaned titles: two articles
    are considered the same story when they share >= min_shared content
    tokens AND cosine >= threshold.  The more authoritative source wins.
    """
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
                break  # i was removed — stop scanning its remaining pairs

    kept = [a for idx, a in enumerate(articles) if idx not in dropped]
    removed = len(articles) - len(kept)
    if removed:
        print(f"  ↯ semantic dedup: removed {removed} near-duplicate(s)")
    return kept


# ─── Main ─────────────────────────────────────────────────────────────────────

def send_to_ntfy(message: str) -> bool:
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    try:
        r = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Title":    "Pakistan & World News",
                "Tags":     "newspaper,news,pakistan",
                "Priority": "default",
            },
            timeout=15,
        )
        return r.status_code in (200, 201)
    except Exception as exc:
        print(f"  ⚠  ntfy POST failed: {exc}", file=sys.stderr)
        return False


def main() -> None:
    now_pk = datetime.now(PKT).strftime("%b %d, %I:%M %p PKT")
    print(f"═══ News fetcher  |  {now_pk}  |  topic={NTFY_TOPIC}  |  window={WINDOW_MINUTES}min  |  cap={MAX_PER_RUN} ═══\n")

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

        # Drop duplicate links and paraphrased stories already sent earlier
        # in this run (feed ordering gives earlier sources priority).
        kept = []
        for c in cands:
            dup = any(_same_article_entries(c["entry"], prior["entry"])
                      for prior in sent)
            if not dup:
                kept.append(c)
        if len(kept) < len(cands):
            print(f"  ↯ semantic dedup vs earlier sent: dropped {len(cands) - len(kept)}")

        for art in kept:
            if len(sent) >= MAX_PER_RUN:
                break
            if source_used[art["source"]] >= 5:
                continue
            body = format_message(art["entry"], art["source"], art["scope"])
            ok = send_to_ntfy(body)
            if ok:
                sent.append(art)
                source_used[art["source"]] += 1
                urls = sorted(_entry_urls(art["entry"]))
                title_tokens = sorted(set(_tokenize(_sim_text(art["entry"]))))
                story_tokens = sorted(_story_tokens(art["entry"]))
                seen[art["id"]] = {
                    "ts": int(time.time()),
                    "kw": ",".join(extract_keywords(entry_text(art["entry"]))),
                    "tt": ",".join(title_tokens),
                    "st": ",".join(story_tokens),
                    "urls": urls,
                    "url": urls[0] if urls else "",
                }
                # Persist after every send so a run killed mid-way (e.g. by the
                # outer watchdog) never re-sends what was already delivered.
                save_seen(seen)
            else:
                print(f"  ⚠  Failed to send: {art['entry'].get('title','?')[:60]}", file=sys.stderr)

    if not sent:
        print("Nothing new — done.")
        return
    print(f"\n  ✔ Sent {len(sent)} article(s) to ntfy topic: {NTFY_TOPIC}")
    print(f"  Queue: {dict(source_used)}")


if __name__ == "__main__":
    main()
