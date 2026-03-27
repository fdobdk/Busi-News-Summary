"""dedup.py — Remove near-duplicate articles using rapidfuzz fuzzy string matching."""

import logging
from typing import List

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 75


def _article_text(article: dict) -> str:
    return f"{article.get('title', '')} {article.get('description', '')}".lower()


def deduplicate(articles: List[dict]) -> List[dict]:
    """
    Cluster near-duplicate articles and keep one per cluster.
    Within each cluster the most recently published article is kept,
    falling back to list order when dates are unavailable.
    """
    if len(articles) <= 1:
        return articles

    n = len(articles)
    texts = [_article_text(a) for a in articles]
    absorbed = [False] * n
    kept: List[dict] = []

    for i in range(n):
        if absorbed[i]:
            continue
        cluster = [i]
        for j in range(i + 1, n):
            if not absorbed[j]:
                if fuzz.token_set_ratio(texts[i], texts[j]) >= SIMILARITY_THRESHOLD:
                    cluster.append(j)
                    absorbed[j] = True
        absorbed[i] = True

        # Keep the most recently published article in this cluster
        best = max(cluster, key=lambda idx: articles[idx].get("published_at") or "")
        kept.append(articles[best])

    logger.info("Deduplication: %d → %d articles", n, len(kept))
    return kept
