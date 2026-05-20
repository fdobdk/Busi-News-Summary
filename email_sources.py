"""email_sources.py - Fetch article candidates from newsletter emails."""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.policy import default
from email.utils import parsedate_to_datetime
from typing import List, Tuple
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
import requests

logger = logging.getLogger(__name__)

_SKIP_LINK_PATTERNS = (
    "unsubscribe",
    "preference",
    "privacy",
    "view in browser",
    "mailto:",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
)

_EXTERNAL_SECTION_NAMES = ("catch up quick", "side letters")
_PARTNER_SECTION_PREFIX = "a message from"

_GENERIC_LINK_TITLES = {
    "read more",
    "see why",
    "request a demo today.",
    "request a free trial",
    "register now to secure your spot.",
    "view more charts",
    "download the research",
    "sign up here",
}

_PROMO_TITLE_PATTERNS = (
    "request a ",
    "don't miss",
    "webinar",
    "benchmark",
    "benchmarks",
    "free trial",
)

_BLOCK_EXCLUDE_MARKERS = (
    "a message from",
    "request a demo today",
    "request a free trial",
    "unlock what’s possible",
    "unlock what's possible",
    "smart reads that caught our eye",
    "the daily benchmark",
    "sign up here",
)


def _mask_email(value: str) -> str:
    if "@" not in value:
        return value[:2] + "***" if value else ""
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        masked_name = name[:1] + "***"
    else:
        masked_name = name[:2] + "***"
    return f"{masked_name}@{domain}"


def _extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _clean_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _looks_like_article_link(url: str, anchor_text: str) -> bool:
    if not url or not anchor_text:
        return False

    lower_url = url.lower()
    lower_text = anchor_text.lower()
    if any(pattern in lower_url or pattern in lower_text for pattern in _SKIP_LINK_PATTERNS):
        return False

    if len(anchor_text.strip()) < 20:
        return False

    words = re.findall(r"[A-Za-z0-9]+", anchor_text)
    return len(words) >= 4


def _unwrap_tracking_url(url: str) -> str:
    """Unwrap common email-tracking redirects when target URL is embedded in querystring."""
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        for key in ("url", "u", "redirect", "target"):
            values = query.get(key)
            if values and values[0].startswith(("http://", "https://")):
                return values[0]
        return url
    except Exception:
        return url


def _resolve_pitchbook_tracking_url(
    url: str,
    cache: dict,
    debug: bool = False,
) -> str:
    """Resolve PitchBook tracking links to final destination URLs."""
    unwrapped = _unwrap_tracking_url(url)
    parsed = urlparse(unwrapped)
    if "news.pitchbook.com" not in parsed.netloc:
        return unwrapped
    if "/ls/click" not in parsed.path:
        return unwrapped

    if unwrapped in cache:
        return cache[unwrapped]

    final_url = unwrapped
    try:
        resp = requests.get(
            unwrapped,
            allow_redirects=True,
            timeout=10,
            headers={"User-Agent": "VCPEDigest/2.0 (PitchBook parser)"},
        )
        if resp.url:
            final_url = resp.url
    except Exception as exc:
        if debug:
            logger.info("PitchBook debug: tracking URL resolve failed: %s", exc)

    cache[unwrapped] = final_url
    return final_url


def _extract_html_body(message: email.message.EmailMessage) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        if message.get_content_type() == "text/html":
            payload = message.get_payload(decode=True)
            if payload:
                charset = message.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    return ""


def _extract_text_body(message: email.message.EmailMessage) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        if message.get_content_type() == "text/plain":
            payload = message.get_payload(decode=True)
            if payload:
                charset = message.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    return ""


def _extract_articles_from_html(html: str, source_name: str, published_date: str | None) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    articles: List[dict] = []

    for anchor in soup.find_all("a", href=True):
        raw_url = anchor.get("href", "").strip()
        url = _unwrap_tracking_url(raw_url)
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not _looks_like_article_link(url, title):
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Pull a short description from nearby text if available.
        parent_text = " ".join(anchor.parent.get_text(" ", strip=True).split())
        description = parent_text if parent_text and parent_text != title else ""
        if len(description) > 600:
            description = description[:600].rstrip() + "..."

        articles.append(
            {
                "title": title,
                "description": description,
                "url": url,
                "source_name": source_name,
                "source_domain": _extract_domain(url),
                "published_date": published_date,
                "source_category": None,
                "_source_type": "pitchbook_email",
            }
        )
    return articles


