"""scoring.py — AI categorisation, scoring, dedup, and rewriting via the Groq API.

All Groq API interactions live here. Three public functions:
  batch_categorize_and_score — assign category + score 1-5 for the full article pool
  final_dedup_check          — AI-powered cross-category duplicate removal (up to 3 passes)
  batch_rewrite              — rewrite summaries for the top articles per category
"""

import json
import logging
import os
import time
from typing import List

logger = logging.getLogger(__name__)


_groq_client = None


def _get_client(model: str):
    global _groq_client
    if _groq_client is None:
        try:
            from groq import Groq
        except ImportError as exc:
            raise ImportError(
                "groq is required. Run:  pip install groq"
            ) from exc

        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY is not set.\n"
                "  1. Create a free account at console.groq.com\n"
                "  2. Go to console.groq.com/keys and create an API key\n"
                "  3. Add it to config/.env:  GROQ_API_KEY=gsk_..."
            )
        _groq_client = Groq(api_key=api_key)
        logger.info("Groq client initialised (model: %s)", model)
    return _groq_client


def _extract_json(raw: str):
    """Pull a JSON array or object out of the model response robustly."""
    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith(("[", "{")):
                raw = part
                break
    for open_ch, close_ch in [("[", "]"), ("{", "}")]:
        start = raw.find(open_ch)
        if start == -1:
            continue
        # Try from the last closing bracket inward until parse succeeds
        end = raw.rfind(close_ch) + 1
        while end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                # Shrink: find the previous closing bracket
                end = raw.rfind(close_ch, start, end - 1) + 1
    raise ValueError(f"No JSON found in response: {raw[:200]}")


def _call(client, model: str, prompt: str, max_tokens: int = 1024) -> str:
    """Make one API call with simple retry on rate-limit (429)."""
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            return resp.choices[0].message.content
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "rate" in msg.lower():
                wait = 10 * (attempt + 1)
                logger.warning("Rate limit hit — waiting %ds before retry %d/3", wait, attempt + 1)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Groq API failed after 3 retries")


# ── Step 1: Categorise + Score ───────────────────────────────────────────

_CATEGORIZE_PROMPT = """\
You are curating a daily news digest for a general partner at a VC/PE firm. \
They need to stay informed across private equity deals, venture capital rounds, \
private credit developments, US IPOs, and Asian market IPOs/startups. \
Assign each article to the single most relevant category. Score based on how \
important this news is for client conversations — prioritize specific deals, \
funding rounds, and market-moving events over generic commentary.

For each article, provide:
1. category — exactly one of: PE, VC, PC, ASIA_IPO, US_IPO, or DISCARD
2. score — 1 to 5:
   5 — Must-see for a VC/PE partner: major market-moving event, landmark deal, \
       significant funding round, or breaking news with named firms and dollar amounts
   4 — Important and timely: confirmed event with named firms, may be missing one \
       detail (exact size or counterparty), but still highly relevant to the category
   3 — Relevant to the category: covers named firms or specific developments, but \
       more analytical or incremental rather than breaking or market-moving
   2 — Tangentially relevant: mentions the sector but lacks specifics, speculative, \
       or only loosely connected to the category
   1 — Not relevant: generic commentary, opinion pieces, trend recaps, or articles \
       that do not meaningfully relate to the category

SOURCE PREFERENCES:
Prefer articles from reputable sources: Bloomberg, The Economist, Reuters, \
Financial Times (FT), Wall Street Journal (WSJ), and other well-known established outlets. \
Give a slight score boost (+1) to articles from these sources over lesser-known outlets \
covering the same topic.

CATEGORY DEFINITIONS:
PE (private equity): buyouts, acquisitions by PE firms, fund closings, PE exits, take-privates, portfolio company deals
VC (venture capital): startup funding rounds (US/Europe/Asia focus), venture investments, accelerator news, y-combinator, accelerator, founder
PC (private credit): private lending, BDC news, credit fund launches, loan defaults, CLOs, leveraged loans, direct lending, private debt, mezzanine, unitranche
ASIA_IPO (Initial Public Offering): companies listing on HKEX, Tokyo Stock Exchange, SGX, Shanghai/Shenzhen; Asian startup funding, startup
US_IPO (Initial Public Offering): companies listing on NYSE/Nasdaq, S-1 filings, SPAC mergers, Startup, Series A, Series B, Series C, Series D, Seed
DISCARD: doesn't fit any category, or not relevant to a VC/PE audience

Short descriptions are fine — some sources are paywalled. Score on the headline if needed. \
Do NOT deduplicate here — just categorise and score every article independently.

ARTICLES:
{articles_block}

Return ONLY a JSON array, no extra text:
[{{"index": 0, "category": "PE", "score": 4}}, ...]
"""

_CATEGORY_MAP = {
    "PE": "private_equity",
    "VC": "venture_capital",
    "PC": "private_credit",
    "ASIA_IPO": "asia_ipo",
    "US_IPO": "us_ipo",
}

