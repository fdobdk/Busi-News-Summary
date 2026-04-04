"""selection.py — Greedy cross-category article selection.

Single-pass greedy selection: for each category (in fixed order), pick
the top-scored articles that are NOT duplicates of anything already
selected in a previous category. Also returns reserve candidates per
category for backfill after AI dedup.
"""

import logging
from typing import Dict, List, Tuple

from dedup import is_duplicate

logger = logging.getLogger(__name__)

CATEGORY_ORDER = ["PE", "VC", "PC", "ASIA_IPO", "US_IPO"]

# How many reserve articles to keep per category for backfill
RESERVE_SIZE = 10


def greedy_select(
    scored_articles: List[dict], final_cap: int = 5,
) -> Tuple[Dict[str, List[dict]], Dict[str, List[dict]]]:
    """Pick top unique articles per category with cross-category dedup.

    Returns (selected, reserves) where:
      - selected: top `final_cap` unique articles per category
      - reserves: next-best candidates per category (for backfill after AI dedup)
    """
    selected_global: List[dict] = []  # global list across ALL categories
    results: Dict[str, List[dict]] = {}
    reserves: Dict[str, List[dict]] = {}

    for category in CATEGORY_ORDER:
        candidates = sorted(
            [a for a in scored_articles if a.get("category") == category],
            key=lambda x: x.get("score", 0),
            reverse=True,
        )
        category_picks: List[dict] = []
        category_reserves: List[dict] = []

        for article in candidates:
            # Check against ALL already-selected articles across all categories
            is_dupe = False
            for picked in selected_global:
                if is_duplicate(article["title"], picked["title"]):
                    is_dupe = True
                    break

            if is_dupe:
                continue

            if len(category_picks) < final_cap:
                category_picks.append(article)
                selected_global.append(article)
            elif len(category_reserves) < RESERVE_SIZE:
                category_reserves.append(article)

        results[category] = category_picks
        reserves[category] = category_reserves

    total = sum(len(v) for v in results.values())
    logger.info("Selected %d articles across %d categories", total, len(results))
    return results, reserves
