"""
Incident matching and lifecycle.

`events` records individual detections; this turns repeat detections of the
same problem into one `incidents` row with a lifecycle (open -> resolved) and
a duration - the only lifecycle precedent already in this codebase is
`Database.record_clinic_status()`'s online/offline flip, which this mirrors:
a fresh reading either continues the current state or closes it out.

Matching is scoped to (clinic, camera) - a single Gemini call reports at most
one category per frame, so there is only ever one truly-open incident per
camera to find. `category == "normal"` (Gemini's "Low, nothing on the
checklist applies" case) is the resolving signal, the same way `status ==
"online"` resolves an offline streak.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

NORMAL = "normal"
_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2}

# How far back an open incident on the same clinic+camera is still
# considered "the same problem still going on" rather than a new one.
MATCH_WINDOW_SECONDS = 7 * 24 * 3600


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


def classify_and_link(db: Any, payload: Dict[str, Any]) -> Optional[int]:
    """
    Resolve or continue an incident for this event's (clinic, camera), and
    return the incident id the event itself should be stamped with - None
    for a `normal` reading (nothing to link) or when there is no category to
    act on at all (e.g. a non-Gemini source).

    ``db`` is a storage.database.Database, left untyped here to avoid a
    circular import - Database.insert_event() calls this module.
    """
    category = payload.get("category")
    if not category:
        return None

    clinic = payload["clinic_name"]
    camera = payload["camera_name"]
    now = payload.get("ts_epoch") or time.time()
    state = payload.get("state")
    cluster = payload.get("cluster")

    if category == NORMAL:
        with db.conn as conn:
            conn.execute(
                "UPDATE incidents SET status = 'resolved', resolved_ts = ? "
                "WHERE clinic_name = ? AND camera_name = ? AND status = 'open'",
                (now, clinic, camera),
            )
        return None

    severity = payload.get("severity", "Low")
    description = payload.get("description") or ""

    existing = db.conn.execute(
        "SELECT id, severity FROM incidents "
        "WHERE clinic_name = ? AND camera_name = ? AND status = 'open' "
        "AND first_seen_ts >= ? ORDER BY last_seen_ts DESC LIMIT 1",
        (clinic, camera, now - MATCH_WINDOW_SECONDS),
    ).fetchone()

    if existing:
        with db.conn as conn:
            conn.execute(
                "UPDATE incidents SET last_seen_ts = ?, severity = ?, "
                "description = ? WHERE id = ?",
                (now, _worse(existing["severity"], severity), description, existing["id"]),
            )
        return int(existing["id"])

    with db.conn as conn:
        cursor = conn.execute(
            "INSERT INTO incidents "
            "(clinic_name, camera_name, category, severity, status, "
            "description, first_seen_ts, last_seen_ts, state, cluster) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (clinic, camera, category, severity, description, now, now, state, cluster),
        )
        return int(cursor.lastrowid)
