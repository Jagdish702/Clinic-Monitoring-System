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

Three supplementary metrics ride alongside the three above, on every
clinic_scores() row, but are never folded into "overall" - they answer a
different question (how much of what's happening is concerning, and how
long it takes to clear) rather than contributing another 0-100 grade:

- High/Medium concern %: of every check logged in the window, what share
  came back High and what share came back Medium - out of the total check
  count, not just the High/Medium ones, so a clinic checked rarely with one
  bad reading reads as more concerning than one checked constantly with
  that same one bad reading buried in hundreds of fine ones.
- ETTR (estimated time to resolution): average minutes from an incident's
  first sighting to its resolution, over incidents that resolved within the
  window (analysis.incidents; see _ttr_minutes()).

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
    analysis.incidents) resolved within the window - clinic_scores()'s
    "ettr_minutes".
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


def _concern_pct_scores(
    db: Database,
    since_epoch: float,
    until_epoch: float,
    state: Optional[str],
    cluster: Optional[str],
) -> Dict[str, Dict[str, float]]:
    """
    Per clinic, {"high_pct", "medium_pct"}: share of the window's logged
    checks that came back High and Medium severity, out of every check
    logged (not just the concerning ones) - clinic_scores()'s "high_pct" and
    "medium_pct".
    """
    clauses = ["ts_epoch >= ?", "ts_epoch < ?"]
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
    totals: Dict[str, float] = defaultdict(float)
    by_severity: Dict[str, Dict[str, float]] = defaultdict(dict)
    for row in rows:
        totals[row["clinic_name"]] += row["n"]
        by_severity[row["clinic_name"]][row["severity"]] = row["n"]
    return {
        clinic: {
            "high_pct": round(100 * by_severity[clinic].get("High", 0) / total, 1),
            "medium_pct": round(100 * by_severity[clinic].get("Medium", 0) / total, 1),
        }
        for clinic, total in totals.items()
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


def _operating_hours_by_day(
    db: Database,
    day_strs: Sequence[str],
    roles: Dict[Tuple[str, str], str],
    state: Optional[str] = None,
    cluster: Optional[str] = None,
) -> Dict[Tuple[str, str], dict]:
    """
    operating_hours() for every (clinic, day) pair with any observation in
    day_strs - one query for the whole span instead of a separate
    get_observations() call per (clinic, day). That per-day, per-clinic
    round trip was the single biggest cost of scoring a window (up to
    len(clinics) * 30 queries for "30D" alone).

    Callers scoring several WINDOWS entries in one request (a clinic/
    cluster/state page's Timelines x Metrics table) should build this once
    over the 30-day span - every other window's days are a subset of it -
    and pass it into _timeliness_scores() as ``hours_by_day``, instead of
    each window recomputing the same overlapping days from scratch: "today"
    alone would otherwise be computed fresh for Today, 3D, 7D, 14D and 30D.
    """
    placeholders = ", ".join("?" for _ in day_strs)
    clauses = [f"day IN ({placeholders})"]
    params: List[Any] = list(day_strs)
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
    obs_rows = db.conn.execute(
        f"SELECT * FROM observations WHERE {' AND '.join(clauses)} "
        "ORDER BY ts_epoch ASC",
        params,
    ).fetchall()
    by_clinic_day: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in obs_rows:
        by_clinic_day[(row["clinic_name"], row["day"])].append(dict(row))

    hours_by_day: Dict[Tuple[str, str], dict] = {}
    for (clinic, day), rows in by_clinic_day.items():
        indoor = indoor_cameras(roles, clinic)
        hours_by_day[(clinic, day)] = operating_hours(rows, indoor)
    return hours_by_day


def _timeliness_scores(
    db: Database,
    day_strs: Sequence[str],
    clinics: Sequence[str],
    roles: Dict[Tuple[str, str], str],
    state: Optional[str] = None,
    cluster: Optional[str] = None,
    hours_by_day: Optional[Dict[Tuple[str, str], dict]] = None,
) -> Dict[str, float]:
    """
    Average, over days the clinic was actually observed, of how close its
    opening and closing were to schedule - full credit inside the open/close
    tolerance windows, one point off per minute beyond them.

    ``hours_by_day`` mirrors ``roles`` on clinic_scores() - a caller scoring
    several windows in one request passes in a cache built once (see
    _operating_hours_by_day()) instead of every window recomputing it.
    """
    if hours_by_day is None:
        hours_by_day = _operating_hours_by_day(db, day_strs, roles, state, cluster)

    per_clinic: Dict[str, List[float]] = defaultdict(list)
    for clinic in clinics:
        for day in day_strs:
            hours = hours_by_day.get((clinic, day))
            if not hours:
                continue
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
    roles: Optional[Dict[Tuple[str, str], str]] = None,
    hours_by_day: Optional[Dict[Tuple[str, str], dict]] = None,
) -> Dict[str, Dict[str, Optional[float]]]:
    """
    One entry per clinic: {"timeliness", "incidents", "camera_availability",
    "overall", "high_pct", "medium_pct", "ettr_minutes"} - each None when the
    window has nothing to judge it from. The last three are supplementary
    (see the module docstring) and never feed "overall".

    ``roles`` lets a caller that scores the same clinics across several
    windows in one request (a clinic/cluster/state page's Timelines x
    Metrics table) infer camera roles once and reuse it, instead of every
    window re-running infer_roles(db.camera_descriptions()) - a fleet-wide
    scan of the events table - for a value that does not depend on the
    window at all. ``hours_by_day`` is the same idea for
    _operating_hours_by_day(): every WINDOWS entry's days are a subset of
    the 30-day one, so a caller can build it once over 30 days and every
    window looks up its own smaller day range from the same dict instead of
    recomputing today's operating hours once per window that includes it.
    Both left unset, still computed fresh here so every other caller's
    behavior is unchanged.
    """
    end, length = WINDOWS.get(window, (0, 7))
    day_strs, since_epoch, until_epoch = _window(end, length)
    clinics = _clinic_universe(db, day_strs, state, cluster)
    if not clinics:
        return {}

    incidents = _incident_scores(db, since_epoch, until_epoch, state, cluster)
    availability = _camera_availability_scores(db, day_strs, state, cluster)
    if roles is None:
        roles = infer_roles(db.camera_descriptions())
    timeliness = _timeliness_scores(
        db, day_strs, clinics, roles, state, cluster, hours_by_day
    )
    concern = _concern_pct_scores(db, since_epoch, until_epoch, state, cluster)
    ettr = _ttr_minutes(db, since_epoch, state, cluster)

    result: Dict[str, Dict[str, Optional[float]]] = {}
    for clinic in clinics:
        core = {
            "timeliness": timeliness.get(clinic),
            "incidents": incidents.get(clinic, 100.0 if clinic in availability else None),
            "camera_availability": availability.get(clinic),
        }
        present = [v for v in core.values() if v is not None]
        result[clinic] = {
            **core,
            "overall": round(sum(present) / len(present), 1) if present else None,
            "high_pct": concern.get(clinic, {}).get("high_pct"),
            "medium_pct": concern.get(clinic, {}).get("medium_pct"),
            "ettr_minutes": ettr.get(clinic),
        }
    return result


