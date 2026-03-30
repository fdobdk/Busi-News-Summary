#!/usr/bin/env python3
"""main.py — Orchestrates the VC/PE Daily Digest pipeline.

New architecture (flat pool):
  1. Fetch ALL sources into one article pool
  2. Finance gate — cheap word check to discard off-topic noise
  3. Global fuzzy dedup by title similarity
  4. AI Call 1 — categorise + score the entire pool
  5. AI Call 2 — rewrite only the winners (score >= min_score, top 5/category)
  6. Group by category, render HTML, send
"""

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dedup import deduplicate
from scoring import batch_categorize_and_score, batch_rewrite, final_dedup_check
from send import send_digest
from sources import fetch_all_articles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fmt_pub_date(iso_str: str) -> str:
    """Convert an ISO datetime string to EST/EDT and HKT in 12-hour format."""
    if not iso_str:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        est = dt.astimezone(ZoneInfo("America/New_York"))
        hkt = dt.astimezone(ZoneInfo("Asia/Hong_Kong"))

        def _12h(d: datetime) -> str:
            hour = d.hour % 12 or 12
            return f"{hour}:{d.strftime('%M')} {'AM' if d.hour < 12 else 'PM'}"

        def _24h(d: datetime) -> str:
            return f"{d.hour}:{d.strftime('%M')}"

        est_label = est.strftime("%Z")   # "EST" or "EDT"
        return (
            f"{est.strftime('%b %d')} · "
            f"{est.month:02d}-{est.day} {_12h(est)} {est_label} / "
            f"{hkt.month:02d}-{hkt.day} {_24h(hkt)} HKT"
        )
    except Exception:
        return ""


