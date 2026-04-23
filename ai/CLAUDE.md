# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the pipeline

```bash
# Install dependencies
pip install -r config/requirements.txt

# Test run — no email sent, saves HTML to preview/email_preview.html
python main.py --dry-run

# Fastest test — skips all AI calls entirely
python main.py --dry-run --no-score

# Show which sources succeeded/failed/returned 0 articles + timing
python main.py --dry-run --verbose

# Send to a specific subscriber list
python main.py --subscribers subscribers/hkt.json
```

Secrets go in `config/.env` (git-ignored). Copy `config/.env.example` to get started. Required keys: `GROQ_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`. Optional: `BRAVE_API_KEY`.

## Architecture

The pipeline is a linear 7-step chain. Each Python file owns exactly one concern:

| File | Responsibility |
|---|---|
| `main.py` | CLI + pipeline orchestration only (`build_digest`, `main`) |
| `sources.py` | Fetch all RSS/Google News/Brave Search into one flat global pool |
| `dedup.py` | Fuzzy dedup with industry-term stripping + source reputation ranking |
| `filter.py` | Finance-gate keyword filter with premium source bypass |
| `scoring.py` | Groq API: categorize + score in batches of 40 |
| `selection.py` | Greedy cross-category selection (local, zero AI cost) |
| `rewrite.py` | Groq API: headline/summary rewrite + final semantic dedup per category |
| `render.py` | Jinja2 HTML rendering, terminal output, date formatting |
| `send.py` | Gmail SMTP sending, subscriber file loading |

### Key design decisions

**Single flat pool** — all sources (Google News RSS, Brave Search, specialty RSS, broad RSS) dump into one list with no category pre-assignment. The AI in `scoring.py` decides category.

**Three source types** — Google News RSS (primary), Brave Search News API (supplementary, one query per category), and specialty/broad RSS feeds. Bloomberg RSS fetched ONCE globally, not per-category.

**Finance gate with premium bypass** — `filter.py:finance_gate()` does a cheap keyword check to drop off-topic articles. Premium domains (Bloomberg, WSJ, FT, Reuters, CNBC, Barron's) bypass the filter entirely.

**Smart dedup** — `dedup.py` strips industry stopwords before fuzzy matching to avoid false positives. When duplicates are found, keeps the article from the more reputable source (tier 1 > tier 2 > tier 3).

**Greedy cross-category selection** — `selection.py:greedy_select()` picks top-scored unique articles in a single pass across all categories. No iterations or backfill needed — highest-scored unique articles naturally bubble up.

**AI called exactly twice per run** — once for categorize+score (1-2 batch calls), once per category for rewrite+dedup (~5 calls). Total: ~7 Groq calls max.

**Scoring scale:**
- 5 = must-see, specific deal with names + numbers
- 4 = significant industry move with concrete details
- 3 = relevant but generic
- 2 = tangentially related
- 1 = not relevant
- `min_score: 3` in config.yaml means scores 1–2 are dropped

### Article dict structure

Every article through the pipeline carries:
```python
{
    "title": str,
    "description": str,
    "url": str,
    "source_name": str,              # publisher name
    "source_domain": str,            # e.g. "bloomberg.com"
    "published_date": str | None,    # ISO 8601
    "source_category": str | None,   # set for specialty RSS feeds
    "_source_type": "rss" | "google_news" | "brave",
    # Added by scoring.py:
    "category": str,                 # "PE" | "VC" | "PC" | "ASIA_IPO" | "US_IPO"
    "score": int,                    # 1–5
    # Added by rewrite.py:
    "rewritten_headline": str,
    "rewritten_summary": str,
}
```

### Adding a category

1. Add a block to `config/config.yaml` under `categories:` (with google_news_queries, brave_query, rss_sources)
2. Add the AI code to `_CATEGORIZE_PROMPT` in `scoring.py`
3. Add the short code to `CATEGORY_ORDER` in `selection.py`
4. Add the mapping to `CATEGORY_KEY_MAP` in `render.py`

### Subscriber lists

Subscriber JSON files live in `subscribers/`. The default is `subscribers/subscribers.json`. The GitHub Actions workflow uses `subscribers/hkt.json` (HKT, Mon–Fri) and `subscribers/est.json` (EST, daily). Override at runtime with `--subscribers`.
