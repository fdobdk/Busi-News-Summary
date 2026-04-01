#!/usr/bin/env python3
"""main.py — Orchestrates the VC/PE Daily Digest pipeline.

Entry point and pipeline coordinator. Parses CLI arguments, loads config,
and runs the 6-step digest pipeline:
  1. Fetch ALL sources into one article pool       (sources.py)
  2. Finance gate — discard off-topic noise         (filters.py)
  3. Global fuzzy dedup by title similarity          (dedup.py)
  4. AI categorise + score the entire pool           (scoring.py)
  5. Group by category, top N, final cross-batch dedup
  6. AI rewrite only the winners                     (scoring.py)
"""

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from dedup import deduplicate
from filters import finance_gate, save_debug_log
from render import get_display_name, print_source_summary, print_terminal_digest, render_html
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
    gated = finance_gate(pool)
    logger.info("Finance gate: %d → %d articles", len(pool), len(gated))

    # ── Step 3: Global fuzzy dedup ──
    logger.info("━━ Step 3 — Global dedup ━━")
    deduped = deduplicate(gated)

    if no_score:
        # --no-score mode: skip AI, distribute articles round-robin into categories
        logger.info("━━ Scoring SKIPPED (--no-score) — %d articles ━━", len(deduped))
        digest: dict = {}
        for cat_key in category_keys:
            display_name = get_display_name(cat_key, config)
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
        debug_path = save_debug_log(pre_score_pool, scored)
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

    # Take top N per category, keep remainder as reserve for backfill
    top_by_cat = {}
    reserve_by_cat = {}
    for cat_key in category_keys:
        full = by_category.get(cat_key, [])
        top_by_cat[cat_key] = full[:final_cap]
        reserve_by_cat[cat_key] = full[final_cap:]

    # ── Step 5b: Final AI dedup across top picks (with backfill from reserve) ──
    total_top = sum(len(v) for v in top_by_cat.values())
    logger.info("━━ Step 5b — Final cross-batch dedup (%d top articles) ━━", total_top)
    iterations = final_dedup_check(top_by_cat, reserve_by_cat, category_keys, config, final_cap)
    print(f"\n  Final dedup: {iterations} pass(es) run (max 4)\n")

    winners = []
    for cat_key in category_keys:
        winners.extend(top_by_cat.get(cat_key, []))

    # ── Step 6: Rewrite winners ──
    logger.info("━━ Step 6 — Rewriting %d winner articles ━━", len(winners))
    batch_rewrite(winners, config)

    # ── Build digest sections ──
    digest = {}
    for cat_key in category_keys:
        display_name = get_display_name(cat_key, config)
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
        help="Override the subscribers file from config (e.g. subscribers/subscribers_hk.json).",
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
