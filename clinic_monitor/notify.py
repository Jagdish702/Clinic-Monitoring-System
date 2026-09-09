"""
Stage 8 - email escalation.

One notification list, three triggers, each on its own per-(clinic, trigger)
cooldown so a stuck camera or a long outage cannot spam the inbox:

- High severity event    -> immediately, from EventLogger.log_event()
- Clinic offline past    -> from Database.record_clinic_status(), once the
  a threshold               current offline streak crosses EMAIL_OFFLINE_MINUTES
- Low daily Clinic Score -> from patrol.py, checked once per clinic per visit

Every send goes through send_email(), which is a no-op (and never raises)
when CM_EMAIL_ENABLED is unset - so a deployment that never configures this
behaves exactly as it did before this module existed, matching the
collector's own opt-in pattern (storage/collector_client.py).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from typing import Any, Dict, Optional, Tuple

import config

log = logging.getLogger(__name__)

# In-memory only: resets on restart, which just means one extra email right
# after a deploy or crash-restart, never a missed one. Good enough for a
# single long-running process; nothing here needs to survive a restart.
_last_sent: Dict[Tuple[str, str], float] = {}


def _cooldown_ok(key: Tuple[str, str]) -> bool:
    last = _last_sent.get(key)
    return last is None or (time.time() - last) >= config.EMAIL_COOLDOWN_MINUTES * 60


def send_email(subject: str, body: str) -> bool:
    """
    Send one plain-text email to CM_EMAIL_TO. Never raises - a notification
    failure must not be allowed to interrupt patrol or event logging, the
    same contract collector_client.push() already keeps for the collector.
    """
    if not config.EMAIL_ENABLED:
        return False
    if not (config.EMAIL_FROM and config.EMAIL_APP_PASSWORD and config.EMAIL_TO):
        log.warning("email alerts enabled but not fully configured - skipping")
        return False
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
        log.error("email send failed (%s): %s", subject, exc)
        return False


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
        f"Time: {event.get('timestamp', '')}\n\n"
        f"{description}\n"
        f"Reason: {event.get('reason', '')}\n"
    )
    if send_email(subject, body):
        _last_sent[key] = time.time()


def notify_offline(
    db: Any, clinic_name: str, state: Optional[str], cluster: Optional[str], reason: str
) -> None:
    """
    Call right after a clinic_status "offline" row is written. Walks the
    clinic's recent status history backward to find when the *current*
    offline streak actually began - a single missed lap must not fire this,
    only a streak that has run past EMAIL_OFFLINE_MINUTES.
    """
    key = (clinic_name, "offline")
    if not _cooldown_ok(key):
        return
    rows = db.conn.execute(
        "SELECT ts_epoch, status FROM clinic_status WHERE clinic_name = ? "
        "ORDER BY ts_epoch DESC LIMIT 50",
        (clinic_name,),
    ).fetchall()
    streak_start = None
    for row in rows:
        if row["status"] != "offline":
            break
        streak_start = row["ts_epoch"]
    if streak_start is None:
        return
    minutes_down = (time.time() - streak_start) / 60
    if minutes_down < config.EMAIL_OFFLINE_MINUTES:
        return
    subject = f"[OFFLINE] {clinic_name} unreachable for {int(minutes_down)} min"
    body = (
        f"Clinic: {clinic_name}\n"
        f"State / Cluster: {state or '-'} / {cluster or '-'}\n"
        f"Offline for: {int(minutes_down)} minutes\n"
        f"Last reason: {reason}\n"
    )
    if send_email(subject, body):
        _last_sent[key] = time.time()


def notify_low_score(
    clinic_name: str, state: Optional[str], cluster: Optional[str], score: float
) -> None:
    """Call with a clinic's just-computed 1D Clinic Score after a visit."""
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
