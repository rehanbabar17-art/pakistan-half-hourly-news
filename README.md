# 🇵🇰 Half-Hourly Pakistan News via ntfy

Automatically collects **important national (Pakistan) and international news**
every **30 minutes** from Pakistani English-language news websites and global feeds,
runs a **7-Step Canonical Event Deduplication & Story Evolution Pipeline**, and
delivers one notification per real-world event via [ntfy.sh](https://ntfy.sh).

## 🔒 Privacy note

Your ntfy topic lives in a **GitHub Actions secret** (`NTFY_TOPIC`), never in
this repo. To create your own topic, pick any random name (e.g.
`NewsPakistan_` + 10 random chars).

## 📬 Subscribe

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.ntfy.app) / [iOS](https://apps.apple.com/app/ntfy/id1625396347)) or use the web dashboard at <https://ntfy.sh>.
2. Subscribe on your phone using the topic you created (the same one you set as
   the `NTFY_TOPIC` secret).

Or via CLI:

```bash
curl -s ntfy.sh/<YOUR_TOPIC_NAME>
```

## 🧠 7-Step Canonical Story Deduplication & Evolution Workflow

For every incoming article, `scripts/fetch_news.py` executes:

### Step 1: Fetch Full Content
- Opens the original article URL and extracts the complete editorial body (`<p>` paragraphs), not just the RSS title or snippet.
- Strips navigation elements, advertisements, related links, scripts, and boilerplate wrappers (including Google News aggregator wrappers).

### Step 2: Create Canonical Story Summary & Event Fingerprint
- Generates:
  - **Canonical Headline**
  - **100–150 Word Summary** synthesized from the extracted editorial content
  - **Key Entities**: People, Organizations, Countries/Locations, Products, and Events
  - **Key Facts**: What happened, Where it happened, When it happened, Why it matters, and 3 concise bullet points
  - **One-Sentence Event Fingerprint** describing the underlying real-world event
  - **128-D Semantic Embedding Vector** representation of the story

### Step 3: 72-Hour Rolling Story Memory Database
- Maintains a rolling memory (`seen_articles.json`) containing the Story ID, original headline, canonical summary, key entities, key facts, Event Fingerprint, semantic embedding, and timestamp.
- **Retention period**: Keeps records for **72 hours** (`SEEN_SCAN_HOURS = 72`) and automatically prunes entries older than 72 hours.

### Step 4 & Step 5: Multi-Dimensional Duplicate Detection & Thresholds
Compares each incoming article against all stories stored within the last 72 hours using semantic vector similarity, Event Fingerprint overlap, entity overlap, and fact similarity:
- **Semantic similarity $\ge 85\%$**: Marked as `DUPLICATE`, suppressed, and logged with source and URL only.
- **Semantic similarity $70\text{–}84\%$**: Compares key entities, facts, and Event Fingerprint; if the core event is identical, marked as `DUPLICATE` and suppressed.
- **Semantic similarity $< 70\%$**: Treated as a `NEW STORY`.

### Step 6: Story Evolution Handling (`SIGNIFICANT UPDATE`)
- Distinguishes between redundant duplicates and genuine story updates (e.g., *"Microsoft announces layoffs"* followed later by *"Microsoft confirms 9,000 layoffs and publishes restructuring details"*).
- When a newer article about a stored event introduces substantially new confirmed figures, outcomes, verdicts, or developments:
  - Replaces the stored summary and updates the 72-hour memory record
  - Delivers the updated alert labeled as `SIGNIFICANT UPDATE`

### Step 7: User Delivery Format
Only stories classified as `NEW STORY` or `SIGNIFICANT UPDATE` are delivered:

```text
Headline

Summary: [100-150 word summary]

Key Facts: • Fact 1 • Fact 2 • Fact 3

Source: [source name]

Published: [timestamp]

Story Type: NEW STORY / SIGNIFICANT UPDATE

Link: [url]
```

## 🚀 Quick start (local)

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
pip install -r requirements.txt
NTFY_TOPIC=your_random_topic_name python scripts/fetch_news.py
```
