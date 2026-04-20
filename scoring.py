"""scoring.py — AI categorize + score via Groq.

Sends all articles to Groq in batches of 40. For each article, the AI
returns category + score as JSON. No rewriting happens here — that's
in rewrite.py. This keeps token cost low.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import List

from groq import Groq

logger = logging.getLogger(__name__)

_client = None

_DEFAULT_CATEGORIZE_PROMPT = """\
You are a financial news curator for VC/PE professionals. For each article below, return a JSON array with:
- "index": the article's index number
- "category": one of "PE", "VC", "PC", "ASIA_IPO", "US_IPO", or "NONE"
- "score": integer 1-5 (5 = must-read deal news, 1 = irrelevant fluff)

Category definitions:
- PE: buyouts, acquisitions by PE firms, fund closings, PE exits, take-privates, portfolio company news
- VC: startup funding rounds, venture investments, accelerator news, seed/series raises
- PC: private lending, BDC news, credit fund launches, loan defaults, CLOs, redemptions, credit spreads, direct lending, mezzanine, unitranche
- ASIA_IPO: companies listing on HKEX/SGX/TSE/SSE/SZSE, Asian startup funding, Asian market deals
- US_IPO: companies listing on NYSE/Nasdaq, S-1 filings, SPAC mergers, IPO pricings
- NONE: doesn't fit any category, discard

IMPORTANT — PE vs PC disambiguation:
Private Credit and Private Equity overlap in firm names (Apollo, Blackstone, KKR, Ares, Blue Owl all do both). Route based on the ACTIVITY described, not the firm name:
- If about LENDING, LOANS, DEBT FUNDS, CLOs, BDCs, REDEMPTIONS, DEFAULT RATES, CREDIT SPREADS → Private Credit
- If about BUYING/ACQUIRING COMPANIES, BUYOUTS, PORTFOLIO COMPANIES, EQUITY EXITS → Private Equity
- Example: "Apollo acquires healthcare company" → PE. "Apollo's debt fund raises $500M CLO" → PC.
- When in doubt between PE and PC, if the words "loan", "lend", "credit", "debt", "BDC", "CLO", or "redemption" appear → Private Credit.

Scoring guide — score primarily on CONTENT RELEVANCE (80%) with a small boost for source quality (20%):

- 5: Specific deal/transaction with names, numbers, and action from any source (e.g., "KKR closes $4.6B healthcare buyout")
- 4: Significant industry move with concrete details (e.g., "SEC approves new IPO disclosure rules"), OR a score-5-quality story from a top-tier source (Bloomberg, WSJ, FT, Reuters)
- 3: Relevant to the category but somewhat generic or lacking specifics (e.g., "PE deal activity rises in Q1")
- 2: Tangentially related, mostly commentary or opinion
- 1: Off-topic for this category regardless of source. A Bloomberg article about currency moves is a 1 in PE. A Bloomberg article about stock gains with no PE/VC/PC angle is a 1.

SOURCE REPUTATION RULE: A top-tier source can bump a 4 up to a 5, or a 3 up to a 4 — but it can NEVER make an irrelevant article relevant. If the article content does not describe an activity that fits the category definition, it scores 1-2 regardless of source. An off-topic Bloomberg article scores lower than an on-topic article from an unknown blog.

Each article can only belong to ONE category. If an article could fit multiple, pick the most specific match.

Return ONLY valid JSON. No explanation, no markdown.

Articles:
"""

_PROMPTS_DIR = Path(__file__).parent / "ai" / "prompts"


def _load_prompt(filename: str, fallback: str) -> str:
    """Load a prompt from ai/prompts with fallback to in-code default."""
    prompt_path = _PROMPTS_DIR / filename
    try:
        text = prompt_path.read_text(encoding="utf-8").strip()
        if text:
            return text + "\n"
    except Exception as exc:
        logger.warning("Prompt load failed for %s (%s). Using fallback.", prompt_path, exc)
    return fallback


_CATEGORIZE_PROMPT = _load_prompt("scoring_categorize_prompt.txt", _DEFAULT_CATEGORIZE_PROMPT)


def _get_client(model: str) -> Groq:
    """Lazy-init and return the Groq client."""
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is required for AI scoring.")
        _client = Groq(api_key=api_key)
    return _client


def _extract_json(raw: str):
    """Robustly extract a JSON array or object from a model response."""
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    for start_char, end_char in [('[', ']'), ('{', '}')]:
        start = raw.find(start_char)
        end = raw.rfind(end_char)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Could not extract JSON from response: {raw[:200]}")


def _call(client: Groq, model: str, prompt: str, max_tokens: int = 4096) -> str:
    """Make a Groq API call with exponential backoff on 429s (max 3 retries)."""
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
            if "429" in str(exc) or "rate" in str(exc).lower():
                wait = 2 ** (attempt + 1)
                logger.warning("Rate limited, retrying in %ds (attempt %d/3)", wait, attempt + 1)
                time.sleep(wait)
            else:
                if attempt < 2:
                    logger.warning("Groq API error (attempt %d/3): %s", attempt + 1, exc)
                    time.sleep(2)
                else:
                    raise
    raise RuntimeError("Groq API failed after 3 retries")


def _format_articles_for_scoring(articles: List[dict], start_index: int = 0) -> str:
    """Format articles as numbered lines for the scoring prompt."""
    lines = []
    for i, a in enumerate(articles, start=start_index):
        hint = ""
        if a.get("source_category"):
            hint = f" (source: {a['source_category']})"
        title = a.get("title", "")
        desc = a.get("description", "")[:200]
        lines.append(f"[{i}{hint}]: {title} | {desc}")
    return "\n".join(lines)


def batch_categorize_and_score(articles: List[dict], config: dict) -> List[dict]:
    """Categorize and score all articles via Groq in batches.

    Each article gets 'category' (PE/VC/PC/ASIA_IPO/US_IPO/NONE) and
    'score' (1-5) fields. Articles scored NONE are filtered out.
    Returns only articles with a valid category.
    """
    model = config.get("scoring", {}).get("scoring_model", "llama-3.3-70b-versatile")
    batch_size = config.get("scoring", {}).get("batch_size", 40)
    client = _get_client(model)

    for batch_start in range(0, len(articles), batch_size):
        batch = articles[batch_start:batch_start + batch_size]
        prompt = _CATEGORIZE_PROMPT + _format_articles_for_scoring(batch, batch_start)

        try:
            raw = _call(client, model, prompt, max_tokens=4096)
            results = _extract_json(raw)
            if not isinstance(results, list):
                results = [results]

            result_map = {}
            for r in results:
                if isinstance(r, dict) and "index" in r:
                    result_map[r["index"]] = r

            for i, article in enumerate(batch, start=batch_start):
                r = result_map.get(i, {})
                cat = r.get("category", "NONE")
                if cat not in ("PE", "VC", "PC", "ASIA_IPO", "US_IPO"):
                    cat = "NONE"
                article["category"] = cat
                article["score"] = int(r.get("score", 1))

        except Exception as exc:
            logger.error(
                "Scoring batch failed (articles %d-%d): %s",
                batch_start, batch_start + len(batch) - 1, exc,
            )
            for article in batch:
                article.setdefault("category", "NONE")
                article.setdefault("score", 1)

        # Rate-limit buffer between batches
        if batch_start + batch_size < len(articles):
            logger.info("Rate-limit buffer: waiting 15s before next batch...")
            time.sleep(15)

    scored = [a for a in articles if a.get("category", "NONE") != "NONE"]
    logger.info("Scored: %d articles → %d kept (non-NONE)", len(articles), len(scored))
    return scored
