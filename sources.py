"""
sources.py — Fetch articles from ALL configured sources into a single flat pool.

Every RSS feed, Google News query, and broad RSS source dumps into one list.
No category assignment happens here — that's the AI's job.
URL-level dedup removes exact duplicates across sources.
"""

import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

import feedparser
import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "VCPEDigest/1.0 (automated financial news aggregator; "
    "contact: your@email.com)"
)

GOOGLE_NEWS_RSS_BASE = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)


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


def _entry_to_article(entry, source_name: str) -> dict:
    """Convert a feedparser entry to a normalised article dict."""
    content = ""
    if entry.get("content"):
        content = entry["content"][0].get("value", "")

    pub_date = _parse_entry_date(entry)
    return {
        "title": _strip_html(entry.get("title", "")),
        "url": entry.get("link", "").strip(),
        "description": _strip_html(entry.get("summary", entry.get("description", ""))),
        "source": source_name,
        "published_at": pub_date.isoformat() if pub_date else None,
        "content": content,
        "_source_type": "rss",
    }


def _download_feed(url: str, label: str) -> Tuple[bytes | None, int | None, str]:
    """
    Download a feed URL via requests (enforces a timeout).
    Returns (content_bytes, http_status, error_string).
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        return resp.content, resp.status_code, ""
    except requests.Timeout:
        logger.warning("Feed timed out (>15s) [%s]", label)
        return None, None, "timeout"
    except Exception as exc:
        logger.warning("Feed download failed [%s]: %s", label, exc)
        return None, None, f"error:{exc}"


def _fetch_google_news(
    query: str, cutoff: datetime,
) -> Tuple[List[dict], str]:
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


def _fetch_rss(
    feed_url: str, source_name: str, cutoff: datetime,
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
            article = _entry_to_article(entry, source_name)
            if article["url"]:
                articles.append(article)

        if not articles and not feed.entries:
            logger.info("No entries returned from [%s].", source_name)
            return [], "empty"

        return articles, "ok"

    except Exception as exc:
        logger.warning("RSS parse failed [%s]: %s", source_name, exc)
        return [], f"error:{exc}"


def fetch_all_articles(config: dict, cutoff_hours: int = 24) -> Tuple[List[dict], List[dict]]:
    """
    Fetch from ALL sources into a single flat pool.  Returns (articles, source_log).

    Sources fetched:
      - Broad RSS feeds (e.g. Bloomberg Markets)
      - Per-category: Google News queries + dedicated RSS feeds

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

    # ── Broad RSS sources ──
    for bsource in settings.get("broad_rss_sources", []):
        fetched, status = _fetch_rss(bsource["url"], bsource["name"], cutoff)
        added = _add(fetched)
        source_log.append({
            "source": bsource["name"], "section": "Broad",
            "count": added, "status": status,
            "paywalled": bsource.get("paywalled", False),
        })
        logger.info("Broad RSS [%s] → %d articles", bsource["name"], added)

    # ── Broad Google News queries ──
    for gnews_query in settings.get("broad_google_news_queries", []):
        fetched, status = _fetch_google_news(gnews_query, cutoff)
        added = _add(fetched)
        source_log.append({
            "source": f"GNews: {gnews_query[:38]}",
            "section": "Broad", "count": added,
            "status": status, "paywalled": True,
        })
        logger.info("Broad GNews [%s...] → %d articles", gnews_query[:40], added)

    # ── Per-category: Google News queries + dedicated RSS feeds ──
    for category_key, category_cfg in config["categories"].items():
        label = category_cfg.get("display_name", category_key)

        for gnews_query in (category_cfg.get("google_news_queries") or []):
            fetched, status = _fetch_google_news(gnews_query, cutoff)
            added = _add(fetched)
            source_log.append({
                "source": f"GNews: {gnews_query[:38]}",
                "section": label, "count": added,
                "status": status, "paywalled": True,
            })

        for source in (category_cfg.get("rss_sources") or []):
            feed_url = source.get("url", "")
            if not feed_url:
                continue
            fetched, status = _fetch_rss(feed_url, source["name"], cutoff)
            added = _add(fetched)
            source_log.append({
                "source": source["name"], "section": label,
                "count": added, "status": status,
                "paywalled": source.get("paywalled", False),
            })

    logger.info("Total pool: %d unique articles from all sources", len(all_articles))
    return all_articles, source_log
