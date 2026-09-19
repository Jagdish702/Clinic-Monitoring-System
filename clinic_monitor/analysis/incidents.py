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

A `normal` reading alone is not fully trusted, though - Gemini's own category
has flip-flopped between a real problem and "normal" for the exact same
unchanged frame before (CUREBAY JASIPUR's obstructed lens, fixed in
ai/gemini_analyzer.py by cross-checking the description text). This module
adds a second, independent check for every resolution: the resolving frame is
compared against the incident's own first-seen frame with the same
mean-pixel-difference measure already used to catch a frozen feed
(analysis.camera_health). A "normal" reading on a frame that still looks like
the original problem does not resolve the incident - it just counts as one
unconfirmed normal reading, and the next reading gets the same chance.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import cv2

import config

log = logging.getLogger(__name__)

NORMAL = "normal"
_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2}
SEVERITY_SCORE = {"High": 90, "Medium": 60, "Low": 30}

# How far back an open incident on the same clinic+camera is still
# considered "the same problem still going on" rather than a new one.
MATCH_WINDOW_SECONDS = 7 * 24 * 3600

# Grayscale mean-absolute-difference (0-255) above which two screenshots
# count as a genuinely different scene, not just lighting/compression noise.
# Looser than camera_health.FROZEN_DIFF on purpose: that compares frames a
# fraction of a second apart from the same visit, this compares frames that
# may be days apart and shot under different lighting (day vs. IR night
# mode) - a much lower bar would call every day/night switch "resolved".
_RESOLVE_DIFF_THRESHOLD = 18.0

# However many "normal, but the frame still looks unchanged" readings in a
# row are tolerated before resolving anyway. Bounds the downside of a wrong
# "still the same" call (from lighting, compression, etc.) to one delayed
# check, rather than an incident that can never close.
_MAX_UNCONFIRMED_NORMALS = 2

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


def _images_differ(path_a: Optional[str], path_b: Optional[str]) -> Optional[bool]:
    """
    Whether two saved screenshots show a genuinely different scene.

    Grayscale (lighting/IR-mode shifts move all three color channels
    together, so color adds noise here without adding information) mean
    absolute difference, the same measure analysis.camera_health already
    uses to catch a frozen feed - just against a much looser threshold,
    since these two frames can be days apart under different lighting
    rather than a fraction of a second apart from the same visit.

    None - not True or False - when either image is missing or unreadable,
    so a caller can tell "looks unchanged" apart from "couldn't check" and
    treat the latter as no reason to withhold resolution.
    """
    if not path_a or not path_b:
        return None
    img_a = cv2.imread(str(Path(config.SCREENSHOT_DIR) / path_a))
    img_b = cv2.imread(str(Path(config.SCREENSHOT_DIR) / path_b))
    if img_a is None or img_b is None:
        return None
    gray_a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)
    if gray_a.shape != gray_b.shape:
        gray_b = cv2.resize(gray_b, (gray_a.shape[1], gray_a.shape[0]))
    return bool(cv2.absdiff(gray_a, gray_b).mean() >= _RESOLVE_DIFF_THRESHOLD)


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
        existing = db.conn.execute(
            "SELECT id, first_screenshot_path, unconfirmed_normal_count "
            "FROM incidents WHERE clinic_name = ? AND camera_name = ? "
            "AND status = 'open'",
            (clinic, camera),
        ).fetchone()
        if not existing:
            return None

        # A "normal" reading is only trusted once the frame itself has
        # moved on from the original problem - see the module docstring.
        # differs is None (couldn't compare - no image on file, or one
        # unreadable) is treated as "no reason to doubt it", same as True.
        differs = _images_differ(existing["first_screenshot_path"], screenshot)
        confirmed = (
            differs is not False
            # +1: this reading, if rejected, is about to become the next
            # unconfirmed count - so _MAX_UNCONFIRMED_NORMALS=2 means the
            # 2nd unchanged-looking "normal" reading resolves anyway, not
            # the 3rd.
            or existing["unconfirmed_normal_count"] + 1 >= _MAX_UNCONFIRMED_NORMALS
        )

        with db.conn as conn:
            if confirmed:
                # The resolving check's own screenshot becomes the incident's
                # "closing" image - last_screenshot_path already means "most
                # recent evidence", and once resolved that's exactly what it is.
                conn.execute(
                    "UPDATE incidents SET status = 'resolved', resolved_ts = ?, "
                    "last_screenshot_path = COALESCE(?, last_screenshot_path) "
                    "WHERE id = ?",
                    (now, screenshot, existing["id"]),
                )
            else:
                log.info(
                    "%s/%s: 'normal' reading rejected - frame still matches "
                    "the original problem (unconfirmed count now %d)",
                    clinic, camera, existing["unconfirmed_normal_count"] + 1,
                )
                conn.execute(
                    "UPDATE incidents SET unconfirmed_normal_count = "
                    "unconfirmed_normal_count + 1 WHERE id = ?",
                    (existing["id"],),
                )
        if confirmed:
            _push(db, int(existing["id"]))
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
                "last_screenshot_path = COALESCE(?, last_screenshot_path), "
                "unconfirmed_normal_count = 0 "
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


def top_concerns(
    db: Any, clinic: str, window: Optional[str] = None, limit: int = 5
) -> List[Dict[str, Any]]:
    """
    A clinic's most frequent non-normal categories in a window, most
    frequent first - "top areas of concern" for a clinic's own page. Counts
    incidents, not raw events, so one long-running problem flagged on every
    visit counts once, not once per check.
    """
    clauses = ["clinic_name = ?", "category != ?"]
    params: List[Any] = [clinic, NORMAL]
    if window:
        from analysis.scoring import WINDOWS, _window
        end, length = WINDOWS.get(window, (0, 30))
        _, since_epoch, until_epoch = _window(end, length)
        clauses.append("last_seen_ts >= ? AND last_seen_ts < ?")
        params.extend([since_epoch, until_epoch])
    where = " AND ".join(clauses)
    rows = db.conn.execute(
        "SELECT category, COUNT(*) AS n, MAX("
        "CASE severity WHEN 'High' THEN 2 WHEN 'Medium' THEN 1 ELSE 0 END"
        ") AS rank FROM incidents "
        f"WHERE {where} GROUP BY category ORDER BY n DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    rank_severity = {v: k for k, v in _SEVERITY_RANK.items()}
    return [
        {
            "category": row["category"],
            "category_group": category_group(row["category"]),
            "count": row["n"],
            "worst_severity": rank_severity.get(row["rank"], "Low"),
        }
        for row in rows
    ]
