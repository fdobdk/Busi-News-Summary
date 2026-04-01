"""filters.py — Pre-scoring article filters and debug utilities.

Contains the finance gate (keyword-based relevance filter that discards
off-topic articles before AI scoring) and Private Credit debug logging
for analyzing keyword matches vs AI category assignments.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Finance gate ─────────────────────────────────────────────────────────
# Universal relevance check — NOT a category filter.  Discards articles
# with zero finance-related terms (weather, sports, politics, etc.)
# Applied before AI scoring to reduce the pool cheaply.

_FINANCE_GATE_WORDS = {
    "deal", "acquire", "fund", "raise", "ipo", "listing", "invest",
    "lend", "loan", "credit", "startup", "merger", "buyout", "stake",
    "billion", "million", "venture", "equity", "debt", "default",
    "exit", "portfolio",
}


def finance_gate(articles: list) -> list:
    """Keep only articles whose title or description contains at least one finance term."""
    kept = []
    for article in articles:
        text = (
            (article.get("title") or "") + " " + (article.get("description") or "")
        ).lower()
        if any(word in text for word in _FINANCE_GATE_WORDS):
            kept.append(article)
    return kept


# ── Debug logging ────────────────────────────────────────────────────────
# Saves JSON debug output for analyzing Private Credit keyword matches
# vs AI category assignments. Used with the --debug CLI flag.
# Output goes to debug/pc_debug_<timestamp>.json.

_PC_DEBUG_KEYWORDS = re.compile(
    r"private credit|direct lend|BDC|CLO|credit fund|private debt|leveraged loan|"
    r"mezzanine|unitranche|loan default|credit spread|redemption|BCRED|HLEND|"
    r"Owl Rock|Ares Capital|Apollo Debt|Blue Owl|debt fund",
    re.IGNORECASE,
)


def save_debug_log(articles: list, scored: list) -> str:
    """Save a JSON debug file showing PC keyword matches vs AI assignments."""
    # Find articles that matched PC keywords BEFORE scoring
    pc_keyword_matches = []
    for a in articles:
        text = (a.get("title") or "") + " " + (a.get("description") or "")
        matches = _PC_DEBUG_KEYWORDS.findall(text)
        if matches:
            pc_keyword_matches.append({
                "title": a.get("title", ""),
                "source": a.get("source", ""),
                "url": a.get("url", ""),
                "pc_keywords_found": list(set(matches)),
            })

    # After scoring, find what happened to those articles
    scored_by_url = {a.get("url"): a for a in scored}
    debug_entries = []
    for match in pc_keyword_matches:
        scored_article = scored_by_url.get(match["url"], {})
        debug_entries.append({
            "title": match["title"],
            "source": match["source"],
            "pc_keywords_found": match["pc_keywords_found"],
            "ai_category": scored_article.get("section", "NOT_SCORED"),
            "ai_score": scored_article.get("score", 0),
            "url": match["url"],
        })

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"debug/pc_debug_{timestamp}.json"
    Path("debug").mkdir(exist_ok=True)
    Path(path).write_text(json.dumps({
        "total_articles_in_pool": len(articles),
        "articles_with_pc_keywords": len(pc_keyword_matches),
        "debug_entries": debug_entries,
    }, indent=2), encoding="utf-8")
    return path