_METRIC_KEYS = (
    "timeliness", "incidents", "camera_availability", "overall",
    "high_pct", "medium_pct", "ettr_minutes",
)


def group_metrics(
    scores: Dict[str, Dict[str, Optional[float]]],
) -> Dict[str, Optional[float]]:
    """
    Every metric averaged across a set of already-computed per-clinic
    clinic_scores() rows - one cluster's or state's own row for the same
    Timelines x Metrics view a single clinic's page shows, without
    recomputing anything: each clinic already carries its own numbers,
    this just averages column-wise instead of picking one clinic's.

    Generalizes group_average() (which only ever averaged "overall") to
    every key so a group page can show the same metric columns a clinic's
    own page does.
    """
    if not scores:
        # Every key present and None, not an empty dict - a brand new
        # cluster/state with no scoreable clinics yet in this window still
        # has to look like a row with nothing to show, not a row missing
        # the columns entirely (which crashed the template the one time
        # this happened for real: Bolangir's first day, before it had any
        # data in most windows).
        return {key: None for key in _METRIC_KEYS}
    keys = next(iter(scores.values())).keys()
    result: Dict[str, Optional[float]] = {}
    for key in keys:
        values = [v[key] for v in scores.values() if v.get(key) is not None]
        result[key] = round(sum(values) / len(values), 1) if values else None
    return result


def clinic_locations(db: Database) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """
    clinic_name -> (state, cluster), the most recently tagged row across
    both observations and clinic_status - used to group an already-computed,
    ungrouped clinic_scores() result by cluster/state without recomputing
    scores per group.

    Both tables, not just observations: a clinic offline since before its
    first successful camera check has zero observations rows, but
    clinic_status still tags state/cluster on every patrol lap regardless
    of whether the check itself succeeded - observations alone left exactly
    these permanently-unreachable clinics "Unassigned" forever, which is
    precisely when the Offline Clinics page most needs to know their
    cluster.

    Most-recent, not most-common: a clinic patrolled since before
    CM_STATE_NAME/CM_CLUSTER_NAME existed has thousands of old untagged
    rows outnumbering its correctly-tagged recent ones, so picking by
    volume would keep it unassigned forever even though every current row
    already carries the right state/cluster.
    """
    rows = db.conn.execute(
        "SELECT clinic_name, state, cluster, ts_epoch FROM observations "
        "WHERE state IS NOT NULL AND state != '' "
        "AND cluster IS NOT NULL AND cluster != '' "
        "UNION ALL "
        "SELECT clinic_name, state, cluster, ts_epoch FROM clinic_status "
        "WHERE state IS NOT NULL AND state != '' "
        "AND cluster IS NOT NULL AND cluster != '' "
        "ORDER BY clinic_name, ts_epoch DESC"
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