def _extract_title_from_context(anchor) -> str:
    """Infer article title from surrounding content in PitchBook bullet sections."""
    anchor_text = _clean_text(anchor.get_text(" ", strip=True))
    anchor_lower = anchor_text.lower()

    if anchor_lower and anchor_lower not in _GENERIC_LINK_TITLES and len(anchor_text) >= 18:
        return anchor_text

    container = anchor.find_parent("td")
    if not container:
        return anchor_text

    bold_candidates = []
    for bold in container.find_all("b"):
        text = _clean_text(bold.get_text(" ", strip=True))
        if not text:
            continue
        lowered = text.lower()
        if lowered in _GENERIC_LINK_TITLES:
            continue
        if "related article" in lowered:
            continue
        if len(text) < 18:
            continue
        bold_candidates.append(text)

    if bold_candidates:
        return max(bold_candidates, key=len)

    return anchor_text


def _nearest_section_header_text(node) -> str:
    """Find nearest previous x-news-group-header text for a node."""
    for prev_tr in node.find_all_previous("tr"):
        header_table = prev_tr.find("table", attrs={"lang": "x-news-group-header"})
        if header_table is not None:
            return _clean_text(header_table.get_text(" ", strip=True)).lower()
    return ""


def _extract_summary_from_context(anchor, title: str) -> str:
    container = anchor.find_parent("td")
    if not container:
        return ""
    text = _clean_text(container.get_text(" ", strip=True))
    if not text:
        return ""
    summary = text.replace(title, "").strip(" .-")
    if len(summary) > 500:
        summary = summary[:500].rstrip() + "..."
    return summary


def _pick_best_link_from_group(group, resolve_cache: dict, debug: bool = False) -> str:
    """Pick best candidate URL from a PitchBook x-news-group block."""
    # First choice: content-area-bottom links (usually the canonical story CTA).
    bottom_anchors = group.select("table[lang='x-content-area-bottom'] a[href]")
    for anchor in bottom_anchors:
        href = _clean_text(anchor.get("href", ""))
        if href:
            return _resolve_pitchbook_tracking_url(href, resolve_cache, debug=debug)

    # Prefer dedicated CTA links first.
    btn_anchors = group.select("table[lang='x-content-btn'] a[href]")
    for anchor in btn_anchors:
        href = _clean_text(anchor.get("href", ""))
        if href:
            return _resolve_pitchbook_tracking_url(href, resolve_cache, debug=debug)

    # Then use prominent image links.
    image_anchors = group.select("table[lang='x-image'] a[href]")
    for anchor in image_anchors:
        href = _clean_text(anchor.get("href", ""))
        if href:
            return _resolve_pitchbook_tracking_url(href, resolve_cache, debug=debug)

    # Finally, fall back to any non-generic link in the block.
    candidates = []
    for anchor in group.find_all("a", href=True):
            href = _clean_text(anchor.get("href", ""))
            if not href:
                continue
            text = _clean_text(anchor.get_text(" ", strip=True)).lower()
            if text in _GENERIC_LINK_TITLES:
                continue
            if "related article" in text:
                continue
            candidates.append((href, len(text)))

    if not candidates:
        return ""

    # Prefer richer anchor text if available.
    candidates.sort(key=lambda item: item[1], reverse=True)
    best = candidates[0][0]
    return _resolve_pitchbook_tracking_url(best, resolve_cache, debug=debug)


def _extract_main_pitchbook_sections(
    soup: BeautifulSoup,
    source_name: str,
    published_date: str | None,
    main_story_limit: int = 2,
    debug: bool = False,
) -> List[dict]:
    """Extract main PitchBook-authored stories from core newsletter sections."""
    all_articles: List[dict] = []
    seen_urls = set()
    resolve_cache: dict = {}

    groups = soup.find_all("table", attrs={"lang": "x-news-group"})
    if debug:
        logger.info("PitchBook debug: found %d x-news-group block(s)", len(groups))
    for group in groups:
        title_table = group.find("table", attrs={"lang": "x-news-group-title"})
        if title_table is None:
            continue
        title = _clean_text(title_table.get_text(" ", strip=True))
        section_header = _nearest_section_header_text(group)
        if debug:
            logger.info(
                "PitchBook debug main candidate: title='%s' | section='%s'",
                title[:120],
                section_header or "(none)",
            )
        if not title or len(title) < 20:
            if debug:
                logger.info("PitchBook debug skip: short/empty title")
            continue
        if any(p in title.lower() for p in _PROMO_TITLE_PATTERNS):
            if debug:
                logger.info("PitchBook debug skip: promo-pattern title")
            continue
        if section_header.startswith(_PARTNER_SECTION_PREFIX):
            if debug:
                logger.info("PitchBook debug skip: partner section")
            continue

        content_areas = group.find_all("table", attrs={"lang": "x-content-area"})
        block_text = _clean_text(" ".join(area.get_text(" ", strip=True) for area in content_areas))
        byline_text = _clean_text(
            " ".join(i.get_text(" ", strip=True) for area in content_areas for i in area.find_all("i"))
        )
        byline_lower = byline_text.lower()
        block_lower = block_text.lower()
        if any(marker in block_lower for marker in _BLOCK_EXCLUDE_MARKERS):
            if debug:
                logger.info("PitchBook debug skip: excluded marker in block text")
            continue
        has_pitchbook_byline = byline_lower.startswith("by ") and "pitchbook" in byline_lower
        has_substantive_content = (
            len(block_text) >= 120
        )
        if not (has_pitchbook_byline or has_substantive_content):
            if debug:
                logger.info(
                    "PitchBook debug skip: weak content/byline (byline='%s', len=%d)",
                    byline_text[:120],
                    len(block_text),
                )
            continue

        url = _pick_best_link_from_group(group, resolve_cache, debug=debug)
        if not url or url in seen_urls:
            if debug:
                logger.info("PitchBook debug skip: missing/duplicate URL")
            continue
        seen_urls.add(url)

        summary = block_text
        summary = summary.replace(title, "").strip(" .-")
        if len(summary) > 500:
            summary = summary[:500].rstrip() + "..."

        all_articles.append(
            {
                "title": title,
                "description": summary,
                "url": url,
                "source_name": source_name,
                "source_domain": _extract_domain(url),
                "published_date": published_date,
                "source_category": "pitchbook_main",
                "_source_type": "pitchbook_email",
            }
        )
        if len(all_articles) >= max(1, main_story_limit):
            break

    if debug:
        logger.info("PitchBook debug: extracted %d main-story article(s)", len(all_articles))

    return all_articles


