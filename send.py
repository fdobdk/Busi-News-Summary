"""send.py — Send the rendered HTML digest via Resend."""

import csv
import json
import logging
import os
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


def load_subscribers(path: str) -> List[dict]:
    """Load subscriber list from a JSON or CSV file."""
    p = Path(path)
    if not p.exists():
        logger.warning("Subscribers file not found: %s", path)
        return []

    if p.suffix.lower() == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("subscribers.json must be a JSON array of objects.")
        return data

    if p.suffix.lower() == ".csv":
        with open(p, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    raise ValueError(f"Unsupported subscriber file format: {p.suffix} (use .json or .csv)")


def send_digest(html: str, subject: str, config: dict, dry_run: bool = False) -> None:
    """
    Send *html* to every subscriber.

    In dry-run mode this is a no-op (the caller has already saved the preview).
    """
    subscribers_file = config["email"]["subscribers_file"]
    subscribers = load_subscribers(subscribers_file)

    if not subscribers:
        logger.warning("No subscribers found — nothing to send.")
        return

    if dry_run:
        logger.info("[DRY RUN] Email send skipped (%d subscriber(s)).", len(subscribers))
        return

    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        raise EnvironmentError("RESEND_API_KEY is not set. Add it to config/.env.")

    try:
        import resend as resend_sdk
    except ImportError:
        raise ImportError("Install the 'resend' package: pip install resend")

    resend_sdk.api_key = api_key
    from_email = config["email"]["from"]
    sent = 0
    failed = 0

    for subscriber in subscribers:
        email = (subscriber.get("email") or "").strip()
        if not email:
            continue
        try:
            resend_sdk.Emails.send({
                "from": from_email,
                "to": email,
                "subject": subject,
                "html": html,
            })
            logger.info("Sent → %s", email)
            sent += 1
        except Exception as exc:
            logger.error("Failed → %s: %s", email, exc)
            failed += 1

    logger.info("Send complete: %d sent, %d failed.", sent, failed)
