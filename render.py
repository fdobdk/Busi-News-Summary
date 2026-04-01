"""render.py — Output rendering for the VC/PE Daily Digest.

Handles all display concerns: HTML email rendering via Jinja2,
terminal digest printing, source summary tables, category display
names, and publication date formatting (EST/HKT dual timezone).
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


# ── Date formatting ──────────────────────────────────────────────────────
# Converts ISO timestamps to dual-timezone display strings for the email
# template (EST/EDT 12-hour + HKT 24-hour).

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
            f"{est.strftime('%b')} {est.day} {_12h(est)} {est_label} / "
            f"{hkt.strftime('%b')} {hkt.day} {_24h(hkt)} HKT"
        )
    except Exception:
        return ""


# ── Category display names ───────────────────────────────────────────────
# Converts internal category keys (e.g. "asia_ipo") to human-readable
# section headers (e.g. "ASIA IPO") for the email and terminal output.

def get_display_name(category_key: str, config: dict) -> str:
    """Get the display name for a category from config, or generate one."""
    cat_cfg = config["categories"].get(category_key, {})
    if cat_cfg.get("display_name"):
        return cat_cfg["display_name"]
    _UP = {"hk", "ipo", "us", "pe", "vc", "asia"}
    return " ".join(
        w.upper() if w in _UP else w.capitalize()
        for w in category_key.split("_")
    )


# ── HTML rendering ───────────────────────────────────────────────────────
# Loads the Jinja2 email template from config/ and renders the final HTML
# with all digest sections, date, and the fmt_pub_date filter.

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


# ── Terminal output ──────────────────────────────────────────────────────
# Pretty-prints the digest and source fetch summary to stdout for
# --dry-run and --verbose modes.

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