def render_html(digest_sections: dict, config: dict, date_str: str) -> str:
    """Render the Jinja2 email template into an HTML string."""
    script_dir = Path(__file__).parent
    env = Environment(
        loader=FileSystemLoader(str(script_dir / "config")),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["fmt_pub_date"] = _fmt_pub_date
    template = env.get_template("email_template.html")
    subject = config["email"]["subject"].format(date=date_str)
    return template.render(sections=digest_sections, date=date_str, subject=subject)


def print_terminal_digest(digest_sections: dict, date_str: str, preview_path: str) -> None:
    """Pretty-print the digest to stdout."""
    width = 70
    print()
    print("=" * width)
    print(f"  VC / PE DAILY DIGEST — {date_str}")
    print("=" * width)

    for _, section in digest_sections.items():
        print(f"\n{'─' * width}")
        print(f"  {section['name'].upper()}")
        print(f"{'─' * width}")

        articles = section.get("articles", [])
        if not articles:
            print("  (no qualifying articles today)")
            continue

        for i, article in enumerate(articles, start=1):
            headline = article.get("title", "")
            summary  = article.get("rewritten_summary") or article.get("description", "")
            score    = article.get("score", "?")
            source   = article.get("source", "")
            url      = article.get("url", "")

            print(f"\n  {i}.  {headline}")
            words, line = summary.split(), ""
            for word in words:
                if len(line) + len(word) + 1 > 64:
                    print(f"      {line}")
                    line = word
                else:
                    line = f"{line} {word}".strip()
            if line:
                print(f"      {line}")
            print(f"      Score: {score}/5  |  {source}")
            if url:
                print(f"      {url}")

    print()
    print("=" * width)
    print(f"  HTML preview saved → {preview_path}")
    print("=" * width)
    print()


# ── Finance gate ─────────────────────────────────────────────────────────
# Universal relevance check — NOT a category filter.  Discards articles
# with zero finance-related terms (weather, sports, politics, etc.)

_FINANCE_GATE_WORDS = {
    "deal", "acquire", "fund", "raise", "ipo", "listing", "invest",
    "lend", "loan", "credit", "startup", "merger", "buyout", "stake",
    "billion", "million", "venture", "equity", "debt", "default",
    "exit", "portfolio",
}


def _finance_gate(articles: list) -> list:
    """Keep only articles whose title or description contains at least one finance term."""
    kept = []
    for article in articles:
        text = (
            (article.get("title") or "") + " " + (article.get("description") or "")
        ).lower()
        # Split on non-alpha to match whole-ish words (avoids "default" matching inside "defaulting" is fine,
        # but more importantly avoids "fund" matching inside "fundamental" — we accept this trade-off
        # since the gate is intentionally broad)
        if any(word in text for word in _FINANCE_GATE_WORDS):
            kept.append(article)
    return kept


def _get_display_name(category_key: str, config: dict) -> str:
    """Get the display name for a category from config, or generate one."""
    cat_cfg = config["categories"].get(category_key, {})
    if cat_cfg.get("display_name"):
        return cat_cfg["display_name"]
    _UP = {"hk", "ipo", "us", "pe", "vc", "asia"}
    return " ".join(
        w.upper() if w in _UP else w.capitalize()
        for w in category_key.split("_")
    )


def print_source_summary(source_log: list) -> None:
    """Print a per-source fetch summary table (shown with --verbose)."""
    width = 70
    print()
    print("─" * width)
    print("  SOURCE FETCH SUMMARY")
    print("─" * width)
    print(f"  {'SOURCE':<30} {'SECTION':<18} {'ARTICLES':>8}  STATUS")
    print("  " + "·" * (width - 2))
    for entry in source_log:
        flag_str = "  [paywalled]" if entry.get("paywalled") else ""
        status = entry["status"]
        marker = "✓" if status == "ok" else "✗"
        print(
            f"  {marker} {entry['source']:<29} {entry['section']:<18} "
            f"{entry['count']:>8}  {status}{flag_str}"
        )
    print("─" * width)
    print()


_PC_DEBUG_KEYWORDS = re.compile(
    r"private credit|direct lend|BDC|CLO|credit fund|private debt|leveraged loan|"
    r"mezzanine|unitranche|loan default|credit spread|redemption|BCRED|HLEND|"
    r"Owl Rock|Ares Capital|Apollo Debt|Blue Owl|debt fund",
    re.IGNORECASE,
)


def _save_debug_log(articles: list, scored: list) -> str:
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


def build_digest(config: dict, no_score: bool = False, verbose: bool = False, debug: bool = False) -> dict:
    """
    Full pipeline:
      1. Fetch all sources → flat pool
      2. Finance gate
      3. Global fuzzy dedup
      4. AI categorise + score  (or passthrough if --no-score)
      5. Group by category, take top N
      6. AI rewrite winners
    """
    settings = config.get("settings", {})
    min_score = config.get("scoring", {}).get("min_score", 4)
    final_cap = settings.get("final_articles_per_category", 5)
    category_keys = list(config["categories"].keys())

    # ── Step 1: Fetch ──
    logger.info("━━ Step 1 — Fetching all sources into one pool ━━")
    pool, source_log = fetch_all_articles(config)

    if len(pool) < 50:
        logger.info("Only %d articles from 24h window — retrying with 48h cutoff", len(pool))
        pool, source_log = fetch_all_articles(config, cutoff_hours=48)

    if verbose:
        print_source_summary(source_log)

    # ── Step 2: Finance gate ──
    logger.info("━━ Step 2 — Finance gate ━━")
    gated = _finance_gate(pool)
    logger.info("Finance gate: %d → %d articles", len(pool), len(gated))

    # ── Step 3: Global fuzzy dedup ──
    logger.info("━━ Step 3 — Global dedup ━━")
    deduped = deduplicate(gated)

    if no_score:
        # --no-score mode: skip AI, assign no categories, show all in original order
        logger.info("━━ Scoring SKIPPED (--no-score) — %d articles ━━", len(deduped))
        digest: dict = {}
        # In no-score mode, distribute articles round-robin into categories
        # so the template still renders sections
        for cat_key in category_keys:
            display_name = _get_display_name(cat_key, config)
            articles = deduped[:final_cap]
            deduped = deduped[final_cap:]
            for a in articles:
                a.setdefault("score", 5)
                a.setdefault("rewritten_summary", a.get("description", ""))
            digest[cat_key] = {"name": display_name, "articles": articles}
        return digest

    # ── Step 4: AI categorise + score ──
    logger.info("━━ Step 4 — AI categorise + score (%d articles) ━━", len(deduped))
    pre_score_pool = list(deduped) if debug else []
    scored = batch_categorize_and_score(deduped, config)
    time.sleep(2)  # rate-limit courtesy before rewrite call

    if debug:
        debug_path = _save_debug_log(pre_score_pool, scored)
        logger.info("Debug log saved → %s", debug_path)

    # ── Step 5: Group by category, take top N per category ──
    by_category = defaultdict(list)
    for article in scored:
        cat = article.get("section")
        if cat in category_keys and article.get("score", 0) >= min_score:
            by_category[cat].append(article)

    # Sort each category by score desc, then recency desc
    for cat in by_category:
        by_category[cat].sort(
            key=lambda a: (
                -a.get("score", 0),
                -(datetime.fromisoformat(a["published_at"]).timestamp()
                  if a.get("published_at") else 0),
            ),
        )

    # ── Step 5b: Final AI dedup across all scored articles ──
    total_scored = sum(len(v) for v in by_category.values())
    logger.info("━━ Step 5b — Final cross-batch dedup (%d scored articles) ━━", total_scored)
    iterations = final_dedup_check(by_category, category_keys, config)
    print(f"\n  Final dedup: {iterations} pass(es) run (max 3)\n")

    # Take top N per category after dedup
    winners = []
    top_by_cat = {}
    for cat_key in category_keys:
        top = by_category.get(cat_key, [])[:final_cap]
        top_by_cat[cat_key] = top
        winners.extend(top)

    # ── Step 6: Rewrite winners ──
    logger.info("━━ Step 5 — Rewriting %d winner articles ━━", len(winners))
    batch_rewrite(winners, config)

    # ── Build digest sections ──
    digest = {}
    for cat_key in category_keys:
        display_name = _get_display_name(cat_key, config)
        articles = top_by_cat.get(cat_key, [])
        digest[cat_key] = {"name": display_name, "articles": articles}
        logger.info("[%s] → %d article(s) in digest", display_name, len(articles))

    return digest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VC/PE Daily Digest — generate and (optionally) send the email."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print digest to terminal and save HTML preview; do NOT send emails.",
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        metavar="FILE",
        help="Path to config YAML file (default: config/config.yaml).",
    )
    parser.add_argument(
        "--preview-file",
        default="preview/email_preview.html",
        metavar="FILE",
        help="Where to save the HTML preview in dry-run mode (default: preview/email_preview.html).",
    )
    parser.add_argument(
        "--no-score",
        action="store_true",
        help="Skip AI scoring entirely — all fetched articles pass through. "
             "Use for testing the pipeline without a Groq API key.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a per-source fetch summary showing which sources succeeded, "
             "returned 0 articles, or errored (401/403/etc.).",
    )
    parser.add_argument(
        "--subscribers",
        metavar="FILE",
        help="Override the subscribers file from config (e.g. config/subscribers_hk.json).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save a debug JSON file showing all articles that matched Private Credit "
             "keywords before AI scoring, what category the AI assigned, and what score.",
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent / "config" / ".env")

    if args.no_score:
        logger.info("Running in --no-score mode. AI scoring disabled — all articles will appear in the digest.")

    if not Path(args.config).exists():
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)
    if args.subscribers:
        config["email"]["subscribers_file"] = args.subscribers
    date_str = datetime.now().strftime("%B %d, %Y")

    try:
        digest_sections = build_digest(config, no_score=args.no_score, verbose=args.verbose, debug=args.debug)
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        sys.exit(1)

    html = render_html(digest_sections, config, date_str)

    if args.dry_run:
        Path(args.preview_file).write_text(html, encoding="utf-8")
        print_terminal_digest(digest_sections, date_str, args.preview_file)
        logger.info("Dry run complete — no emails sent.")
    else:
        subject = config["email"]["subject"].format(date=date_str)
        send_digest(html, subject, config, dry_run=False)
        logger.info("Done.")


if __name__ == "__main__":
    main()
