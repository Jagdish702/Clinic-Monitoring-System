"""
Stage 7 - clinic scoring.

Three categories, each 0-100, averaged into one overall Clinic Score:

- Timeliness: how closely the clinic opened/closed to its expected schedule
  (config.EXPECTED_OPEN/EXPECTED_CLOSE, config.SCHEDULE_TOLERANCE_OPEN_MINUTES/
  SCHEDULE_TOLERANCE_CLOSE_MINUTES), reusing report.operating_hours() rather
  than re-deriving open/close times from raw observations a second way.
- Incidents: only High/Medium severity events count - Low is everyday
  activity, not an incident. 100, minus 10 per High and 5 per Medium that
  day, floored at 0.
- Camera Availability: the fraction of camera-observations that were
  actually usable (mirrors report._usable()'s own definition - "ok",
  "needs_cleaning" or unset counts as usable), multiplied by the fraction
  of device checks that found the clinic online at all. A clinic that went
  fully offline produces no observation rows, so without that second
  factor a total outage would be invisible to a pure observations ratio.

A category is ``None`` (not 0) when there is nothing to judge it from - a
clinic never observed in the window has no timeliness verdict, not a scored
failure. The overall score averages whichever categories have a value; a
clinic with zero data in the window scores ``None`` overall, not 0.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import config
from analysis.camera_role import indoor_cameras, infer_roles
from report import _expected_at, operating_hours
from storage.database import Database, ignored_clause

# name -> (end, length): "end" is how many days ago the window's last day
# is (0 = today, 1 = yesterday); "length" is how many calendar days it
# spans. Today/3D/7D/14D/30D all end today and just reach further back;
# Yesterday is the one window that's closed off from today rather than
# running up to now.
WINDOWS: Dict[str, Tuple[int, int]] = {
    "Today": (0, 1),
    "Yesterday": (1, 1),
    "3D": (0, 3),
    "7D": (0, 7),
    "14D": (0, 14),
    "30D": (0, 30),
}

INCIDENT_PENALTY = {"High": 10, "Medium": 5}
UNUSABLE_HEALTH = {"no_signal", "frozen", "too_dark", "obstructed"}


def _window(
    end: int, length: int, today: Optional[date] = None
) -> Tuple[List[str], float, float]:
    """
    The window's calendar-day strings (newest last), start epoch, and end
    epoch (exclusive). ``end`` days back is the window's last day; it spans
    ``length`` days ending there. "Today" (end=0) gets an end epoch of
    midnight tomorrow, which is really just "no upper bound yet" since
    nothing can be timestamped in the future - the same end-exclusive query
    shape works for both a window still running and a truly closed one
    like "Yesterday" (end=1) without special-casing either.
    """
    today = today or datetime.now().date()
    last_day = today - timedelta(days=end)
    day_strs = [(last_day - timedelta(days=i)).isoformat() for i in range(length)][::-1]
    since_epoch = datetime.combine(
        last_day - timedelta(days=length - 1), datetime.min.time()
    ).timestamp()
    until_epoch = datetime.combine(
        last_day + timedelta(days=1), datetime.min.time()
    ).timestamp()
    return day_strs, since_epoch, until_epoch


def _clinic_universe(
    db: Database, day_strs: Sequence[str], state: Optional[str], cluster: Optional[str]
) -> List[str]:
    """
    Every clinic with any observation or status row in the window, narrowed
    to one state/cluster if given. Observations, not events, because a quiet
    but healthy clinic may have no events at all and must still be scored.
    """
    hide, hide_params = ignored_clause()
    clauses = ["day IN ({})".format(", ".join("?" for _ in day_strs))]
    params: List[Any] = list(day_strs)
    if state:
        clauses.append("state = ?")
        params.append(state)
    if cluster:
        clauses.append("cluster = ?")
        params.append(cluster)
    where = " AND ".join(clauses)
    names = set()
    for table in ("observations", "clinic_status"):
        table_hide = f" AND {hide}" if (hide and table == "observations") else ""
        table_params = params + (hide_params if table_hide else [])
        rows = db.conn.execute(
            f"SELECT DISTINCT clinic_name AS c FROM {table} WHERE {where}{table_hide}",
            table_params,
        ).fetchall()
        names.update(r["c"] for r in rows)
    return sorted(names)


def _incident_scores(
    db: Database,
    since_epoch: float,
    until_epoch: float,
    state: Optional[str],
    cluster: Optional[str],
) -> Dict[str, float]:
    """100 minus the weighted High/Medium event count, per clinic, floored at 0."""
    clauses = ["ts_epoch >= ?", "ts_epoch < ?", "severity IN ('High','Medium')"]
    params: List[Any] = [since_epoch, until_epoch]
    if state:
        clauses.append("state = ?")
        params.append(state)
    if cluster:
        clauses.append("cluster = ?")
        params.append(cluster)
    hide, hide_params = ignored_clause()
    if hide:
        clauses.append(hide)
        params.extend(hide_params)
    rows = db.conn.execute(
        "SELECT clinic_name, severity, COUNT(*) AS n FROM events "
        f"WHERE {' AND '.join(clauses)} GROUP BY clinic_name, severity",
        params,
    ).fetchall()
    penalty: Dict[str, float] = defaultdict(float)
    for row in rows:
        penalty[row["clinic_name"]] += INCIDENT_PENALTY[row["severity"]] * row["n"]
    return {clinic: max(0.0, 100.0 - points) for clinic, points in penalty.items()}


def _ttr_minutes(
    db: Database, since_epoch: float, state: Optional[str], cluster: Optional[str]
) -> Dict[str, float]:
    """
    Average minutes-to-resolution, per clinic, over incidents (see
    analysis.incidents) resolved within the window - not yet wired into
    clinic_scores()/the dashboard, built standalone for now.
    """
    clauses = ["status = 'resolved'", "resolved_ts >= ?"]
    params: List[Any] = [since_epoch]
    if state:
        clauses.append("state = ?")
        params.append(state)
    if cluster:
        clauses.append("cluster = ?")
        params.append(cluster)
    hide, hide_params = ignored_clause()
    if hide:
        clauses.append(hide)
        params.extend(hide_params)
    rows = db.conn.execute(
        "SELECT clinic_name, resolved_ts, first_seen_ts FROM incidents "
        f"WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    per_clinic: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        per_clinic[row["clinic_name"]].append(
            (row["resolved_ts"] - row["first_seen_ts"]) / 60
        )
    return {
        clinic: round(sum(minutes) / len(minutes), 1)
        for clinic, minutes in per_clinic.items()
    }


def _camera_availability_scores(
    db: Database, day_strs: Sequence[str], state: Optional[str], cluster: Optional[str]
) -> Dict[str, float]:
    """Usable-observation ratio, multiplied by the online-check ratio."""
    placeholders = ", ".join("?" for _ in day_strs)
    clauses = [f"day IN ({placeholders})"]
    params: List[Any] = list(day_strs)
    if state:
        clauses.append("state = ?")
        params.append(state)
    if cluster:
        clauses.append("cluster = ?")
        params.append(cluster)
    where = " AND ".join(clauses)

    hide, hide_params = ignored_clause()
    obs_where = where + (f" AND {hide}" if hide else "")
    obs_rows = db.conn.execute(
        "SELECT clinic_name, health_status, COUNT(*) AS n FROM observations "
        f"WHERE {obs_where} GROUP BY clinic_name, health_status",
        params + (hide_params if hide else []),
    ).fetchall()
    usable: Dict[str, float] = defaultdict(float)
    total_obs: Dict[str, float] = defaultdict(float)
    for row in obs_rows:
        total_obs[row["clinic_name"]] += row["n"]
        if row["health_status"] not in UNUSABLE_HEALTH:
            usable[row["clinic_name"]] += row["n"]

    status_rows = db.conn.execute(
        "SELECT clinic_name, status, COUNT(*) AS n FROM clinic_status "
        f"WHERE {where} GROUP BY clinic_name, status",
        params,
    ).fetchall()
    online: Dict[str, float] = defaultdict(float)
    total_status: Dict[str, float] = defaultdict(float)
    for row in status_rows:
        total_status[row["clinic_name"]] += row["n"]
        if row["status"] == "online":
            online[row["clinic_name"]] += row["n"]

    clinics = set(total_obs) | set(total_status)
    scores: Dict[str, float] = {}
    for clinic in clinics:
        obs_ratio = (usable[clinic] / total_obs[clinic]) if total_obs.get(clinic) else 1.0
        status_ratio = (
            (online[clinic] / total_status[clinic]) if total_status.get(clinic) else 1.0
        )
        scores[clinic] = round(obs_ratio * status_ratio * 100, 1)
    return scores


def _timeliness_scores(
    db: Database,
    day_strs: Sequence[str],
    clinics: Sequence[str],
    roles: Dict[Tuple[str, str], str],
) -> Dict[str, float]:
    """
    Average, over days the clinic was actually observed, of how close its
    opening and closing were to schedule - full credit inside the open/close
    tolerance windows, one point off per minute beyond them.
    """
    per_clinic: Dict[str, List[float]] = defaultdict(list)
    for clinic in clinics:
        indoor = indoor_cameras(roles, clinic)
        for day in day_strs:
            rows = db.get_observations(day, clinic)
            if not rows:
                continue
            hours = operating_hours(rows, indoor)
            day_when = datetime.strptime(day, "%Y-%m-%d")
            deviations = []
            if hours["opened"]:
                target = _expected_at(day_when, config.EXPECTED_OPEN)
                delta = abs((hours["opened"] - target).total_seconds()) / 60
                deviations.append(max(0.0, delta - config.SCHEDULE_TOLERANCE_OPEN_MINUTES))
            if hours["closed"]:
                target = _expected_at(day_when, config.EXPECTED_CLOSE)
                delta = abs((hours["closed"] - target).total_seconds()) / 60
                deviations.append(max(0.0, delta - config.SCHEDULE_TOLERANCE_CLOSE_MINUTES))
            if not deviations:
                continue  # observed, but never staffed - nothing to judge
            day_score = max(0.0, 100.0 - sum(deviations))
            per_clinic[clinic].append(day_score)
    return {
        clinic: round(sum(scores) / len(scores), 1)
        for clinic, scores in per_clinic.items()
    }


def clinic_scores(
    db: Database,
    window: str = "7D",
    state: Optional[str] = None,
    cluster: Optional[str] = None,
) -> Dict[str, Dict[str, Optional[float]]]:
    """
    One entry per clinic: {"timeliness", "incidents", "camera_availability",
    "overall"} - each None when the window has nothing to judge it from.
    """
    end, length = WINDOWS.get(window, (0, 7))
    day_strs, since_epoch, until_epoch = _window(end, length)
    clinics = _clinic_universe(db, day_strs, state, cluster)
    if not clinics:
        return {}

    incidents = _incident_scores(db, since_epoch, until_epoch, state, cluster)
    availability = _camera_availability_scores(db, day_strs, state, cluster)
    roles = infer_roles(db.camera_descriptions())
    timeliness = _timeliness_scores(db, day_strs, clinics, roles)

    result: Dict[str, Dict[str, Optional[float]]] = {}
    for clinic in clinics:
        cats = {
            "timeliness": timeliness.get(clinic),
            "incidents": incidents.get(clinic, 100.0 if clinic in availability else None),
            "camera_availability": availability.get(clinic),
        }
        present = [v for v in cats.values() if v is not None]
        cats["overall"] = round(sum(present) / len(present), 1) if present else None
        result[clinic] = cats
    return result


def clinic_locations(db: Database) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """
    clinic_name -> (state, cluster), one pick per clinic - used to group an
    already-computed, ungrouped clinic_scores() result by cluster/state
    without recomputing scores per group.
    """
    rows = db.conn.execute(
        "SELECT clinic_name, state, cluster, COUNT(*) AS n FROM observations "
        "GROUP BY clinic_name, state, cluster ORDER BY clinic_name, n DESC"
    ).fetchall()
    picked: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for row in rows:
        picked.setdefault(row["clinic_name"], (row["state"], row["cluster"]))
    return picked


def group_average(
    scores: Dict[str, Dict[str, Optional[float]]],
) -> Optional[float]:
    """Average overall score across a set of already-computed clinic scores."""
    values = [v["overall"] for v in scores.values() if v["overall"] is not None]
    return round(sum(values) / len(values), 1) if values else None


def most_problematic_now(db: Database, limit: int = 3) -> List[Dict[str, Any]]:
    """
    The clinics most worth looking at right now - not a window average, the
    current situation: offline beats a bad recent event, which beats a low
    24-hour score.
    """
    day = datetime.now().date().isoformat()
    # A correlated subquery here ("WHERE ts_epoch = (SELECT MAX(...) WHERE
    # clinic_name = ...)") re-scans the whole table once per row without an
    # index built for it - effectively O(n^2), measured at 17s against a
    # clinic_status table with ~12k rows. SQLite's own documented behavior
    # for a bare column alongside MAX() in one GROUP BY - it comes from the
    # same row as the max - gives the identical result in one pass instead.
    latest_status = db.conn.execute(
        "SELECT clinic_name, status, timestamp, reason, cluster, state, "
        "MAX(ts_epoch) AS ts_epoch FROM clinic_status GROUP BY clinic_name"
    ).fetchall()
    offline_now = {
        r["clinic_name"]: r for r in latest_status if r["status"] == "offline"
    }

    since = time.time() - 2 * 3600
    hide, hide_params = ignored_clause()
    recent = db.conn.execute(
        "SELECT clinic_name, camera_name, severity, description, ts_epoch "
        "FROM events WHERE ts_epoch >= ? AND severity IN ('High','Medium') "
        "AND acknowledged = 0" + (f" AND {hide}" if hide else "") +
        " ORDER BY ts_epoch DESC",
        [since] + (hide_params if hide else []),
    ).fetchall()
    worst_recent: Dict[str, Any] = {}
    for row in recent:
        clinic = row["clinic_name"]
        if clinic not in worst_recent or (
            row["severity"] == "High" and worst_recent[clinic]["severity"] != "High"
        ):
            worst_recent[clinic] = row

    entries: List[Dict[str, Any]] = []
    seen = set()
    for clinic, row in offline_now.items():
        entries.append({
            "clinic_name": clinic, "rank_score": 0.0, "reason": "Device offline",
            "detail": row["reason"] or "unreachable", "state": row["state"],
            "cluster": row["cluster"],
        })
        seen.add(clinic)
    for clinic, row in worst_recent.items():
        if clinic in seen:
            continue
        entries.append({
            "clinic_name": clinic,
            "rank_score": 10.0 if row["severity"] == "High" else 40.0,
            "reason": f"{row['severity']} alert",
            "detail": row["description"],
            "state": None, "cluster": None,
        })
        seen.add(clinic)

    if len(entries) < limit:
        today_scores = clinic_scores(db, window="Today")
        ranked = sorted(
            ((c, v["overall"]) for c, v in today_scores.items() if v["overall"] is not None),
            key=lambda item: item[1],
        )
        for clinic, overall in ranked:
            if len(entries) >= limit * 2:
                break
            if clinic in seen:
                continue
            entries.append({
                "clinic_name": clinic, "rank_score": 50.0 + overall / 10,
                "reason": "Low today score", "detail": f"score {overall}",
                "state": None, "cluster": None,
            })
            seen.add(clinic)

    entries.sort(key=lambda e: e["rank_score"])
    return entries[:limit]
