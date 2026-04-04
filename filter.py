"""filter.py — Finance-gate filter.

A minimal universal relevance gate. NOT a category filter — just removes
completely off-topic articles (weather, sports, politics that leaked in
from broad feeds). All sources are treated equally.
"""

import logging
from typing import List

logger = logging.getLogger(__name__)

FINANCE_KEYWORDS = {
     # Deal activity
    "deal", "acquire", "acquisition", "merger", "buyout", "takeover",
    "stake", "bid", "divest", "divestiture", "spinoff", "carveout",
    
    # Funding & capital
    "fund", "raise", "raised", "raises", "financing", "refinancing",
    "capital", "billion", "million", "trillion",
    
    # PE/VC specific
    "equity", "venture", "startup", "seed", "series", "portfolio",
    "buyout", "lbo", "investor", "investors", "backed",
    
    # Credit & debt
    "credit", "debt", "loan", "lend", "lending", "default",
    "redemption", "redemptions", "clo", "bdc", "mezzanine",
    
    # IPO & public markets
    "ipo", "listing", "offering", "shares", "underwriter",
    "prospectus", "filing", "filed", "s-1", "spac", "valuation",
    
    # Common financial verbs/nouns in headlines
    "closes", "closed", "secures", "secured", "backs", "backed",
    "targets", "sells", "buys", "invests", "exits", "launches",
    "hires", "appoints", "taps",
    
    # Financial structure terms
    "restructuring", "bankruptcy", "liquidation", "recapitalization",
    "leveraged", "syndicated", "tranche", "unitranche",
    
    # Industry terms that appear in relevant headlines
    "fintech", "proptech", "healthtech", "biotech", "saas",
    "ai", "semiconductor",
}


def finance_gate(articles: List[dict]) -> List[dict]:
    """Keep only articles that are plausibly finance-related.

    Checks title + description for at least one finance keyword.
    All sources are treated equally — no bypasses.
    """
    passed = []
    for article in articles:
        text = f"{article.get('title', '')} {article.get('description', '')}".lower()
        if any(kw in text for kw in FINANCE_KEYWORDS):
            passed.append(article)

    logger.info("Finance gate: %d → %d articles", len(articles), len(passed))
    return passed
