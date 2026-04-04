"""dedup.py — Fuzzy headline deduplication with industry-term stripping.

Uses smart local matching: strips industry stopwords to avoid false
positives, then falls back to standard fuzzy matching. When two articles
are duplicates, keeps the one from the more reputable source.
"""

import logging
import re
from typing import List

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

INDUSTRY_STOPWORDS = {
    'private', 'credit', 'equity', 'venture', 'capital', 'fund', 'market',
    'investment', 'financial', 'global', 'new', 'report', 'says', 'could',
    'may', 'will', 'firm', 'company', 'group', 'partners', 'management',
    'investor', 'investors', 'funding', 'portfolio', 'growth', 'billion',
    'million', 'year', 'quarter', 'annual', 'deal', 'deals', 'markets',
}

# Source reputation tiers (lower rank = more reputable)
_TIER_1 = {
    "bloomberg.com", "wsj.com", "ft.com", "reuters.com", "cnbc.com", "barrons.com",
}
_TIER_2 = {
    "techcrunch.com", "privateequitywire.co.uk", "altassets.net",
    "financeasia.com", "asia.nikkei.com", "alleywatch.com",
    "scmp.com", "asiafinancial.com", "dealroom.net",
}


def _source_rank(article: dict) -> int:
    """Return reputation rank (lower = more reputable)."""
    domain = article.get("source_domain", "")
    if domain in _TIER_1:
        return 0
    if domain in _TIER_2:
        return 1
    return 2


def extract_key_terms(title: str) -> set:
    """Extract meaningful terms from a headline, stripping industry boilerplate."""
    words = re.findall(r'\b[A-Za-z]+\b', title.lower())
    return set(w for w in words if w not in INDUSTRY_STOPWORDS and len(w) > 2)


def extract_entities_and_numbers(title: str) -> tuple[set, set]:
    """Extract proper nouns and dollar/percentage figures from a headline."""
    proper_nouns = set(re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', title))
    numbers = set(re.findall(r'\$?[\d,.]+\s*(?:billion|million|trillion|B|M|T|%)', title, re.IGNORECASE))
    return proper_nouns, numbers


def is_duplicate(title_a: str, title_b: str) -> bool:
    """Check if two headlines are about the same story."""
    # Method 1: key term overlap
    terms_a = extract_key_terms(title_a)
    terms_b = extract_key_terms(title_b)

    if terms_a and terms_b:
        overlap = len(terms_a & terms_b)
        smaller = min(len(terms_a), len(terms_b))
        if smaller > 0 and overlap / smaller >= 0.6:
            return True

    # Method 2: fuzzy match
    if fuzz.token_sort_ratio(title_a, title_b) > 75:
        return True

    # # Method 3: same entity + same number = same story
    # nouns_a, nums_a = extract_entities_and_numbers(title_a)
    # nouns_b, nums_b = extract_entities_and_numbers(title_b)
    # if nouns_a & nouns_b and nums_a & nums_b:
    #     return True

    return False


def deduplicate(articles: List[dict]) -> List[dict]:
    """Deduplicate by headline similarity, keeping the more reputable source."""
    if not articles:
        return []

    kept: List[dict] = []
    for article in articles:
        duplicate_found = False
        for i, existing in enumerate(kept):
            if is_duplicate(article["title"], existing["title"]):
                # Replace if the incoming article is from a more reputable source
                if _source_rank(article) < _source_rank(existing):
                    kept[i] = article
                duplicate_found = True
                break
        if not duplicate_found:
            kept.append(article)

    logger.info("Dedup: %d → %d articles", len(articles), len(kept))
    return kept
