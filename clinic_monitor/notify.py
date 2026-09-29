"""
Stage 8 - email and Teams escalation.

One notification list, three triggers, each on its own cooldown so a stuck
camera or a long outage cannot spam the inbox:

- High severity event    -> immediately, from EventLogger.log_event(),
  a threshold               per (clinic, trigger) - also posted to Teams,
                             see below
- Clinic offline past    -> from Database.record_clinic_status(), once the
  a threshold               current offline streak crosses EMAIL_OFFLINE_MINUTES,
                             per (cluster, trigger) - one digest listing every
                             clinic currently offline in the cluster, not one
                             email per clinic, so a cluster-wide outage cannot
                             fire a burst large enough to trip the mail
                             provider's own daily sending limit
- Low daily Clinic Score -> from patrol.py, checked once per clinic per
                             visit, per (clinic, trigger)

Every send goes through send_email(), which is a no-op (and never raises)
when CM_EMAIL_ENABLED is unset - so a deployment that never configures this
behaves exactly as it did before this module existed, matching the
collector's own opt-in pattern (storage/collector_client.py).

High-severity events are also posted to a Teams channel via
send_teams_message(), a no-op the same way when CM_TEAMS_ENABLED is unset -
its own independent switch, not tied to email being configured at all.
"""

from __future__ import annotations

import base64
import json
import logging
import smtplib
import ssl
import time
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
import requests

import config

log = logging.getLogger(__name__)

# In-memory only: resets on restart, which just means one extra email right
# after a deploy or crash-restart, never a missed one. Good enough for a
# single long-running process; nothing here needs to survive a restart.
_last_sent: Dict[Tuple[str, str], float] = {}

# Set when the mail provider itself reports its sending limit exceeded (a
# real Gmail 550 seen during the 2026-09-16 Berhampur outage, when the whole
# cluster going offline at once fired one email per clinic and tripped the
# free account's daily cap). Every other clinic's send would fail the exact
# same way until the provider's own window clears, so sends are skipped
# outright rather than retried - one log line instead of dozens.
_quota_backoff_until = 0.0

# Gmail's own wording for this condition ("550 5.4.5 Daily user sending
# limit exceeded ..."); matched case-insensitively against the exception
# text rather than the SMTP code alone, since a bare 550 covers other
# rejections (bad recipient, policy) that should keep retrying normally.
_QUOTA_ERROR_MARKER = "sending limit exceeded"


def _cooldown_ok(key: Tuple[str, str]) -> bool:
    last = _last_sent.get(key)
    return last is None or (time.time() - last) >= config.EMAIL_COOLDOWN_MINUTES * 60


def send_email(subject: str, body: str, image_path: Optional[Path] = None) -> bool:
    """
    Send one email to CM_EMAIL_TO, plain text unless ``image_path`` points at
    a real file - the evidence screenshot for a High-severity event - in
    which case it's attached inline. Never raises - a notification failure
    must not be allowed to interrupt patrol or event logging, the same
    contract collector_client.push() already keeps for the collector.
    """
    if not config.EMAIL_ENABLED:
        return False
    if not (config.EMAIL_FROM and config.EMAIL_APP_PASSWORD and config.EMAIL_TO):
        log.warning("email alerts enabled but not fully configured - skipping")
        return False
    global _quota_backoff_until
    if time.time() < _quota_backoff_until:
        return False

    image_bytes = None
    if image_path is not None:
        try:
            image_bytes = image_path.read_bytes()
        except OSError as exc:
            log.warning("could not attach screenshot %s: %s", image_path, exc)

    if image_bytes:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body))
        image = MIMEImage(image_bytes, name=image_path.name)
        image.add_header("Content-Disposition", "attachment", filename=image_path.name)
        msg.attach(image)
    else:
        msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM
    msg["To"] = ", ".join(config.EMAIL_TO)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT, timeout=15) as server:
            server.starttls(context=context)
            server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
            server.sendmail(config.EMAIL_FROM, list(config.EMAIL_TO), msg.as_string())
        log.info("email sent: %s", subject)
        return True
    except Exception as exc:                      # never let email break the caller
        if _QUOTA_ERROR_MARKER in str(exc).lower():
            _quota_backoff_until = time.time() + config.EMAIL_QUOTA_BACKOFF_MINUTES * 60
            log.error(
                "email provider sending limit exceeded (%s) - suppressing "
                "all email sends for %d min", subject, config.EMAIL_QUOTA_BACKOFF_MINUTES,
            )
        else:
            log.error("email send failed (%s): %s", subject, exc)
        return False


