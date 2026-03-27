"""
scoring.py — Categorise, score, and rewrite articles via the Groq API.

Two public functions:
  batch_categorize_and_score — assign category + score 1-5 (cheap, JSON-only output)
  batch_rewrite              — rewrite summaries for top articles
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
        end   = raw.rfind(close_ch) + 1
        if start != -1 and end > start:
            return json.loads(raw[start:end])
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
   5 — Named firm + dollar amount + discrete event (deal, fund close, IPO filing)
   4 — Named firm + confirmed event, one detail missing (size or parties unknown)
   3 — Named firms, analytical rather than breaking news
   2 — Vague or speculative, no specific firm or deal
   1 — Generic outlook, trend piece, or commentary

CATEGORY DEFINITIONS:
PE: buyouts, acquisitions by PE firms, fund closings, PE exits, take-privates, portfolio company deals
VC: startup funding rounds (US/Europe focus), venture investments, accelerator news
PC: private lending, BDC news, credit fund launches, loan defaults, CLOs, leveraged loans
ASIA_IPO: companies listing on HKEX, Tokyo Stock Exchange, SGX, Shanghai/Shenzhen; Asian startup funding
US_IPO: companies listing on NYSE/Nasdaq, S-1 filings, SPAC mergers
DISCARD: doesn't fit any category, or not relevant to a VC/PE audience

DEDUPLICATION:
Some articles may cover the same story (same company, same event) from different sources. \
When you spot duplicates, score only the BEST version and DISCARD the rest. \
Prefer the version from the more reputable source (Bloomberg, WSJ, FT > other outlets). \
If reputation is equal, prefer the non-paywalled source that gives readers a free view.

Short descriptions are fine — some sources are paywalled. Score on the headline if needed.

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


def _format_articles_block(articles: List[dict]) -> str:
    """Format articles for the AI prompt — title + source + description."""
    lines = []
    for i, a in enumerate(articles):
        lines.append(f"[{i}] {a.get('title', '').strip()[:200]}")
        lines.append(f"    Source: {a.get('source', '')}")
        desc = (a.get("description") or "").strip()[:300]
        if desc:
            lines.append(f"    {desc}")
        lines.append("")
    return "\n".join(lines)


def _categorize_batch(articles: List[dict], model: str) -> None:
    """Categorise + score a single batch. Mutates articles in place."""
    prompt = _CATEGORIZE_PROMPT.format(
        articles_block=_format_articles_block(articles)
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


def _format_rewrite_block(articles: List[dict]) -> str:
    """Rewrite formatter — title + description only."""
    lines = []
    for i, a in enumerate(articles):
        lines.append(f"[{i}] {a.get('title', '').strip()[:200]}")
        desc = (a.get("description") or "").strip()[:300]
        if desc:
            lines.append(f"    {desc}")
        lines.append("")
    return "\n".join(lines)


def batch_rewrite(articles: List[dict], config: dict) -> List[dict]:
    """
    Rewrite summaries for a list of articles in a single API call.
    Expects at most ~25 articles (top 5 per category).
    Falls back to original description on failure.
    """
    if not articles:
        return []

    model = config["scoring"]["model"]
    prompt = _REWRITE_PROMPT.format(rewrite_block=_format_rewrite_block(articles))

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
