#!/usr/bin/env python3
"""main.py — Orchestrates the VC/PE Daily Digest pipeline.

Entry point and pipeline coordinator. Parses CLI arguments, loads config,
and runs the 7-step digest pipeline:
  1. Fetch ALL sources into one global pool           (sources.py)
  2. Fuzzy headline dedup — local, zero AI cost       (dedup.py)
  3. Finance-gate filter — local, zero AI cost        (filter.py)
  4. AI categorize + score in batches of 40           (scoring.py)
  5. Greedy cross-category selection — local           (selection.py)
  6. AI rewrite + final semantic dedup per category   (rewrite.py)
  7. Render HTML email + send via Gmail SMTP          (render.py, send.py)
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from dedup import deduplicate
from email_sources import fetch_pitchbook_articles
from filter import finance_gate
from render import CATEGORY_KEY_MAP, get_display_name, print_source_summary, print_terminal_digest, render_html
from rewrite import rewrite_all_categories
from scoring import batch_categorize_and_score
from selection import greedy_select
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


def build_digest(config: dict, no_score: bool = False, verbose: bool = False) -> dict:
    """
    Full pipeline:
      1. Fetch all sources → flat global pool
      2. Fuzzy headline dedup (local)
      3. Finance-gate filter (local)
      4. AI categorize + score
      5. Greedy cross-category selection (local)
      6. AI rewrite + final semantic dedup
    Returns dict of {config_key: {"name": str, "articles": list}}.
    """
    settings = config.get("settings", {})
    min_score = config.get("scoring", {}).get("min_score", 3)
    final_cap = settings.get("final_articles_per_category", 5)
    category_keys = list(config["categories"].keys())

    t0 = time.time()

    # ── Step 1: Fetch all sources ──
    logger.info("━━ Step 1 — Fetching all sources into global pool ━━")
    pool, source_log = fetch_all_articles(config)

    if len(pool) < 50:
        logger.info("Only %d articles from 24h window — retrying with 48h cutoff", len(pool))
        pool, source_log = fetch_all_articles(config, cutoff_hours=48)

    if verbose:
        print_source_summary(source_log)
        logger.info("Step 1 complete: %d articles (%.1fs)", len(pool), time.time() - t0)

    # ── Step 2: Fuzzy headline dedup (local, zero AI cost) ──
    t1 = time.time()
    logger.info("━━ Step 2 — Fuzzy headline dedup ━━")
    deduped = deduplicate(pool)
    if verbose:
        logger.info("Step 2 complete: %d → %d (%.1fs)", len(pool), len(deduped), time.time() - t1)

    # ── Step 3: Finance-gate filter (local, zero AI cost) ──
    t2 = time.time()
    logger.info("━━ Step 3 — Finance-gate filter ━━")
    gated = finance_gate(deduped)
    if verbose:
        logger.info("Step 3 complete: %d → %d (%.1fs)", len(deduped), len(gated), time.time() - t2)

    if no_score:
        # --no-score mode: skip AI, distribute articles round-robin
        logger.info("━━ Scoring SKIPPED (--no-score) — %d articles ━━", len(gated))
        digest: dict = {}
        remaining = list(gated)
        for cat_key in category_keys:
            display_name = get_display_name(cat_key, config)
            articles = remaining[:final_cap]
            remaining = remaining[final_cap:]
            for a in articles:
                a.setdefault("score", 5)
                a.setdefault("rewritten_headline", a.get("title", ""))
                a.setdefault("rewritten_summary", a.get("description", ""))
            digest[cat_key] = {"name": display_name, "articles": articles}
        return digest

    # ── Step 4: AI categorize + score ──
    t3 = time.time()
    logger.info("━━ Step 4 — AI categorize + score (%d articles) ━━", len(gated))
    scored = batch_categorize_and_score(gated, config)
    if verbose:
        logger.info("Step 4 complete: %d scored, %d kept (%.1fs)", len(gated), len(scored), time.time() - t3)

    # Filter by min_score
    scored = [a for a in scored if a.get("score", 0) >= min_score]
    logger.info("After min_score filter (>=%d): %d articles", min_score, len(scored))

    # Rate-limit buffer between scoring and rewrite models
    logger.info("Rate-limit buffer: waiting 15s before rewrite step...")
    time.sleep(15)

    # ── Step 5: Greedy cross-category selection (local, zero AI cost) ──
    t4 = time.time()
    logger.info("━━ Step 5 — Greedy cross-category selection ━━")
    selected, reserves = greedy_select(scored, final_cap)
    if verbose:
        for cat, arts in selected.items():
            logger.info("  %s: %d articles (+%d reserves)", cat, len(arts), len(reserves.get(cat, [])))
        logger.info("Step 5 complete (%.1fs)", time.time() - t4)

    # ── Step 6: Per-category AI dedup (max 3 passes) + rewrite ──
    t5 = time.time()
    total_selected = sum(len(v) for v in selected.values())
    logger.info("━━ Step 6 — Per-category AI dedup + rewrite (%d articles) ━━", total_selected)
    rewritten = rewrite_all_categories(selected, reserves, config, verbose=verbose)
    if verbose:
        logger.info("Step 6 complete (%.1fs)", time.time() - t5)

    # ── Build digest sections (convert short codes → config keys) ──
    digest = {}
    for short_code, articles in rewritten.items():
        config_key = CATEGORY_KEY_MAP.get(short_code, short_code)
        display_name = get_display_name(short_code, config)
        digest[config_key] = {"name": display_name, "articles": articles}
        logger.info("[%s] → %d article(s) in digest", display_name, len(articles))

    # Ensure all categories appear even if empty
    for cat_key in category_keys:
        if cat_key not in digest:
            display_name = get_display_name(cat_key, config)
            digest[cat_key] = {"name": display_name, "articles": []}

    if verbose:
        logger.info("Total pipeline time: %.1fs", time.time() - t0)

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
        help="Where to save the HTML preview in dry-run mode.",
    )
    parser.add_argument(
        "--no-score",
        action="store_true",
        help="Skip AI scoring entirely — distribute fetched articles round-robin.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print source fetch summary, article counts at each stage, and timing.",
    )
    parser.add_argument(
        "--subscribers",
        metavar="FILE",
        help="Override the subscribers file from config.",
    )
    parser.add_argument(
        "--test-pitchbook",
        action="store_true",
        help="Test only PitchBook email ingestion and print extracted articles.",
    )
    parser.add_argument(
        "--pitchbook-cutoff-hours",
        type=int,
        default=24,
        metavar="HOURS",
        help="Hours lookback for --test-pitchbook mode (default: 24).",
    )
    parser.add_argument(
        "--pitchbook-debug",
        action="store_true",
        help="Print detailed IMAP/sender/subject diagnostics in --test-pitchbook mode.",
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent / "config" / ".env")

    if args.no_score:
        logger.info("Running in --no-score mode. AI scoring disabled.")

    if not Path(args.config).exists():
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)
    if args.subscribers:
        config["email"]["subscribers_file"] = args.subscribers

    if args.test_pitchbook:
        logger.info("Running PitchBook-only test mode (cutoff=%dh).", args.pitchbook_cutoff_hours)
        articles, status = fetch_pitchbook_articles(
            config,
            cutoff_hours=args.pitchbook_cutoff_hours,
            debug=args.pitchbook_debug,
        )
        logger.info("PitchBook fetch status: %s", status)
        logger.info("PitchBook extracted articles: %d", len(articles))
        if not articles:
            logger.info(
                "No PitchBook articles found. Check sender list, subject keywords, IMAP inbox, and cutoff hours."
            )
            return

        print()
        print("=" * 70)
        print(f"  PITCHBOOK TEST RESULTS ({len(articles)} article(s))")
        print("=" * 70)
        for i, article in enumerate(articles, start=1):
            title = article.get("title", "")
            source = article.get("source_name", "")
            pub = article.get("published_date") or "n/a"
            url = article.get("url", "")
            summary = (article.get("description") or "").strip()
            print(f"\n  {i}. {title}")
            print(f"     Source: {source} | Published: {pub}")
            if summary:
                print(f"     Summary: {summary[:220]}")
            if url:
                print(f"     URL: {url}")
        print()
        return

    date_str = datetime.now().strftime("%B %d, %Y")

    try:
        digest_sections = build_digest(config, no_score=args.no_score, verbose=args.verbose)
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        sys.exit(1)

    # ── Step 7: Render HTML + send ──
    html = render_html(digest_sections, config, date_str)

    if args.dry_run:
        preview_path = Path(args.preview_file)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(html, encoding="utf-8")
        print_terminal_digest(digest_sections, date_str, args.preview_file)
        logger.info("Dry run complete — no emails sent.")
    else:
        subject = config["email"]["subject"].format(date=date_str)
        send_digest(html, subject, config, dry_run=False)
        logger.info("Done.")


if __name__ == "__main__":
    main()
