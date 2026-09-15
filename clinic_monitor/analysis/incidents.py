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
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

NORMAL = "normal"
_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2}
SEVERITY_SCORE = {"High": 90, "Medium": 60, "Low": 30}

# How far back an open incident on the same clinic+camera is still
# considered "the same problem still going on" rather than a new one.
MATCH_WINDOW_SECONDS = 7 * 24 * 3600

# Fine category -> the broad bucket it's filtered by on the incident list.
# One entry per value ai.gemini_analyzer's "category" field can return.
CATEGORY_GROUPS: Dict[str, str] = {
    "staff_apron": "Staff behavior",
    "staff_grooming": "Staff behavior",
    "staff_badge": "Staff behavior",
    "staff_head_cover": "Staff behavior",
    "ppe_sample_collection": "Staff behavior",
    "staff_phone": "Staff behavior",
    "staff_eating": "Staff behavior",
    "parking_area": "Clinic infrastructure",
    "compound_wall": "Clinic infrastructure",
    "reception_cleanliness": "Clinic infrastructure",
    "medical_waste": "Clinic infrastructure",
    "sample_area_hygiene": "Clinic infrastructure",
    "pharmacy_organization": "Clinic infrastructure",
    "hand_sanitizer": "Clinic infrastructure",
    "camera_health": "Camera-related",
    "emergency": "Emergency",
    "normal": "Normal",
}


def severity_score(severity: str) -> int:
    return SEVERITY_SCORE.get(severity, 0)


def category_group(category: str) -> str:
    return CATEGORY_GROUPS.get(category, "Other")


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


def _push(db: Any, incident_id: int) -> None:
    """
    Mirror one incident's current full state to the collector, the same
    way insert_event()/insert_observation() already push their own tables.

    Guarded by ``db._push`` exactly like those - False means this database
    IS the collector's own copy, and pushing from there would try to reach
    the collector from inside its own request handler. The origin's
    ``incidents.id`` is only unique on that one VM, not across the fleet
    (two clusters can both have an incident #49), so it rides along in the
    payload only for logging - the collector matches rows by
    (clinic, camera, first_seen_ts) instead, see
    Database.upsert_incident().
    """
    if not getattr(db, "_push", False):
        return
    row = db.conn.execute(
        "SELECT * FROM incidents WHERE id = ?", (incident_id,)
    ).fetchone()
    if row:
        from storage import collector_client
        collector_client.push("incidents", dict(row))


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
    screenshot = payload.get("screenshot_path")

    if category == NORMAL:
        resolved_id = db.conn.execute(
            "SELECT id FROM incidents WHERE clinic_name = ? AND camera_name = ? "
            "AND status = 'open'",
            (clinic, camera),
        ).fetchone()
        with db.conn as conn:
            # The resolving check's own screenshot becomes the incident's
            # "closing" image - last_screenshot_path already means "most
            # recent evidence", and once resolved that's exactly what it is.
            conn.execute(
                "UPDATE incidents SET status = 'resolved', resolved_ts = ?, "
                "last_screenshot_path = COALESCE(?, last_screenshot_path) "
                "WHERE clinic_name = ? AND camera_name = ? AND status = 'open'",
                (now, screenshot, clinic, camera),
            )
        if resolved_id:
            _push(db, int(resolved_id["id"]))
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
                "description = ?, "
                "last_screenshot_path = COALESCE(?, last_screenshot_path) "
                "WHERE id = ?",
                (
                    now, _worse(existing["severity"], severity), description,
                    screenshot, existing["id"],
                ),
            )
        _push(db, int(existing["id"]))
        return int(existing["id"])

    with db.conn as conn:
        cursor = conn.execute(
            "INSERT INTO incidents "
            "(clinic_name, camera_name, category, severity, status, "
            "description, first_seen_ts, last_seen_ts, state, cluster, "
            "first_screenshot_path, last_screenshot_path) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
            (
                clinic, camera, category, severity, description, now, now,
                state, cluster, screenshot, screenshot,
            ),
        )
        new_id = int(cursor.lastrowid)
    _push(db, new_id)
    return new_id


SORT_KEYS = {
    "first_seen": lambda i: i["first_seen_ts"],
    "last_seen": lambda i: i["last_seen_ts"],
    "severity": lambda i: i["severity_score"],
    "alphabetical": lambda i: i["clinic_name"].lower(),
    # "Time since Open" for an unresolved incident and "Time to Resolution"
    # for a resolved one are the same underlying duration, just labelled
    # differently in the UI depending on which one the row actually is.
    "duration": lambda i: i["duration_seconds"],
}


def _fmt(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None


def _enrich(row: Dict[str, Any], now: float) -> Dict[str, Any]:
    row["severity_score"] = severity_score(row["severity"])
    row["category_group"] = category_group(row["category"])
    row["duration_seconds"] = (
        (row["resolved_ts"] - row["first_seen_ts"])
        if row["status"] == "resolved" and row["resolved_ts"] is not None
        else (now - row["first_seen_ts"])
    )
    row["first_seen_str"] = _fmt(row["first_seen_ts"])
    row["last_seen_str"] = _fmt(row["last_seen_ts"])
    row["resolved_str"] = _fmt(row["resolved_ts"])
    return row


def list_incidents(
    db: Any,
    *,
    window: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    group: Optional[str] = None,
    clinic: Optional[str] = None,
    state: Optional[str] = None,
    cluster: Optional[str] = None,
    sort: str = "last_seen",
    direction: str = "desc",
) -> List[Dict[str, Any]]:
    """
    Filtered, then sorted, incidents for the incident-list page. Filters
    decide which rows are in the set; sort only reorders that same set -
    it never changes which incidents are in it.
    """
    clauses: List[str] = []
    params: List[Any] = []

    if window:
        from analysis.scoring import WINDOWS, _window
        end, length = WINDOWS.get(window, (0, 7))
        _, since_epoch, until_epoch = _window(end, length)
        clauses.append("last_seen_ts >= ? AND last_seen_ts < ?")
        params.extend([since_epoch, until_epoch])
    if status:
        clauses.append("status = ?")
        params.append(status)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if group:
        categories = [c for c, g in CATEGORY_GROUPS.items() if g == group]
        if categories:
            clauses.append(f"category IN ({', '.join('?' for _ in categories)})")
            params.extend(categories)
    if clinic:
        clauses.append("clinic_name = ?")
        params.append(clinic)
    if state:
        clauses.append("state = ?")
        params.append(state)
    if cluster:
        clauses.append("cluster = ?")
        params.append(cluster)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.conn.execute(f"SELECT * FROM incidents {where}", params).fetchall()

    now = time.time()
    incidents = [_enrich(dict(r), now) for r in rows]
    key_fn = SORT_KEYS.get(sort, SORT_KEYS["last_seen"])
    incidents.sort(key=key_fn, reverse=(direction != "asc"))
    return incidents


def get_incident(db: Any, incident_id: int) -> Optional[Dict[str, Any]]:
    """One incident, enriched with severity_score/category_group/duration."""
    row = db.conn.execute(
        "SELECT * FROM incidents WHERE id = ?", (incident_id,)
    ).fetchone()
    if not row:
        return None
    return _enrich(dict(row), time.time())
