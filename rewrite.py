"""rewrite.py — Per-category AI dedup + rewrite via Groq.

For each category:
  1. Run up to 3 AI dedup passes. Each pass sends the current articles
     to the AI which identifies duplicates. The less reputable duplicate
     is removed and the next reserve article backfills into its slot.
  2. Once clean, rewrite headlines/summaries in a single call.

One Groq call per dedup pass + one rewrite call per category.
"""

import logging
import re
import time
from pathlib import Path
from typing import Dict, List

from scoring import _call, _extract_json, _get_rewrite_client

logger = logging.getLogger(__name__)

# Dedicated debug logger for AI dedup — writes to debug_dedup.log (overwritten each run)
_dedup_debug_logger = logging.getLogger("dedup_debug")
_dedup_debug_logger.propagate = False  # don't echo to console

MAX_DEDUP_PASSES = 3
_PROMPTS_DIR = Path(__file__).parent / "ai" / "prompts"


def _init_dedup_debug_log():
    """Set up file handler for debug_dedup.log (called once per run)."""
    if _dedup_debug_logger.handlers:
        return  # already initialized
    log_dir = Path(__file__).parent / "preview"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "debug_dedup.log"
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    _dedup_debug_logger.addHandler(handler)
    _dedup_debug_logger.setLevel(logging.DEBUG)

_DEFAULT_DEDUP_PROMPT = """\
You are deduplicating a list of news articles for a financial digest.

Two articles are DUPLICATES if they are about the same underlying NEWS EVENT, even if:
- They are from different sources
- They use different headlines or wording
- They emphasize different angles or details of the same event
- One is a "what to know" or "explainer" and the other is the breaking news

The test: If a reader read Article A, would Article B tell them about a DIFFERENT thing that happened? If no — if both articles exist because the same thing happened — they are duplicates.

Examples:
- "KKR Raises $23B for PE Fund" vs "KKR Secures $23B in Largest-Ever Haul" → DUPLICATE (same fundraise)
- "Blue Owl Limits Redemptions" vs "Blue Owl's Fund Hit by 22% Withdrawal Request" vs "Blue Owl Paid Out Less Than Quarter of Requests" → ALL DUPLICATES (same redemption crisis)
- "SpaceX Targets $2T Valuation in IPO" vs "SpaceX's IPO Could Be Largest-Ever — What to Know" vs "SpaceX Filed to Go Public — Steps to Buy Shares" → ALL DUPLICATES (same IPO filing)
- "KKR Raises $23B Fund" vs "Apollo Closes $5B Fund" → NOT duplicates (different companies, different events)

When you find a group of 3+ articles about the same event, keep ONLY the one with the most specific, factual headline (names + numbers). Remove all others.

PROCESS — think step by step:
1. Compare every article against every other article in the list.
2. For each pair, state whether they are duplicates and why.
3. After your analysis, output your final answer.

Write your pairwise analysis first. Then on a new line write RESULT_JSON: followed by the JSON.

RESULT_JSON:
{"removed": [{"index": 1, "duplicate_of": 0, "reason": "same KKR fundraise event"}]}

If no duplicates:
RESULT_JSON:
{"removed": []}

Articles:
"""

_DEFAULT_REWRITE_PROMPT = """\
You are rewriting headlines and summaries for a professional VC/PE daily digest.

Write a 2–3 sentence summary for each article. Do NOT restate the headline. \
Sentence 1: pack in the key specifics the headline misses — firm names, dollar amounts, \
deal terms, parties involved. Sentences 2–3: add context on why it matters, what it \
signals for the market, or the likely effect.

Return JSON array:
[
  {"index": 0, "headline": "...", "summary": "..."},
  ...
]

Return ONLY valid JSON. No explanation, no markdown.

Articles:
"""


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


_DEDUP_PROMPT = _load_prompt("dedup_prompt.txt", _DEFAULT_DEDUP_PROMPT)
_REWRITE_PROMPT = _load_prompt("rewrite_prompt.txt", _DEFAULT_REWRITE_PROMPT)