# Teams' "Post card in a chat or channel" action hard-rejects any card over
# ~28 KB of JSON with a 413 RequestEntityTooLarge - and that rejection
# happens *inside* the flow, invisibly to us: the webhook itself still
# returns 202 Accepted, so there is no way to detect the failure from the
# HTTP response. The only real fix is staying under the limit in the first
# place. CARD_SIZE_BUDGET leaves headroom below the real ~28 KB ceiling for
# the JSON structure itself (keys, braces, the schema boilerplate).
CARD_SIZE_BUDGET = 24_000
# A raw screenshot (17-21 KB) already becomes 23-28 KB once base64-encoded -
# right at or over the whole card's own budget before a single word of text
# is added. Shrunk to this size on read, never touching the original file
# (still used at full quality for the email attachment and the dashboard).
# Named distinctly from config.SCREENSHOT_JPEG_QUALITY - that one governs
# the capture pipeline's own saved files, unrelated to this card-only copy.
TEAMS_SCREENSHOT_MAX_DIMENSION = 480
TEAMS_SCREENSHOT_JPEG_QUALITY = 55


def _shrink_screenshot_for_card(image_path: Path) -> Optional[bytes]:
    """
    A small, low-quality re-encode of the screenshot, built only for
    embedding in a Teams card - never written back to image_path. Returns
    None (not raises) on any failure, so a corrupt or unreadable frame just
    means the card goes out without an image rather than not going out at
    all.
    """
    try:
        data = np.frombuffer(image_path.read_bytes(), dtype=np.uint8)
        frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if frame is None:
            return None
        h, w = frame.shape[:2]
        scale = TEAMS_SCREENSHOT_MAX_DIMENSION / max(h, w)
        if scale < 1:
            frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
        ok, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), TEAMS_SCREENSHOT_JPEG_QUALITY]
        )
        return encoded.tobytes() if ok else None
    except Exception as exc:
        log.warning("could not shrink screenshot %s for Teams card: %s", image_path, exc)
        return None


