"""sources.py — Fetch articles from ALL configured sources into a single flat pool.

Sources: Google News RSS, Brave Search News API, specialty RSS feeds,
and broad RSS feeds (Bloomberg fetched ONCE, not per-category).
URL-level dedup removes exact duplicates across sources.
"""

import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from urllib.parse import urlparse

import feedparser
import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "VCPEDigest/2.0 (automated financial news aggregator)"
)

GOOGLE_NEWS_RSS_BASE = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)

BRAVE_NEWS_ENDPOINT = "https://api.search.brave.com/res/v1/news/search"


# ── Helpers ──────────────────────────────────────────────────────────────

def _parse_entry_date(entry) -> datetime | None:
    """Extract a timezone-aware datetime from a feedparser entry."""
    for attr in ("published_parsed", "updated_parsed"):
        value = getattr(entry, attr, None)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&middot;", "\u00b7").replace("&rarr;", "\u2192")
    return re.sub(r"\s+", " ", text).strip()


def _extract_domain(url: str) -> str:
    """Extract the bare domain from a URL (e.g. 'www.bloomberg.com' → 'bloomberg.com')."""
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _entry_to_article(entry, source_name: str, source_category: str | None = None) -> dict:
    """Convert a feedparser entry to a normalised article dict."""
    pub_date = _parse_entry_date(entry)
    url = entry.get("link", "").strip()
    return {
        "title": _strip_html(entry.get("title", "")),
        "description": _strip_html(entry.get("summary", entry.get("description", ""))),
        "url": url,
        "source_name": source_name,
        "source_domain": _extract_domain(url),
        "published_date": pub_date.isoformat() if pub_date else None,
        "source_category": source_category,
        "_source_type": "rss",
    }


# ── Feed downloading ────────────────────────────────────────────────────