def _is_high_quality_source_summary(summary: str, min_chars: int, min_words: int) -> bool:
    """Heuristic check for whether source summary is already digest-ready."""
    text = (summary or "").strip()
    if not text:
        return False

    lowered = text.lower()
    boilerplate_markers = (
        "read more",
        "click here",
        "subscribe",
        "sign up",
        "view in browser",
        "watch now",
    )
    if any(marker in lowered for marker in boilerplate_markers):
        return False

    if len(text) < min_chars:
        return False

    word_count = len(re.findall(r"\b\w+\b", text))
    return word_count >= min_words


def _format_articles(articles: List[dict]) -> str:
    """Format articles as numbered lines for prompts."""
    lines = []
    for i, a in enumerate(articles):
        title = a.get("title", "")
        desc = a.get("description", "")[:200]
        source = a.get("source_name", "")
        url = a.get("url", "")
        lines.append(f"[{i}]: {title} | {desc} | Source: {source} | URL: {url}")
    return "\n".join(lines)


def _dedup_category(articles: List[dict], client, model: str,
                    category: str = "") -> List[dict]:
    """Run a single AI dedup pass on a category's articles.

    Returns the deduplicated list (duplicates removed).
    """
    dbg = _dedup_debug_logger

    if len(articles) <= 1:
        return articles

    prompt = _DEDUP_PROMPT + _format_articles(articles)

    # Debug: log articles being sent
    dbg.debug("=" * 70)
    dbg.debug("CATEGORY: %s | ARTICLES SENT: %d", category, len(articles))
    dbg.debug("-" * 70)
    for i, a in enumerate(articles):
        dbg.debug("  [%d] %s | Source: %s | URL: %s", i, a.get("title", ""), a.get("source_name", ""), a.get("url", ""))
    dbg.debug("-" * 70)

    try:
        raw = _call(client, model, prompt, max_tokens=4096)

        # Debug: log complete raw response (chain-of-thought + JSON)
        dbg.debug("RAW MODEL RESPONSE:")
        dbg.debug("%s", raw)
        dbg.debug("-" * 70)

        # Extract JSON after RESULT_JSON: marker (discard chain-of-thought)
        json_raw = raw
        if "RESULT_JSON:" in json_raw:
            json_raw = json_raw.split("RESULT_JSON:")[-1].strip()

        result = _extract_json(json_raw)

        if isinstance(result, list):
            result = {"removed": result}

        removed = result.get("removed", [])

        # Debug: log parsed result
        dbg.debug("PARSED RESULT: %s", result)
        if not removed:
            dbg.debug("OUTCOME: No duplicates found — all articles kept")
            dbg.debug("=" * 70)
            return articles

        # Collect indices to drop
        drop_indices = set()
        for r in removed:
            if isinstance(r, dict) and "index" in r:
                drop_idx = r["index"]
                if 0 <= drop_idx < len(articles):
                    drop_indices.add(drop_idx)
                    logger.info(
                        "  Dedup drop: [%d] %s (duplicate_of [%s]: %s)",
                        drop_idx,
                        articles[drop_idx]["title"][:50],
                        r.get("duplicate_of", "?"),
                        r.get("reason", ""),
                    )

        if not drop_indices:
            return articles

        kept = [a for i, a in enumerate(articles) if i not in drop_indices]

        # Debug: log final outcome
        dbg.debug("OUTCOME: Dropped %d article(s)", len(drop_indices))
        dbg.debug("KEPT ARTICLES:")
        for i, a in enumerate(kept):
            dbg.debug("  [%d] %s", i, a.get("title", ""))
        dbg.debug("=" * 70)

        return kept

    except Exception as exc:
        logger.warning("  Dedup failed for %s: %s", category, exc)
        dbg.debug("DEDUP FAILED for %s: %s", category, exc)
        dbg.debug("=" * 70)
        return articles


