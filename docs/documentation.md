# VC/PE Daily Digest

A plain-English guide to setting up, running, and customising your automated deal-news email.

---

## Table of Contents

1. [What is this?](#what-is-this)
2. [Project structure](#project-structure)
3. [How it works](#how-it-works)
4. [Prerequisites](#prerequisites)
5. [Installation](#installation)
6. [API keys](#api-keys)
7. [Subscribers](#subscribers)
8. [config.yaml](#configyaml)
9. [Scoring system](#scoring-system)
10. [News sources](#news-sources)
11. [Testing](#testing)
12. [Sending live](#sending-live)
13. [Scheduling](#scheduling)
14. [FAQ](#faq)

---

## What is this?

This tool reads financial news feeds every morning, selects the most relevant deal news across five categories, and emails a clean digest to your subscriber list. Categories covered: Private Equity, Venture Capital, Private Credit, Asia IPO & Startups, US IPO.

The AI is the core feature. Rather than using keyword matching to sort articles into categories, all articles are fetched into a single pool, deduplicated globally, then sent to the AI in one batch. The AI assigns each article to exactly one category, scores it 1-5, and the top articles get AI-rewritten summaries. This eliminates duplicate articles across categories and produces better categorisation than keyword rules.

---

## Project structure

```
News_Summary/
│
├── main.py                  Entry point — CLI parsing + pipeline orchestration
├── sources.py               Fetches articles from ALL sources into one flat pool
│                            (Google News RSS, broad RSS — no category routing)
├── filters.py               Finance gate (keyword relevance filter) and debug logging
├── dedup.py                 Fuzzy deduplication via rapidfuzz (threshold 75/100)
├── scoring.py               AI categorisation + scoring, cross-batch dedup, summary rewriting
│                            All Groq API calls live here (2-3 calls per run)
├── render.py                HTML email rendering (Jinja2), terminal output, date formatting
├── send.py                  Loads subscribers, sends HTML email via Gmail SMTP
│
├── .gitignore               Excludes config/.env, __pycache__, generated files
│
├── config/
│   ├── config.yaml          All user-facing settings: scoring, email, sources
│   ├── email_template.html  Jinja2 HTML email layout (edit to change design)
│   ├── .env                 API keys (secret — git-ignored)
│   ├── .env.example         API key template — copy to .env and fill in
│   └── requirements.txt     Python dependencies (pip install -r config/requirements.txt)
│
├── subscribers/
│   ├── subscribers.json     Default recipient list (JSON array of {email, name})
│   ├── subscribers_est.json EST timezone subscriber list
│   └── subscribers_hk.json  HKT timezone subscriber list
│
├── docs/
│   └── documentation.md     This file
│
└── preview/
    └── email_preview.html   Generated HTML preview from --dry-run (git-ignored)
```

### Data flow

```
sources.py          filters.py       dedup.py         scoring.py          render.py    send.py
──────────          ──────────       ────────         ──────────          ─────────    ───────
Broad RSS       ─┐
Broad GNews     ─┤
Category GNews  ─┤──→ Flat pool ──→ Finance gate ──→ Global dedup ──→ AI categorise
Category RSS    ─┘                                                    + score
                                                                         │
                                                                   Group by category
                                                                   top 5 each
                                                                         │
                                                                   Final AI dedup
                                                                   (up to 3 passes)
                                                                         │
                                                                   AI rewrite winners
                                                                         │
                                                                   Render HTML ──→ Send email
```

---

## How it works

The pipeline runs in these steps:

```
Fetch all sources  →  Finance gate  →  Global dedup  →  AI categorise + score  →  Final AI dedup  →  AI rewrite winners  →  Render  →  Send
```

### Step 1 — Fetch

ALL articles are fetched into a single flat pool — no category assignment yet:

- **Broad RSS feeds** (e.g. Bloomberg Markets, WSJ, Economist, Reuters) — fetched once
- **Broad Google News queries** — Bloomberg-specific finance queries
- **Per-category Google News queries** — fetched from config but pooled together
- **Per-category RSS feeds** — dedicated sources, also pooled

Only articles from the last 24 hours are kept (falls back to 48 hours if fewer than 50 articles). All feeds have a 15-second timeout. URL-level dedup removes exact duplicates during fetching.

### Step 2 — Finance gate

A cheap, universal relevance check (lives in `filters.py`). If the article title and description contain none of these finance terms, it's discarded:

> deal, acquire, fund, raise, IPO, listing, invest, lend, loan, credit, startup, merger, buyout, stake, billion, million, venture, equity, debt, default, exit, portfolio

This catches general news (weather, sports, politics) that leaked in from broad RSS feeds while letting all financial articles through regardless of category. It is NOT a category filter.

### Step 3 — Global dedup

Uses fuzzy string matching (rapidfuzz, threshold 75/100) across the ENTIRE pool to identify articles covering the same story from different sources. Keeps only the most recent version. An article that appeared in PE, VC, and IPO feeds now exists exactly once.

### Step 4 — AI categorise + score (one batch)

The entire deduplicated pool is sent to Groq in one batch (split into chunks of 50 if volume is high). The AI does two things for each article:

1. **Assigns exactly one category:** PE, VC, PC, ASIA_IPO, US_IPO, or DISCARD
2. **Scores 1-5** for importance to a VC/PE general partner

This eliminates all overlap by design — one article, one category, decided by the AI which understands context far better than keyword matching.

### Step 5 — Final AI dedup

After scoring, a second AI pass checks for duplicate stories across all categories (up to 3 iterations). This catches duplicates that fuzzy matching missed — e.g. articles with different wording but covering the same deal. Prefers more reputable sources (Bloomberg, WSJ, FT).

### Step 6 — AI rewrite (winners only)

Articles scoring >= `min_score` (default 3) are grouped by category, top 5 per category are kept. Only these winners (at most 25 articles) are sent for summary rewriting. Summaries lead with key specifics the headline misses, followed by market context.

### Step 7 — Send

The digest is rendered into an HTML email using the Jinja2 template (`render.py`) and delivered via Gmail SMTP (`send.py`).

**Total Groq API calls per run:** 1-2 for categorise+score (depends on pool size) + 1-3 for final dedup + 1 for rewrite = 3-6 calls.

---

## Prerequisites

- Python 3.11 or higher
- A Groq account (free, no credit card) at console.groq.com
- A Gmail account with an App Password for SMTP sending

---

## Installation

```bash
pip install -r config/requirements.txt
cp config/.env.example config/.env
# Open config/.env and fill in your keys
```

---

## API keys

Open `config/.env` and replace the placeholder values:

```
GROQ_API_KEY=gsk_...
GMAIL_ADDRESS=your_gmail@gmail.com
GMAIL_APP_PASSWORD=your_app_password_here
```

Never commit `config/.env` to git or share it.

| Key | Source |
|---|---|
| `GROQ_API_KEY` | console.groq.com/keys |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Google Account > Security > App Passwords |

---

## Subscribers

Edit `subscribers/subscribers.json`:

```json
[
  {"email": "john@example.com", "name": "John Smith"},
  {"email": "jane@vcfirm.com",  "name": "Jane Doe"}
]
```

A `subscribers.csv` file with `email` and `name` columns also works. Update `subscribers_file` in `config/config.yaml` to point to it.

Multiple subscriber lists are supported for different timezones (e.g. `subscribers_hk.json`, `subscribers_est.json`). Override at runtime with `--subscribers subscribers/subscribers_hk.json`.

---

## config.yaml

### Settings block

| Key | Purpose |
|---|---|
| `final_articles_per_category` | Hard cap on articles shown per section in the final digest |
| `broad_rss_sources` | RSS feeds fetched once into the global pool |
| `broad_google_news_queries` | Google News queries for broad financial coverage |

### Scoring block

```yaml
scoring:
  model: "llama-3.3-70b-versatile"
  min_score: 3
```

Available Groq models (all free, limits reset daily):

| Model | Speed | Quality | Requests/day |
|---|---|---|---|
| `llama-3.1-8b-instant` | Fastest | Good | 14,400 |
| `gemma2-9b-it` | Fast | Good | 14,400 |
| `llama-3.3-70b-versatile` | Slower | Best | 1,000 |

### Email block

```yaml
email:
  subject: "VC/PE Daily News — {date}"
  subscribers_file: "subscribers/subscribers.json"
```

### Category block structure

Each category has Google News queries and optional RSS feeds. The AI handles all categorisation — no keyword filters needed.

```yaml
categories:
  private_equity:
    google_news_queries:
      - '"private equity" buyout OR acquisition OR deal'

    rss_sources:
      - { name: "FT PE", url: "https://...", paywalled: true }
```

Categories can optionally specify a `display_name` for the email section header (e.g. `"ASIA IPO & STARTUPS"`). Without one, the key is converted automatically (`asia_ipo` → `ASIA IPO`).

---

## Scoring system

Articles are scored 1 to 5 by the AI. The top `final_articles_per_category` (default 5) per section appear in the digest, sorted highest first.

| Score | What it means | Example |
|---|---|---|
| 5 | Named entity, dollar amount, and discrete event | "Apollo Closes $25B Credit Fund, Largest on Record" |
| 4 | Named entity and confirmed event, one detail missing | "KKR in Talks to Acquire [Company], Terms Not Disclosed" |
| 3 | Named firms, primarily analytical rather than breaking news | "How Blackstone Is Repositioning Its Real Estate Book" |
| 2 | Vague or speculative, no specific deal or firm named | "PE Firms Increasingly Eyeing Healthcare" |
| 1 | Generic outlook, trend recap, or opinion | "2026 Private Credit Outlook: What to Expect" |

Paywalled sources often provide only a headline and one-line snippet. The model is instructed not to penalise short descriptions.

### AI-generated summaries

Summaries are written by the AI to complement the headline, not restate it:
- **Sentence 1:** Key specifics the headline misses — firm names, dollar amounts, deal terms, parties involved.
- **Sentences 2-3:** Context on why it matters, what it signals for the market, or the likely effect.

---

## News sources

### Active sources

| Source type | Sources |
|---|---|
| Broad RSS | Bloomberg Markets, Bloomberg Business, WSJ US News, The Economist, Reuters |
| Broad GNews | Bloomberg-specific finance queries |
| PE RSS | FT PE |
| VC RSS | TechCrunch Venture, AlleyWatch |
| Asia RSS | FinanceAsia, SCMP Business, Asia Financial |
| US IPO RSS | Dealroom |
| Google News | 31+ queries across all categories |

### Removed sources

| Source | Reason |
|---|---|
| NVCA | HTTP 403 |
| Private Debt Investor | HTTP 401 |
| Reuters Business | DNS no longer resolves |
| Nasdaq RSS | Chronic timeouts |
| Seeking Alpha | Returns hundreds of unrelated articles |

---

## Testing

```bash
python main.py --dry-run                  # full pipeline, no email sent
python main.py --dry-run --verbose        # also prints per-source fetch summary
python main.py --dry-run --no-score       # skips AI scoring (fastest test)
python main.py --dry-run --debug          # saves PC keyword debug JSON
```

`--dry-run` saves `preview/email_preview.html`. Open it in a browser to preview the email.

Use `--no-score` to verify sources return relevant articles before spending API calls. Use `--verbose` to diagnose which sources succeed or fail. Use `--debug` to save a JSON file analyzing Private Credit keyword matches vs AI assignments.

---

## Sending live

```bash
python main.py
python main.py --subscribers subscribers/subscribers_hk.json   # HKT audience
python main.py --subscribers subscribers/subscribers_est.json   # EST audience
```

Confirm before going live:
- `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` are set in `config/.env`
- `subscribers/subscribers.json` has the correct addresses

---

## Scheduling

### GitHub Actions (current setup)

The project includes a GitHub Actions workflow (`.github/workflows/daily-digest.yml`) that runs two parallel jobs:

- **HKT digest** — runs at 23:29 UTC Sun-Thu (7:59 AM HKT) using `subscribers_hk.json`
- **EST digest** — runs at 11:19 UTC daily (7:19 AM EST) using `subscribers_est.json`

Manual dispatch is also supported via the Actions tab with audience selection (hk, est, or both).

### Windows (Task Scheduler)

1. Open Task Scheduler
2. Create Basic Task, trigger: Daily
3. Action: Start a program, set to `python`
4. Arguments: `main.py`
5. Start in: full path to the project folder

### Mac / Linux (cron)

```
0 7 * * * cd /path/to/News_Summary && python main.py >> digest.log 2>&1
```

---

## FAQ

**The digest is empty for a section. Why?**
Three likely causes: (1) RSS feeds had no articles in the last 24 hours, (2) all articles were filtered out by the finance gate, or (3) the AI scored everything below the threshold. Run with `--no-score --verbose` to diagnose.

**I see 429 errors from Groq.**
Your rate limit has been hit. The script retries up to 3 times with increasing backoff (10s, 20s, 30s). If it persists, wait for the daily limit to reset or switch to a smaller model with higher limits in `config/config.yaml`.

**How much does this cost per day?**
Nothing on default settings. Groq is free tier and Gmail SMTP has no per-email cost.

**Can I add a new category?**
Yes. Add a new category block in `config/config.yaml` with `google_news_queries` and optional `rss_sources`. The AI prompt in `scoring.py` also needs the new category added to `_CATEGORIZE_PROMPT` and `_CATEGORY_MAP`. Then add the section to the colour map in `config/email_template.html` if needed.

**How do I change the summary style?**
Edit `_REWRITE_PROMPT` in `scoring.py`.

**Can I change duplicate detection sensitivity?**
Yes. Adjust `SIMILARITY_THRESHOLD` in `dedup.py` (0-100 scale). Higher = only near-identical articles flagged. Lower = more aggressive merging.

**An article appears in the wrong category. Why?**
The AI assigns categories based on the headline and description. If a source consistently miscategorises, check that the category definitions in `_CATEGORIZE_PROMPT` in `scoring.py` are clear enough to distinguish the edge case.

**What if the AI categorisation call fails?**
All articles get score 0 and the digest will be empty for that run. A warning is logged. The pipeline does not crash.

**Where is the finance gate logic?**
In `filters.py`. The keyword list (`_FINANCE_GATE_WORDS`) and the `finance_gate()` function live there.

**Where is the HTML rendering logic?**
In `render.py`. This includes the Jinja2 template rendering, date formatting (EST/HKT dual timezone), terminal digest output, and category display name generation.