def send_teams_message(
    subject: str, body: str, image_path: Optional[Path] = None
) -> bool:
    """
    Post one Adaptive Card to the Teams channel behind CM_TEAMS_WEBHOOK_URL.

    The flow behind this webhook ("When a Teams webhook request is
    received" + "Post card in a chat or channel", Power Automate's own
    template) only accepts the standard Teams message-with-adaptive-card
    envelope - a plain {"text": ...} body fails its own schema check with
    "Property 'type' must be 'AdaptiveCard'" (confirmed against a real
    flow's failed-run diagnostics, 2026-09-28). This is not a generic
    Incoming Webhook - the request body must be exactly this shape.

    ``image_path``, when given, is embedded directly in the card as a
    base64 data URI rather than linked by URL - the dashboard that would
    otherwise serve it binds to 127.0.0.1 only (the SSH tunnel is the
    whole security model, see DEPLOY_GCP.md), so Teams' own servers could
    never fetch a URL to it. It is shrunk first (see
    _shrink_screenshot_for_card) and dropped entirely if the card would
    still be too big even shrunk - see CARD_SIZE_BUDGET.

    Never raises - see send_email()'s docstring for why.
    """
    if not config.TEAMS_ENABLED:
        return False
    if not config.TEAMS_WEBHOOK_URL:
        log.warning("Teams alerts enabled but CM_TEAMS_WEBHOOK_URL is not set - skipping")
        return False
    lines = [ln for ln in body.splitlines() if ln.strip()]
    card_body = [
        {"type": "TextBlock", "text": subject, "weight": "Bolder", "size": "Medium", "wrap": True},
    ] + [{"type": "TextBlock", "text": ln, "wrap": True} for ln in lines]

    def _card_payload(body_items):
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": body_items,
                    },
                }
            ],
        }

    payload = _card_payload(card_body)
    if image_path is not None:
        small = _shrink_screenshot_for_card(image_path)
        if small is not None:
            image_item = {
                "type": "Image",
                "url": f"data:image/jpeg;base64,{base64.b64encode(small).decode('ascii')}",
                "size": "Stretch",
                "altText": "evidence screenshot",
            }
            candidate = _card_payload(card_body + [image_item])
            if len(json.dumps(candidate)) <= CARD_SIZE_BUDGET:
                payload = candidate
            else:
                log.warning(
                    "Teams card for %s still too large with the shrunk screenshot "
                    "(%d bytes shrunk) - sending without the image",
                    subject, len(small),
                )
    try:
        resp = requests.post(config.TEAMS_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Teams message sent: %s", subject)
        return True
    except Exception as exc:                       # never let Teams break the caller
        log.error("Teams send failed (%s): %s", subject, exc)
        return False


def _format_time(event: Dict[str, Any]) -> str:
    """
    dd/mm/yy hh:mm AM/PM, from ts_epoch when available - readable at a
    glance in an email subject line or a phone notification, unlike the
    raw ISO 8601 timestamp (e.g. 2026-09-28T22:47:07+05:30) stored on the
    event for everything else that reads it.
    """
    ts_epoch = event.get("ts_epoch")
    if ts_epoch is not None:
        try:
            return datetime.fromtimestamp(ts_epoch).strftime("%d/%m/%y %I:%M %p")
        except (OSError, OverflowError, ValueError):
            pass
    return event.get("timestamp", "")


def notify_high_severity(event: Dict[str, Any]) -> None:
    """Call right after a High-severity event is inserted."""
    if event.get("severity") != "High":
        return
    clinic = event.get("clinic_name") or "unknown clinic"
    key = (clinic, "high")
    if not _cooldown_ok(key):
        return
    description = event.get("description") or ""
    subject = f"[HIGH] {clinic} - {description[:80]}"
    body = (
        f"Clinic: {clinic}\n"
        f"Camera: {event.get('camera_name', '')}\n"
        f"State / Cluster: {event.get('state') or '-'} / {event.get('cluster') or '-'}\n"
        f"Time: {_format_time(event)}\n\n"
        f"{description}\n"
        f"Reason: {event.get('reason', '')}\n"
    )
    screenshot = event.get("screenshot_path")
    image_path = Path(config.SCREENSHOT_DIR) / screenshot if screenshot else None
    sent_email = send_email(subject, body, image_path)
    sent_teams = send_teams_message(subject, body, image_path)
    if sent_email or sent_teams:
        _last_sent[key] = time.time()


def _offline_minutes(db: Any, clinic_name: str) -> Optional[float]:
    """
    How long a clinic's current offline streak has been running, or None if
    its most recent check was not offline at all. Walks clinic_status
    backward from the latest row to find when the streak began.
    """
    rows = db.conn.execute(
        "SELECT ts_epoch, status FROM clinic_status WHERE clinic_name = ? "
        "ORDER BY ts_epoch DESC LIMIT 50",
        (clinic_name,),
    ).fetchall()
    if not rows or rows[0]["status"] != "offline":
        return None
    streak_start = None
    for row in rows:
        if row["status"] != "offline":
            break
        streak_start = row["ts_epoch"]
    return (time.time() - streak_start) / 60 if streak_start is not None else None


def notify_offline(
    db: Any, clinic_name: str, state: Optional[str], cluster: Optional[str], reason: str
) -> None:
    """
    Call right after a clinic_status "offline" row is written.

    Rate-limited per cluster, not per clinic. A whole cluster going down at
    once - one wedged emulator, one lost network link - used to fire one
    email per clinic within minutes of each other: the 2026-09-16 Berhampur
    outage sent about 20 of these back to back and tripped the mail
    account's daily sending limit, silently dropping the rest of the day's
    alerts fleet-wide. One email per cluster per cooldown window, listing
    every clinic currently offline in it, says the same thing for a
    fraction of the sends - and the clinic that triggered it still has to
    have been down past EMAIL_OFFLINE_MINUTES, so a single missed lap still
    cannot fire this.
    """
    key = (cluster or clinic_name, "offline")
    if not _cooldown_ok(key):
        return
    minutes_down = _offline_minutes(db, clinic_name)
    if minutes_down is None or minutes_down < config.EMAIL_OFFLINE_MINUTES:
        return

    others = db.currently_offline(cluster) if cluster else None
    if not others:
        others = [{"clinic_name": clinic_name, "reason": reason}]
    lines = []
    for row in others:
        mins = _offline_minutes(db, row["clinic_name"])
        down_for = f"{int(mins)} min" if mins is not None else "unknown"
        lines.append(f"  - {row['clinic_name']}: down {down_for} - {row['reason'] or '-'}")

    count = len(others)
    if count > 1:
        subject = f"[OFFLINE] {cluster or state} - {count} clinics unreachable"
    else:
        subject = f"[OFFLINE] {clinic_name} unreachable for {int(minutes_down)} min"
    body = (
        f"State / Cluster: {state or '-'} / {cluster or '-'}\n"
        f"{count} clinic(s) currently unreachable:\n\n" + "\n".join(lines) + "\n"
    )
    if send_email(subject, body):
        _last_sent[key] = time.time()


def notify_low_score(
    clinic_name: str, state: Optional[str], cluster: Optional[str], score: float
) -> None:
    """Call with a clinic's just-computed 1D Clinic Score after a visit."""
    if not config.EMAIL_LOW_SCORE_ENABLED:
        return
    if score >= config.EMAIL_LOW_SCORE_THRESHOLD:
        return
    key = (clinic_name, "low_score")
    if not _cooldown_ok(key):
        return
    subject = f"[LOW SCORE] {clinic_name} scored {score:.0f}/100 today"
    body = (
        f"Clinic: {clinic_name}\n"
        f"State / Cluster: {state or '-'} / {cluster or '-'}\n"
        f"Today's Clinic Score: {score:.0f}/100 "
        f"(threshold {config.EMAIL_LOW_SCORE_THRESHOLD:.0f})\n"
    )
    if send_email(subject, body):
        _last_sent[key] = time.time()