def _extract_external_section_articles(
    soup: BeautifulSoup,
    source_name: str,
    published_date: str | None,
    debug: bool = False,
) -> List[dict]:
    """Extract article links from external sections ('Catch Up Quick', 'Side Letters')."""
    all_articles: List[dict] = []
    seen_urls = set()
    resolve_cache: dict = {}

    header_tables = soup.find_all("table", attrs={"lang": "x-news-group-header"})
    for header in header_tables:
        header_text = _clean_text(header.get_text(" ", strip=True)).lower()
        if not any(name in header_text for name in _EXTERNAL_SECTION_NAMES):
            continue

        tr = header.find_parent("tr")
        if tr is None:
            continue

        if debug:
            logger.info("PitchBook debug: parsing target section '%s'", header_text)

        for sibling_tr in tr.find_next_siblings("tr"):
            # Stop when next section header starts.
            if sibling_tr.find("table", attrs={"lang": "x-news-group-header"}) is not None:
                break

            for anchor in sibling_tr.find_all("a", href=True):
                raw_url = _clean_text(anchor.get("href", ""))
                if not raw_url:
                    continue

                title = _extract_title_from_context(anchor)
                if not title:
                    continue
                if title.lower() in _GENERIC_LINK_TITLES:
                    continue

                final_url = _resolve_pitchbook_tracking_url(raw_url, resolve_cache, debug=debug)
                if not final_url:
                    continue
                if final_url in seen_urls:
                    continue
                seen_urls.add(final_url)

                summary = _extract_summary_from_context(anchor, title)
                all_articles.append(
                    {
                        "title": title,
                        "description": summary,
                        "url": final_url,
                        "source_name": source_name,
                        "source_domain": _extract_domain(final_url),
                        "published_date": published_date,
                        "source_category": "pitchbook_external",
                        "_source_type": "pitchbook_email",
                    }
                )

    return all_articles


def _extract_articles_from_text(text: str, source_name: str, published_date: str | None) -> List[dict]:
    articles: List[dict] = []
    seen_urls = set()
    for match in re.finditer(r"https?://[^\s<>\"]+", text):
        url = _unwrap_tracking_url(match.group(0).rstrip(".,);"))
        if url in seen_urls or any(pattern in url.lower() for pattern in _SKIP_LINK_PATTERNS):
            continue
        seen_urls.add(url)
        articles.append(
            {
                "title": "PitchBook article",
                "description": "",
                "url": url,
                "source_name": source_name,
                "source_domain": _extract_domain(url),
                "published_date": published_date,
                "source_category": None,
                "_source_type": "pitchbook_email",
            }
        )
    return articles