_BATCH_SIZE = 50  # max articles per AI categorisation call


def _format_article_block(articles: List[dict], include_source: bool = True) -> str:
    """Format articles for AI prompts — title + optional source + description.

    Used by both categorisation (with source) and rewrite (without source) prompts.
    """
    lines = []
    for i, a in enumerate(articles):
        lines.append(f"[{i}] {a.get('title', '').strip()[:200]}")
        if include_source:
            lines.append(f"    Source: {a.get('source', '')}")
        desc = (a.get("description") or "").strip()[:300]
        if desc:
            lines.append(f"    {desc}")
        lines.append("")
    return "\n".join(lines)


def _categorize_batch(articles: List[dict], model: str) -> None:
    """Categorise + score a single batch. Mutates articles in place."""
    prompt = _CATEGORIZE_PROMPT.format(
        articles_block=_format_article_block(articles)
    )
    # ~16 tokens per entry (index + category + score) + JSON overhead
    max_tokens = min(len(articles) * 20 + 60, 2000)

    try:
        client = _get_client(model)
        raw = _call(client, model, prompt, max_tokens=max_tokens)
        results = _extract_json(raw)

        result_map = {r["index"]: r for r in results if "index" in r}
        for i, article in enumerate(articles):
            r = result_map.get(i, {})
            article["score"] = int(r.get("score", 0))
            cat_code = (r.get("category") or "DISCARD").upper()
            cat_key = _CATEGORY_MAP.get(cat_code)
            if cat_key:
                article["section"] = cat_key
            else:
                article["score"] = 0  # DISCARD

    except Exception as exc:
        logger.warning("Categorise+score batch failed: %s — all get score 0", exc)
        for article in articles:
            article.setdefault("score", 0)


def batch_categorize_and_score(articles: List[dict], config: dict) -> List[dict]:
    """
    Categorise and score all articles.  Splits into batches of _BATCH_SIZE
    if the pool is large.  Returns the same list with 'score' and 'section'
    set on each article.  Articles categorised as DISCARD get score=0.
    """
    if not articles:
        return []

    model = config["scoring"]["model"]
    n_batches = (len(articles) + _BATCH_SIZE - 1) // _BATCH_SIZE

    logger.info(
        "Categorise+score %d articles in %d batch(es) via Groq (%s)...",
        len(articles), n_batches, model,
    )

    for batch_idx in range(n_batches):
        start = batch_idx * _BATCH_SIZE
        end = min(start + _BATCH_SIZE, len(articles))
        batch = articles[start:end]

        logger.info("  Batch %d/%d: articles %d–%d", batch_idx + 1, n_batches, start, end - 1)
        _categorize_batch(batch, model)

        if batch_idx < n_batches - 1:
            time.sleep(2)  # rate-limit courtesy between batches

    categorised = sum(1 for a in articles if a.get("score", 0) > 0)
    logger.info("Categorise+score done: %d/%d articles kept", categorised, len(articles))
    return articles


# ── Step 1b: Final cross-batch deduplication ─────────────────────────────

_FINAL_DEDUP_PROMPT = """\
You are checking a curated news digest for duplicate stories. \
Two articles are duplicates if they cover the SAME event/deal/announcement, \
even if the wording or source differs.

When choosing which duplicate to KEEP, apply these rules in order:
1. Prefer the ORIGINAL source — the outlet that broke or first reported the story. \
   Remove aggregator rewrites (e.g. "Reuters reports that…", "According to Bloomberg…") \
   in favour of the original article itself.
2. Prefer reputable sources. Priority (highest first): Bloomberg, The Economist, \
   Reuters, Financial Times (FT), Wall Street Journal (WSJ), and other well-known \
   established outlets. Lesser-known or niche sources should be removed when a \
   reputable source covers the same story.
3. If reputation is equal, prefer the non-paywalled version.

If a duplicate appears across different categories, re-evaluate which \
category is the BEST fit and keep it there.

ARTICLES (grouped by category):
{articles_block}

If there are NO duplicates, return exactly: {{"duplicates": []}}

If there ARE duplicates, return:
{{"duplicates": [{{"remove_index": 3, "reason": "duplicate of index 1, same Apollo deal, keeping Bloomberg original over aggregator rewrite"}}]}}

Return ONLY JSON, no extra text.
"""