def _download_feed(url: str, label: str) -> Tuple[bytes | None, int | None, str]:
    """Download a feed URL via requests (enforces a timeout)."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        return resp.content, resp.status_code, ""
    except requests.Timeout:
        logger.warning("Feed timed out (>15s) [%s]", label)
        return None, None, "timeout"
    except Exception as exc:
        logger.warning("Feed download failed [%s]: %s", label, exc)
        return None, None, f"error:{exc}"


# ── Google News ─────────────────────────────────────────────────────────

def _fetch_google_news(query: str, cutoff: datetime) -> Tuple[List[dict], str]:
    """Fetch articles from Google News RSS for a query."""
    feed_url = GOOGLE_NEWS_RSS_BASE.format(query=urllib.parse.quote(query))
    content, status_code, err = _download_feed(feed_url, f"GNews:{query[:40]}")
    if content is None:
        return [], err

    if status_code and status_code >= 400:
        logger.warning("Google News RSS failed [%s...] — HTTP %s", query[:50], status_code)
        return [], f"http_{status_code}"

    try:
        feed = feedparser.parse(content)
        articles = []
        for entry in feed.entries:
            pub_date = _parse_entry_date(entry)
            if pub_date and pub_date < cutoff:
                continue

            source_obj = getattr(entry, "source", None)
            if source_obj and hasattr(source_obj, "get"):
                publisher = source_obj.get("title") or "Google News"
            else:
                publisher = "Google News"

            article = _entry_to_article(entry, publisher)
            article["_source_type"] = "google_news"
            if article["url"]:
                articles.append(article)

        return articles, ("ok" if articles else "empty")
    except Exception as exc:
        logger.warning("Google News RSS parse failed [%s...]: %s", query[:50], exc)
        return [], f"error:{exc}"


# ── RSS feeds ───────────────────────────────────────────────────────────

def _fetch_rss(
    feed_url: str, source_name: str, cutoff: datetime,
    source_category: str | None = None,
) -> Tuple[List[dict], str]:
    """Fetch and parse a single RSS feed."""
    content, status_code, err = _download_feed(feed_url, source_name)
    if content is None:
        return [], err

    if status_code and status_code >= 400:
        logger.warning("SKIPPED [%s] — HTTP %s.", source_name, status_code)
        return [], f"http_{status_code}"

    try:
        feed = feedparser.parse(content)
        articles = []
        for entry in feed.entries:
            pub_date = _parse_entry_date(entry)
            if pub_date and pub_date < cutoff:
                continue
            article = _entry_to_article(entry, source_name, source_category)
            if article["url"]:
                articles.append(article)

        if not articles and not feed.entries:
            return [], "empty"

        return articles, "ok"
    except Exception as exc:
        logger.warning("RSS parse failed [%s]: %s", source_name, exc)
        return [], f"error:{exc}"


# ── Brave Search News API ───────────────────────────────────────────────

def _fetch_brave_news(query: str) -> Tuple[List[dict], str]:
    """Fetch articles from Brave Search News API for a query."""
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        return [], "no_api_key"

    try:
        resp = requests.get(
            BRAVE_NEWS_ENDPOINT,
            params={"q": query, "count": 10},
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            logger.warning("Brave Search failed [%s...] — HTTP %s", query[:40], resp.status_code)
            return [], f"http_{resp.status_code}"

        data = resp.json()
        results = data.get("results", [])
        if not results:
            return [], "empty"

        articles = []
        for r in results:
            url = r.get("url", "")
            meta = r.get("meta_url", {})
            hostname = meta.get("hostname", "") if isinstance(meta, dict) else ""
            if not hostname:
                hostname = _extract_domain(url) or "Brave Search"

            articles.append({
                "title": _strip_html(r.get("title", "")),
                "description": _strip_html(r.get("description", "")),
                "url": url,
                "source_name": hostname,
                "source_domain": _extract_domain(url),
                "published_date": r.get("page_age") or None,
                "source_category": None,
                "_source_type": "brave",
            })

        return articles, "ok"
    except Exception as exc:
        logger.warning("Brave Search failed [%s...]: %s", query[:40], exc)
        return [], f"error:{exc}"


# ── Main entry point ────────────────────────────────────────────────────

def fetch_all_articles(config: dict, cutoff_hours: int = 24) -> Tuple[List[dict], List[dict]]:
    """
    Fetch from ALL sources into a single flat pool.  Returns (articles, source_log).

    Sources fetched (in order):
      1. Broad RSS feeds (Bloomberg Markets fetched ONCE here)
      2. Broad Google News queries
      3. Per-category: Google News queries + specialty RSS feeds (tagged with source_category)
      4. Per-category: Brave Search News API (one query per category)

    URL-level dedup ensures each article appears exactly once.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)
    settings = config.get("settings", {})

    source_log: List[dict] = []
    all_articles: List[dict] = []
    seen_urls: set = set()

    def _add(articles: List[dict]) -> int:
        """Add articles to the pool, skipping URL duplicates. Returns count added."""
        added = 0
        for a in articles:
            if a["url"] and a["url"] not in seen_urls:
                seen_urls.add(a["url"])
                all_articles.append(a)
                added += 1
        return added

    # ── 1. Broad RSS sources (Bloomberg fetched ONCE here) ──
    for bsource in settings.get("broad_rss_sources", []):
        fetched, status = _fetch_rss(bsource["url"], bsource["name"], cutoff)
        added = _add(fetched)
        source_log.append({
            "source": bsource["name"], "section": "Broad",
            "count": added, "status": status,
        })
        logger.info("Broad RSS [%s] → %d articles", bsource["name"], added)

    # ── 2. Broad Google News queries ──
    for gnews_query in settings.get("broad_google_news_queries", []):
        fetched, status = _fetch_google_news(gnews_query, cutoff)
        added = _add(fetched)
        source_log.append({
            "source": f"GNews: {gnews_query[:38]}",
            "section": "Broad", "count": added, "status": status,
        })
        logger.info("Broad GNews [%s...] → %d articles", gnews_query[:40], added)

    # ── 3. Per-category: Google News queries + specialty RSS feeds ──
    for category_key, category_cfg in config["categories"].items():
        label = category_cfg.get("display_name", category_key)

        for gnews_query in (category_cfg.get("google_news_queries") or []):
            fetched, status = _fetch_google_news(gnews_query, cutoff)
            added = _add(fetched)
            source_log.append({
                "source": f"GNews: {gnews_query[:38]}",
                "section": label, "count": added, "status": status,
            })

        for source in (category_cfg.get("rss_sources") or []):
            feed_url = source.get("url", "")
            if not feed_url:
                continue
            fetched, status = _fetch_rss(
                feed_url, source["name"], cutoff,
                source_category=category_key,
            )
            added = _add(fetched)
            source_log.append({
                "source": source["name"], "section": label,
                "count": added, "status": status,
            })

    # ── 4. Per-category: Brave Search News API ──
    for category_key, category_cfg in config["categories"].items():
        brave_query = category_cfg.get("brave_query")
        if not brave_query:
            continue
        label = category_cfg.get("display_name", category_key)
        fetched, status = _fetch_brave_news(brave_query)
        added = _add(fetched)
        source_log.append({
            "source": f"Brave: {brave_query[:36]}",
            "section": label, "count": added, "status": status,
        })
        if added > 0:
            logger.info("Brave [%s] → %d articles", category_key, added)

    logger.info("Total pool: %d unique articles from all sources", len(all_articles))
    return all_articles, source_log
