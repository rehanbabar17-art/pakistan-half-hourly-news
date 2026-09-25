# 🇵🇰 Half-Hourly Pakistan News via ntfy

Automatically collects **important national (Pakistan) and international news**
every **30 minutes** from Pakistani English-language news websites, then pushes
them straight to your phone/desktop via [ntfy.sh](https://ntfy.sh) — no app
store accounts, no email digests, just instant notifications.

## 🔒 Privacy note

Your ntfy topic lives in a **GitHub Actions secret** (`NTFY_TOPIC`), never in
this repo. To create your own topic, pick any random name (e.g.
`NewsPakistan_` + 10 random chars) — ntfy topic names are public to anyone who
guesses them, so the longer and more random, the better.

## 📬 Subscribe

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.ntfy.app) / [iOS](https://apps.apple.com/app/ntfy/id1625396347)) or use the web dashboard at <https://ntfy.sh>.
2. Subscribe on your phone using the topic you created (the same one you set as
   the `NTFY_TOPIC` secret).

Or via CLI:

```bash
curl -s ntfy.sh/<YOUR_TOPIC_NAME>
```

## ⚙️ How it works

- A **GitHub Actions** workflow runs every 30 minutes (`0,30 * * * *`).
- `scripts/fetch_news.py` fetches **RSS feeds** from Pakistani English outlets plus
  Google News RSS (fresher, cross-source coverage):

| Source | Feed | Scope |
|---|---|---|
| Dawn | `dawn.com/feeds/home` | 🇳🇵 National |
| Business Recorder | Google News `site:brecorder.com` | 🇳🇵 National |
| Google News | `Pakistan news today` query | 🇳🇵 National |
| Google News | `Pakistan world news` query | 🌍 International |
| Google News | `Imran Khan` query (US/UK/IN locale) | 🌍 International |

- All timestamps are in **Pakistan Standard Time (PKT / UTC+5)**.
- **National** articles always pass through; **international** articles must
  match priority keywords (word-boundary regex, no false positives).

### Tracked keywords

| Category | Keywords |
|---|---|
| Telecoms | Telenor, ONIC, Ufone, e&, PTCL, Etisalat, Jazz, Zong |
| Judiciary | Supreme Court, Lahore High Court, Islamabad High Court, Federal Constitutional Court |
| Politics | Imran Khan, PTI, Pakistan Tehreek-e-Insaf, Shehbaz, Zardari, Bhutto, Nawaz |
| Regional & Fuel | Pakistan, Kashmir, Balochistan, CPEC, Parliament, Senate |
| Fuel Prices | Petrol, Diesel, Fuel Price, Petrol Price, Diesel Price, Petroleum, Petrol Hike, Fuel Relief, Gasoline |

### Deduplication

- **Canonical URL dedup**: links are normalized (host casing, `www`, fragments,
  trailing slashes, and common tracking parameters removed) and checked within
  a feed, across feeds, and against the saved sent-article cache.
- **Paraphrase dedup**: normalized content words are compared across headlines;
  when titles alone are not conclusive, title and RSS-summary fingerprints are
  compared together. This catches reworded reports from different links while
  requiring both title and broader-story evidence to avoid suppressing unrelated
  stories that merely mention the same person or country.
- **Cross-run dedup**: each sent article saves canonical links and title/story
  fingerprints in the seen-article cache. New-format cache entries are checked
  by URL, title similarity, and title-plus-summary similarity; older cache
  entries remain readable. The existing example — "Seven former Australia
  captains appeal for Imran Khan" vs "Allan Border, Steve Waugh among
  ex-Australia captains to make humanitarian plea for Imran Khan" — is covered.
- **Serialized runs**: GitHub Actions allows only one fetch-and-notify run at a
  time, preventing overlapping jobs from both sending the same uncached story.
- **Source priority**: when duplicate articles appear in one feed, the more
  authoritative source wins (Dawn > Business Recorder > GNews).
- **Per-source cap**: max 5 articles per source per run so one outlet can't flood.
- **Round-robin interleave** across sources prevents one outlet from dominating
  the 12-article-per-run cap.
- A **seen-article cache** (persisted via GitHub Actions cache) carries dedup state
  across cron runs.

### Reliability

- Every feed is fetched in an isolated child process with a **12s hard kill** — a
  stalled or throttled source can never hang the run.
- If the Google News **Imran Khan** feed stalls (intermittent on GitHub runners),
  a **fallback URL** is tried automatically.
- Each feed's articles are **sent to ntfy as soon as they're collected**, so a
  later feed failing/killed never loses news the user has already been sent.
- An outer **210s watchdog** (`timeout`) kills the whole worker if it ever
  freezes, so every cron run completes — no infinite/cancelled runs.

## ▶️ Manual run

From the **Actions** tab, click **Run workflow** to trigger a news push immediately.

## 🔑 Setup

1. Fork/clone this repo.
2. Create a random ntfy topic (e.g. `NewsPakistan_XXXXXXXXXX`) and subscribe to
   it in the ntfy app.
3. In GitHub, go to **Settings → Secrets and variables → Actions** and add:

   | Secret | Value |
   |---|---|
   | `NTFY_TOPIC` | your random topic name |
   | `NTFY_SERVER` | `https://ntfy.sh` (or your self-hosted server) |

4. The workflow runs automatically every 30 minutes.

## 🛠️ Customize

- **Keywords** — edit `HIGH_PRIORITY_KEYWORDS` in `scripts/fetch_news.py`.
- **Frequency** — edit the `cron` line in `.github/workflows/half-hourly-news.yml`.
- **Sources** — add/remove entries in the `RSS_FEEDS` list.
- **Cap** — change `MAX_PER_RUN` (default: 12) to increase/decrease notifications.

## 🚀 Quick start (local)

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
pip install -r requirements.txt
NTFY_TOPIC=your_random_topic_name python scripts/fetch_news.py
```