def final_dedup_check(
    top_by_cat: dict,
    reserve_by_cat: dict,
    category_keys: list,
    config: dict,
    final_cap: int = 5,
) -> int:
    """
    AI-powered final dedup across the top picks per category.
    Runs up to 4 iterations. Each pass:
      1. Send top picks to AI to find duplicates across/within categories
      2. AI picks the best category + source for each duplicate story
      3. Remove the losing article from its category
      4. Backfill from the reserve pool so each category stays at final_cap

    Mutates top_by_cat in place. Returns number of iterations run.
    """
    model = config["scoring"]["model"]
    max_iterations = 4

    # Track which reserve articles are already in top picks (by URL)
    used_urls = set()
    for cat_key in category_keys:
        for a in top_by_cat.get(cat_key, []):
            used_urls.add(a.get("url"))

    for iteration in range(1, max_iterations + 1):
        # Build a flat indexed list of top articles grouped by category
        index_map = {}  # global_index → (cat_key, local_index)
        gi = 0
        lines = []
        for cat_key in category_keys:
            articles = top_by_cat.get(cat_key, [])
            if not articles:
                continue
            cat_label = cat_key.replace("_", " ").upper()
            lines.append(f"── {cat_label} ──")
            for li, a in enumerate(articles):
                lines.append(f"[{gi}] {a.get('title', '').strip()[:200]}")
                lines.append(f"    Source: {a.get('source', '')}")
                desc = (a.get("description") or "").strip()[:300]
                if desc:
                    lines.append(f"    {desc}")
                lines.append("")
                index_map[gi] = (cat_key, li)
                gi += 1

        if gi == 0:
            break

        logger.info("  Final dedup pass %d: checking %d articles across all categories", iteration, gi)

        max_tokens = min(gi * 15 + 100, 2000)
        prompt = _FINAL_DEDUP_PROMPT.format(articles_block="\n".join(lines))
        try:
            client = _get_client(model)
            raw = _call(client, model, prompt, max_tokens=max_tokens)
            result = _extract_json(raw)

            if isinstance(result, list):
                duplicates = result
            else:
                duplicates = result.get("duplicates", [])

            if not duplicates:
                logger.info("  Final dedup pass %d: no duplicates found ✓", iteration)
                return iteration

            logger.info("  Final dedup pass %d: removing %d duplicate(s)", iteration, len(duplicates))

            # Collect indices to remove (process highest local index first so pops stay valid)
            removals = []
            for dup in duplicates:
                ri = dup.get("remove_index")
                if ri is not None and ri in index_map:
                    removals.append(index_map[ri])

            # Remove duplicates and track which categories lost articles
            cats_needing_backfill = set()
            for cat_key, local_idx in sorted(removals, key=lambda x: -x[1]):
                articles = top_by_cat.get(cat_key, [])
                if local_idx < len(articles):
                    removed = articles.pop(local_idx)
                    used_urls.discard(removed.get("url"))
                    cats_needing_backfill.add(cat_key)

            # Backfill: pull next-best article from reserve into categories that lost one
            for cat_key in cats_needing_backfill:
                top = top_by_cat.get(cat_key, [])
                reserve = reserve_by_cat.get(cat_key, [])
                while len(top) < final_cap and reserve:
                    candidate = reserve.pop(0)
                    if candidate.get("url") not in used_urls:
                        top.append(candidate)
                        used_urls.add(candidate.get("url"))
                        break

            time.sleep(2)  # rate-limit courtesy

        except Exception as exc:
            logger.warning("  Final dedup pass %d failed: %s — skipping", iteration, exc)
            return iteration

    return max_iterations


# ── Step 2: Rewrite ──────────────────────────────────────────────────────

_REWRITE_PROMPT = """\
Write a 2–3 sentence summary for each article. Do NOT restate the headline. \
Sentence 1: pack in the key specifics the headline misses — firm names, dollar amounts, \
deal terms, parties involved. Sentences 2–3: add context on why it matters, what it \
signals for the market, or the likely effect.

ARTICLES:
{rewrite_block}

Return ONLY a JSON array, no extra text:
[{{"index": 0, "rewritten_summary": "..."}}, ...]
"""


def batch_rewrite(articles: List[dict], config: dict) -> List[dict]:
    """
    Rewrite summaries for a list of articles in a single API call.
    Expects at most ~25 articles (top 5 per category).
    Falls back to original description on failure.
    """
    if not articles:
        return []

    model = config["scoring"]["model"]
    prompt = _REWRITE_PROMPT.format(rewrite_block=_format_article_block(articles, include_source=False))

    try:
        client = _get_client(model)
        logger.info("Rewriting %d articles via Groq (%s)...", len(articles), model)
        # ~80 tokens per summary × up to 25 articles + overhead
        max_tokens = min(len(articles) * 100 + 60, 3000)
        raw = _call(client, model, prompt, max_tokens=max_tokens)
        results = _extract_json(raw)

        rewrite_map = {r["index"]: r for r in results if "index" in r}
        for i, article in enumerate(articles):
            r = rewrite_map.get(i, {})
            article["rewritten_summary"] = r.get("rewritten_summary") or article.get("description", "")

        logger.info("Batch rewrote %d articles", len(articles))

    except Exception as exc:
        logger.warning("Batch rewrite failed: %s — using original descriptions", exc)
        for article in articles:
            article.setdefault("rewritten_summary", article.get("description", ""))

    return articles