def _rewrite_category(
    articles: List[dict],
    client,
    model: str,
    rewrite_cfg: dict | None = None,
) -> List[dict]:
    """Rewrite only low-quality summaries and keep high-quality source summaries."""
    if not articles:
        return []

    rewrite_cfg = rewrite_cfg or {}

    min_chars = int(rewrite_cfg.get("direct_summary_min_chars", 80))
    min_words = int(rewrite_cfg.get("direct_summary_min_words", 12))

    to_rewrite = []
    to_rewrite_index_map = []

    for i, article in enumerate(articles):
        source_summary = (article.get("description") or "").strip()
        if _is_high_quality_source_summary(source_summary, min_chars=min_chars, min_words=min_words):
            article["rewritten_headline"] = article["title"]
            article["rewritten_summary"] = source_summary
        else:
            to_rewrite.append(article)
            to_rewrite_index_map.append(i)

    if not to_rewrite:
        return articles

    logger.info(
        "Using source summaries for %d/%d; AI rewriting %d article(s).",
        len(articles) - len(to_rewrite),
        len(articles),
        len(to_rewrite),
    )

    prompt = _REWRITE_PROMPT + _format_articles(to_rewrite)

    try:
        raw = _call(client, model, prompt, max_tokens=8192)
        result = _extract_json(raw)

        if not isinstance(result, list):
            result = result.get("articles", []) if isinstance(result, dict) else []

        rewritten_map = {}
        for a in result:
            if isinstance(a, dict) and "index" in a:
                rewritten_map[a["index"]] = a

        for local_idx, article in enumerate(to_rewrite):
            original_idx = to_rewrite_index_map[local_idx]
            if local_idx in rewritten_map:
                r = rewritten_map[local_idx]
                articles[original_idx]["rewritten_headline"] = r.get("headline", article["title"])
                articles[original_idx]["rewritten_summary"] = r.get("summary", article.get("description", ""))
            else:
                articles[original_idx]["rewritten_headline"] = article["title"]
                articles[original_idx]["rewritten_summary"] = article.get("description", "")

        return articles

    except Exception as exc:
        logger.error("Rewrite failed for category: %s", exc)
        for article in to_rewrite:
            article["rewritten_headline"] = article["title"]
            article["rewritten_summary"] = article.get("description", "")
        return articles


def rewrite_all_categories(
    selected: Dict[str, List[dict]],
    reserves: Dict[str, List[dict]],
    config: dict,
    verbose: bool = False,
) -> Dict[str, List[dict]]:
    """Per-category AI dedup (single pass) then rewrite.

    Args:
        selected: top articles per category from greedy_select
        reserves: unused (kept for API compatibility)
        config: pipeline config dict
        verbose: if True, write debug_dedup.log to preview/
    """
    dedup_model = config.get("scoring", {}).get("dedup_model", "openai/gpt-oss-120b")
    rewrite_model = config.get("scoring", {}).get("rewrite_model", "openai/gpt-oss-120b")
    rewrite_cfg = config.get("scoring", {}).get("rewrite_strategy", {})
    dedup_client = _get_rewrite_client(dedup_model)
    rewrite_client = _get_rewrite_client(rewrite_model)

    # Initialize debug log file only when --verbose
    if verbose:
        _init_dedup_debug_log()

    results: Dict[str, List[dict]] = {}
    categories = list(selected.keys())

    for cat_idx, category in enumerate(categories):
        articles = list(selected[category])

        if not articles:
            results[category] = []
            continue

        # ── Single AI dedup pass ──
        logger.info("  %s AI dedup (%d articles)", category, len(articles))
        articles = _dedup_category(articles, dedup_client, dedup_model, category=category)
        logger.info("  %s after dedup: %d articles", category, len(articles))

        logger.info("Rate-limit buffer: waiting 15s before rewrite...")
        time.sleep(15)

        # ── Rewrite ──
        logger.info("Rewriting %s (%d articles)", category, len(articles))
        results[category] = _rewrite_category(
            articles,
            rewrite_client,
            rewrite_model,
            rewrite_cfg if isinstance(rewrite_cfg, dict) else {},
        )

        # Rate-limit buffer between categories
        if cat_idx < len(categories) - 1:
            logger.info("Rate-limit buffer: waiting 15s before next category...")
            time.sleep(15)

    return results
