"""send.py — Send the rendered HTML digest via Gmail SMTP."""

import csv
import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
    Send *html* to every subscriber via Gmail SMTP.

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

    gmail_address = os.getenv("GMAIL_ADDRESS", "")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")
    if not gmail_address or not gmail_password:
        raise EnvironmentError(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set. Add them to config/.env."
        )

    sent = 0
    failed = 0

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_password)

        for subscriber in subscribers:
            email = (subscriber.get("email") or "").strip()
            if not email:
                continue
            try:
                msg = MIMEMultipart("alternative")
                msg["From"] = gmail_address
                msg["To"] = email
                msg["Subject"] = subject
                msg.attach(MIMEText(html, "html"))

                server.sendmail(gmail_address, email, msg.as_string())
                logger.info("Sent → %s", email)
                sent += 1
            except Exception as exc:
                logger.error("Failed → %s: %s", email, exc)
                failed += 1

    logger.info("Send complete: %d sent, %d failed.", sent, failed)