def fetch_pitchbook_articles(
    config: dict,
    cutoff_hours: int = 24,
    debug: bool = False,
) -> Tuple[List[dict], str]:
    """Fetch candidate article links from recent PitchBook newsletter emails."""
    email_cfg = (config.get("settings", {}).get("pitchbook_email") or {})
    if not email_cfg.get("enabled", False):
        return [], "disabled"

    senders = email_cfg.get("senders") or []
    if not senders:
        return [], "no_senders"

    imap_host = email_cfg.get("imap_host") or os.getenv("IMAP_HOST", "imap.gmail.com")
    imap_user = os.getenv("IMAP_EMAIL") or os.getenv("GMAIL_ADDRESS", "")
    imap_password = os.getenv("IMAP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD", "")
    source_name = email_cfg.get("source_name", "PitchBook")
    raw_subject_keywords = email_cfg.get("subject_keywords")
    if raw_subject_keywords is None:
        subject_keywords: List[str] = []
    else:
        subject_keywords = [str(s).lower() for s in raw_subject_keywords if str(s).strip()]

    if not imap_user or not imap_password:
        return [], "missing_imap_credentials"

    since_date = (datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)).strftime("%d-%b-%Y")
    debug_samples_per_sender = int(email_cfg.get("debug_samples_per_sender", 5))

    all_articles: List[dict] = []
    seen_urls = set()
    try:
        with imaplib.IMAP4_SSL(imap_host) as mail:
            mail.login(imap_user, imap_password)
            mail.select("INBOX")
            if debug:
                logger.info(
                    "PitchBook debug: host=%s, user=%s, since=%s, senders=%d, subject_keywords=%s",
                    imap_host,
                    _mask_email(imap_user),
                    since_date,
                    len(senders),
                    subject_keywords,
                )

            for sender in senders:
                query = f'(SINCE "{since_date}" FROM "{sender}")'
                status, data = mail.search(None, query)
                if debug:
                    logger.info("PitchBook debug: sender=%s query=%s status=%s", sender, query, status)
                if status != "OK" or not data or not data[0]:
                    if debug:
                        logger.info("PitchBook debug: sender=%s matched_messages=0", sender)
                    continue

                msg_ids = data[0].split()
                if debug:
                    logger.info("PitchBook debug: sender=%s matched_messages=%d", sender, len(msg_ids))

                sample_subject_hits = 0
                sample_subject_misses = 0
                sample_seen = 0
                for msg_id in reversed(msg_ids[-max(8, debug_samples_per_sender):]):
                    fetch_status, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if fetch_status != "OK" or not msg_data:
                        continue
                    raw_message = msg_data[0][1]
                    if not raw_message:
                        continue

                    message = email.message_from_bytes(raw_message, policy=default)
                    subject = (message.get("Subject") or "").lower()
                    date_header = (message.get("Date") or "").strip()
                    keyword_hit = (not subject_keywords) or any(k in subject for k in subject_keywords)
                    if debug and sample_seen < debug_samples_per_sender:
                        logger.info(
                            "PitchBook debug sample [%s]: date=%s | keyword_hit=%s | subject=%s",
                            sender,
                            date_header or "n/a",
                            keyword_hit,
                            (message.get("Subject") or "").strip()[:180],
                        )
                        sample_seen += 1
                    if not keyword_hit:
                        sample_subject_misses += 1
                        continue
                    sample_subject_hits += 1

                    date_header = message.get("Date")
                    published_date = None
                    if date_header:
                        try:
                            dt = parsedate_to_datetime(date_header)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            published_date = dt.astimezone(timezone.utc).isoformat()
                        except Exception:
                            published_date = None

                    html = _extract_html_body(message)
                    if html:
                        soup = BeautifulSoup(html, "html.parser")
                        extracted = _extract_main_pitchbook_sections(
                            soup,
                            source_name,
                            published_date,
                            main_story_limit=int(email_cfg.get("main_story_limit", 2)),
                            debug=debug,
                        )
                        include_external = bool(email_cfg.get("include_external_sections", True))
                        if include_external:
                            extracted.extend(
                                _extract_external_section_articles(
                                    soup,
                                    source_name,
                                    published_date,
                                    debug=debug,
                                )
                            )
                        allow_fallback = bool(email_cfg.get("allow_generic_html_fallback", False))
                        if not extracted and allow_fallback:
                            # Fallback for unexpected template changes.
                            extracted = _extract_articles_from_html(html, source_name, published_date)
                    else:
                        text = _extract_text_body(message)
                        extracted = _extract_articles_from_text(text, source_name, published_date)

                    for article in extracted:
                        url = article.get("url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            all_articles.append(article)

                if debug:
                    logger.info(
                        "PitchBook debug: sender=%s subject_hits=%d subject_misses=%d",
                        sender,
                        sample_subject_hits,
                        sample_subject_misses,
                    )

        if not all_articles:
            return [], "empty"
        return all_articles, "ok"
    except imaplib.IMAP4.error as exc:
        logger.warning("PitchBook email fetch IMAP auth error: %s", exc)
        return [], "imap_auth_error"
    except Exception as exc:
        logger.warning("PitchBook email fetch failed: %s", exc)
        return [], f"error:{exc}"
