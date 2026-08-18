"""
Daily operational report per clinic.

Reads the observations a patrol recorded during the day and writes one report
per clinic covering operating hours, occupancy, the checkup area, and camera
health.

    report.bat                     today, every clinic observed
    report.bat 2026-08-11
    report.py --day 2026-08-11 --clinic "CUREBAY BANAMALIPUR"

Reports are written to ``reports/<date>_<clinic>.md`` and printed.

A note on what these numbers are
--------------------------------
A patrol samples roughly one minute per clinic per lap, so it sees a small
fraction of the day. Everything here is therefore split into:

* **Confirmed** - directly observed in a frame (e.g. "4 people were visible at
  once at 11:20"). A floor, never a total.
* **Estimated** - inferred across gaps, with the assumption stated.

Unique visitor counting is **not** attempted. Counting people entering without
double-counting needs continuous video and object tracking across frames;
sampled snapshots cannot support it, so no such figure is produced. Occupancy
observations are given instead, with an explicit range.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from analysis.camera_health import FROZEN_DIFF, HealthStatus  # noqa: E402
from analysis.camera_role import indoor_cameras, infer_roles  # noqa: E402
from storage.database import Database  # noqa: E402

REPORT_DIR = config.BASE_DIR / "reports"

# Expected schedule, used only to say whether opening/closing matched it.
EXPECTED_OPEN = config.EXPECTED_OPEN
EXPECTED_CLOSE = config.EXPECTED_CLOSE
EXPECTED_LUNCH = (config.EXPECTED_LUNCH_START, config.EXPECTED_LUNCH_END)

CROWDING_THRESHOLD = config.CROWDING_PERSONS
INACTIVITY_MINUTES = config.INACTIVITY_MINUTES
LUNCH_MIN_MINUTES = config.LUNCH_MIN_MINUTES


def _time(row: dict) -> datetime:
    return datetime.fromtimestamp(row["ts_epoch"])


def _hhmm(when: Optional[datetime]) -> str:
    return when.strftime("%H:%M") if when else "not observed"


def _active(row: dict) -> bool:
    """Did this observation show the clinic being used?"""
    return (row.get("max_persons") or 0) > 0 or (row.get("motion_frames") or 0) > 0


def effective_status(row: dict) -> Optional[str]:
    """
    Re-derive the health verdict from the stored measurements.

    Verdicts recorded before the frozen-feed rule took priority can say
    "obstructed" for a stream that had simply stalled. The raw numbers are
    stored, so the conclusion is recomputed here instead of trusting the label
    written at the time - old reports then correct themselves.
    """
    status = row.get("health_status")
    change = row.get("frame_change")
    if (
        status
        and status != HealthStatus.NO_SIGNAL.value
        and change is not None
        and change < FROZEN_DIFF
    ):
        return HealthStatus.FROZEN.value
    return status


def _usable(row: dict) -> bool:
    """Whether the camera was working well enough to believe."""
    return effective_status(row) in (
        None,
        HealthStatus.OK.value,
        HealthStatus.NEEDS_CLEANING.value,
    )


def visit_times(rows: Sequence[dict], gap_seconds: float = 180.0) -> List[float]:
    """
    Collapse per-camera rows into the patrol visits they came from.

    Every camera in a visit is recorded a few seconds apart, so counting
    distinct timestamps counts cameras, not visits - which made a single
    4-camera visit look like four checks seconds apart, and in turn made the
    reported timing resolution nonsense.
    """
    stamps = sorted(r["ts_epoch"] for r in rows)
    visits: List[float] = []
    for stamp in stamps:
        if not visits or stamp - visits[-1] > gap_seconds:
            visits.append(stamp)
    return visits


# --------------------------------------------------------------------------- #
# 1. Operating hours
# --------------------------------------------------------------------------- #
def operating_hours(
    rows: Sequence[dict], indoor: Optional[set] = None
) -> dict:
    """
    Infer opening, closing and the lunch break from when activity was seen.

    These are bounded by the patrol's sampling: the clinic opened *at or
    before* the first activity seen, and closed *at or after* the last. The
    gap between visits is reported so the reader knows the margin.

    When the indoor camera is known, only it is used. Activity on an outdoor
    camera says nothing about whether the clinic is working - someone walking
    past at 21:00 would otherwise extend the day's "closing time" by hours.
    """
    usable = [r for r in rows if _usable(r)]
    scope = [r for r in usable if r["camera_name"] in indoor] if indoor else []
    used_indoor = bool(scope)
    active = [r for r in (scope or usable) if _active(r)]
    visits = visit_times(usable)

    gaps = [b - a for a, b in zip(visits, visits[1:])] if len(visits) > 1 else []
    typical_gap = median(gaps) / 60 if gaps else 0.0

    if not active:
        return {
            "opened": None, "closed": None, "lunch": None,
            "resolution_min": typical_gap, "first_seen": None, "last_seen": None,
            "used_indoor": used_indoor, "basis": sorted(indoor) if indoor else [],
            "note": "no activity was observed all day",
        }

    opened = _time(active[0])
    closed = _time(active[-1])

    # Lunch: the longest quiet stretch between two active observations that
    # falls inside the middle of the day.
    lunch: Optional[Tuple[datetime, datetime]] = None
    longest = timedelta(0)
    for previous, following in zip(active, active[1:]):
        gap_start, gap_end = _time(previous), _time(following)
        gap = gap_end - gap_start
        midday = 11 <= gap_start.hour <= 16
        if midday and gap > longest and gap >= timedelta(minutes=LUNCH_MIN_MINUTES):
            longest, lunch = gap, (gap_start, gap_end)

    return {
        "opened": opened,
        "closed": closed,
        "lunch": lunch,
        "resolution_min": typical_gap,
        "first_seen": _time(usable[0]),
        "last_seen": _time(usable[-1]),
        "used_indoor": used_indoor,
        "basis": sorted(indoor) if indoor else [],
        "note": "",
    }


def _expected_at(day: datetime, expected: str) -> datetime:
    hour, minute = (int(p) for p in expected.split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _verdict(
    actual: Optional[datetime],
    expected: str,
    watched_from: Optional[datetime],
    watched_to: Optional[datetime],
    opening: bool,
) -> str:
    """
    Judge opening/closing against the schedule - but only when the monitoring
    actually covered the relevant time.

    Without this guard a patrol started at 16:00 reports "opened 7 hours late",
    which says nothing about the clinic and everything about when we happened
    to be watching. Coverage gaps must read as unknown, not as a finding.
    """
    if actual is None or watched_from is None or watched_to is None:
        return "not observed"

    target = _expected_at(actual, expected)
    delta = (actual - target).total_seconds() / 60

    if opening:
        if watched_from > target:
            return (
                f"cannot tell - monitoring only began at {_hhmm(watched_from)}, "
                f"after the expected {expected}"
            )
        if abs(delta) <= config.SCHEDULE_TOLERANCE_MINUTES:
            return "on time"
        return f"{abs(delta):.0f} min {'late' if delta > 0 else 'early'}"

    # Closing. Seeing activity after the expected close is a real finding, but
    # it says the clinic had *not* closed - not that we watched it close. And
    # seeing no activity late only means something if we were still watching.
    if delta > config.SCHEDULE_TOLERANCE_MINUTES:
        return f"still active {delta:.0f} min after the expected {expected}"
    if watched_to < target:
        return (
            f"cannot tell - monitoring stopped at {_hhmm(watched_to)}, "
            f"before the expected {expected}"
        )
    if abs(delta) <= config.SCHEDULE_TOLERANCE_MINUTES:
        return "on time"
    return f"quiet from {_hhmm(actual)}, {abs(delta):.0f} min before {expected}"


# --------------------------------------------------------------------------- #
# 2. Occupancy (what can honestly be said instead of a visitor count)
# --------------------------------------------------------------------------- #
def occupancy(rows: Sequence[dict]) -> dict:
    """
    Occupancy observations, with confirmed and estimated figures kept apart.

    * ``peak`` - most people visible in one frame. Directly observed.
    * ``lower_bound`` - the largest single-frame count, i.e. the day certainly
      had at least this many people in it.
    * ``upper_estimate`` - the sum of per-visit peaks, which assumes complete
      turnover between visits and so cannot be exceeded by the true count of
      people seen. Almost always an over-count.

    The truth lies between them, and neither is a visitor total.
    """
    usable = [r for r in rows if _usable(r) and (r.get("max_persons") or 0) > 0]
    if not usable:
        return {
            "peak": 0, "peak_at": None, "peak_camera": None,
            "lower_bound": 0, "upper_estimate": 0,
            "hourly": {}, "visits_with_people": 0, "coverage_min": 0.0,
        }

    peak_row = max(usable, key=lambda r: r["max_persons"])
    hourly: Dict[int, int] = defaultdict(int)
    for row in usable:
        hour = _time(row).hour
        hourly[hour] = max(hourly[hour], row["max_persons"])

    # Sum per visit (not per camera) so two cameras seeing the same person in
    # the same visit are not added together twice.
    by_visit: Dict[Tuple[int, str], int] = defaultdict(int)
    for row in usable:
        key = (int(row["ts_epoch"] // 300), row["clinic_name"])   # 5-minute bucket
        by_visit[key] = max(by_visit[key], row["max_persons"])

    watched_seconds = sum((r.get("frames") or 0) for r in rows) * config.CAPTURE_INTERVAL_SEC

    return {
        "peak": peak_row["max_persons"],
        "peak_at": _time(peak_row),
        "peak_camera": peak_row["camera_name"],
        "lower_bound": peak_row["max_persons"],
        "upper_estimate": sum(by_visit.values()),
        "hourly": dict(sorted(hourly.items())),
        "visits_with_people": len(by_visit),
        "coverage_min": watched_seconds / 60,
    }


# --------------------------------------------------------------------------- #
# 3. Checkup area
# --------------------------------------------------------------------------- #
def checkup_area(rows: Sequence[dict], hours: dict) -> dict:
    """
    Assess the camera that behaves like the checkup/consultation area.

    The busiest indoor camera is used, since no camera is labelled by role.
    Only observable facts are reported - never anything about why people are
    there or what condition they are in.
    """
    usable = [r for r in rows if _usable(r)]
    if not usable:
        return {"camera": None, "findings": ["no usable camera footage"]}

    totals: Dict[str, int] = defaultdict(int)
    for row in usable:
        totals[row["camera_name"]] += row.get("max_persons") or 0
    camera = max(totals, key=totals.get) if any(totals.values()) else usable[0]["camera_name"]

    area = [r for r in usable if r["camera_name"] == camera]
    active = [r for r in area if _active(r)]
    findings: List[str] = []

    if active:
        findings.append(
            f"activity observed on {len(active)} of {len(area)} checks"
        )
    else:
        findings.append("no activity observed on this camera all day")

    crowded = [r for r in area if (r.get("max_persons") or 0) >= CROWDING_THRESHOLD]
    if crowded:
        worst = max(crowded, key=lambda r: r["max_persons"])
        findings.append(
            f"unusually crowded {len(crowded)}x - peak {worst['max_persons']} "
            f"people at {_hhmm(_time(worst))} (threshold {CROWDING_THRESHOLD})"
        )

    # Prolonged inactivity only counts between opening and closing: an empty
    # room before opening is not a finding.
    if hours.get("opened") and hours.get("closed"):
        window = [r for r in area if hours["opened"] <= _time(r) <= hours["closed"]]
        quiet_start = None
        for row in window:
            if _active(row):
                quiet_start = None
                continue
            if quiet_start is None:
                quiet_start = _time(row)
            elif (_time(row) - quiet_start) >= timedelta(minutes=INACTIVITY_MINUTES):
                findings.append(
                    f"no activity for over {INACTIVITY_MINUTES} min during "
                    f"opening hours, from {_hhmm(quiet_start)} to {_hhmm(_time(row))}"
                )
                quiet_start = None

    unusual = [r for r in area if r.get("unusual")]
    for row in unusual:
        findings.append(f"flagged at {_hhmm(_time(row))}: {row.get('description', '')}")

    return {"camera": camera, "findings": findings, "checks": len(area)}


# --------------------------------------------------------------------------- #
# 4. Camera health
# --------------------------------------------------------------------------- #
def camera_health(rows: Sequence[dict]) -> List[dict]:
    """Per-camera verdict, using the status seen on most checks."""
    by_camera: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_camera[row["camera_name"]].append(row)

    report = []
    for camera, camera_rows in sorted(by_camera.items()):
        counts: Dict[str, int] = defaultdict(int)
        for row in camera_rows:
            counts[effective_status(row) or "unknown"] += 1
        worst = max(counts, key=counts.get)
        try:
            status = HealthStatus(worst)
        except ValueError:
            status = None
        report.append(
            {
                "camera": camera,
                "status": status,
                "raw": worst,
                "checks": len(camera_rows),
                "matching": counts[worst],
                "label": status.label if status else "not assessed",
                "action": status.action if status else "",
            }
        )
    return report


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def build_report(
    clinic: str, day: str, rows: Sequence[dict], indoor: Optional[set] = None
) -> str:
    hours = operating_hours(rows, indoor)
    occ = occupancy(rows)
    checkup = checkup_area(rows, hours)
    health = camera_health(rows)
    checks = len(visit_times(rows))

    out: List[str] = []
    add = out.append

    add(f"# {clinic} - daily report")
    add(f"\n**Date:** {day}  ")
    add(f"**Patrol checks:** {checks}  ")
    add(f"**Cameras seen:** {len({r['camera_name'] for r in rows})}  ")
    add(f"**Report generated:** {datetime.now():%Y-%m-%d %H:%M}")

    # -- 1. hours --
    add("\n## 1. Operating hours\n")
    if hours["note"]:
        add(f"{hours['note']}.\n")
    add(f"Monitoring covered **{_hhmm(hours['first_seen'])} to "
        f"{_hhmm(hours['last_seen'])}**. Nothing outside that window was seen.\n")
    if hours.get("used_indoor"):
        add(f"Times below come from the indoor camera "
            f"({', '.join(hours['basis'])}) only. Activity outdoors - someone "
            f"walking past in the evening - says nothing about whether the "
            f"clinic is working.\n")
    else:
        add("_No indoor camera has been identified for this clinic yet, so "
            "these times use every working camera and may be stretched by "
            "passers-by outside._\n")
    add("| | Observed | Expected | Verdict |")
    add("| --- | --- | --- | --- |")
    add(f"| Opened (first activity seen) | {_hhmm(hours['opened'])} | {EXPECTED_OPEN} | "
        f"{_verdict(hours['opened'], EXPECTED_OPEN, hours['first_seen'], hours['last_seen'], True)} |")
    add(f"| Closed (last activity seen) | {_hhmm(hours['closed'])} | {EXPECTED_CLOSE} | "
        f"{_verdict(hours['closed'], EXPECTED_CLOSE, hours['first_seen'], hours['last_seen'], False)} |")
    if hours["lunch"]:
        start, end = hours["lunch"]
        add(f"| Lunch | {_hhmm(start)} - {_hhmm(end)} | "
            f"{EXPECTED_LUNCH[0]} - {EXPECTED_LUNCH[1]} | "
            f"{(end - start).total_seconds() / 60:.0f} min quiet |")
    else:
        add(f"| Lunch | not identified | {EXPECTED_LUNCH[0]} - {EXPECTED_LUNCH[1]} "
            f"| no midday gap over {LUNCH_MIN_MINUTES} min |")

    if hours["opened"]:
        gap = (
            f"Checks were about {hours['resolution_min']:.0f} minutes apart, which "
            "is the accuracy of every time above."
            if hours["resolution_min"] >= 1
            else "Only one check was made, so these are single observations rather "
            "than a timeline."
        )
        add(
            f"\nThese are bounds, not exact times: activity was first seen at "
            f"{_hhmm(hours['opened'])} and last seen at {_hhmm(hours['closed'])}, so "
            f"the clinic was open at least between those. {gap}"
        )

    # -- 2. occupancy --
    add("\n## 2. Occupancy\n")
    add("**No unique visitor total is given.** Counting people entering without "
        "double-counting requires continuous video and tracking between frames; "
        "this system samples snapshots, so any single total would be invented. "
        "What was actually seen:\n")
    add("| Measure | Value | Confidence |")
    add("| --- | --- | --- |")
    add(f"| Most people visible at once | **{occ['peak']}** "
        f"{'at ' + _hhmm(occ['peak_at']) + ' on ' + str(occ['peak_camera']) if occ['peak'] else ''} "
        f"| confirmed - seen in one frame |")
    add(f"| At least this many people present today | **{occ['lower_bound']}** "
        f"| confirmed - a floor, not a total |")
    add(f"| At most this many people seen | **{occ['upper_estimate']}** "
        f"| estimated - sums each check's peak, assuming nobody was counted "
        f"twice across checks; usually an over-count |")
    add(f"| Checks with people present | {occ['visits_with_people']} of {checks} "
        f"| confirmed |")
    add(f"| Time actually watched | {occ['coverage_min']:.0f} min | "
        f"confirmed - the rest of the day was not observed |")

    if occ["hourly"]:
        add("\n**Peak people visible, by hour** (confirmed observations):\n")
        add("| Hour | Peak |")
        add("| --- | --- |")
        for hour, peak in occ["hourly"].items():
            add(f"| {hour:02d}:00 | {'#' * min(peak, 20)} {peak} |")

    # -- 3. checkup area --
    add("\n## 3. Checkup area\n")
    if checkup["camera"]:
        add(f"Assessed from **{checkup['camera']}** (the busiest camera; "
            f"{checkup.get('checks', 0)} checks).\n")
    for finding in checkup["findings"]:
        add(f"- {finding}")
    add("\nOnly what the camera showed is reported here - no inference about "
        "anyone's condition or reason for being present.")

    # -- 4. camera health --
    add("\n## 4. Camera health\n")
    add("| Camera | Status | Seen on | Action |")
    add("| --- | --- | --- | --- |")
    for item in health:
        mark = "OK" if item["status"] is HealthStatus.OK else "**FAULT**"
        add(f"| {item['camera']} | {mark} - {item['label']} | "
            f"{item['matching']}/{item['checks']} checks | {item['action'] or '-'} |")

    faults = [h for h in health if h["status"] and h["status"].is_problem]
    if faults:
        add(f"\n**{len(faults)} camera(s) need attention.** A faulty camera reports "
            "calm forever, so anything it covers was not monitored today.")
    else:
        add("\nAll cameras were working.")

    return "\n".join(out) + "\n"


def clinic_slug(name: str) -> str:
    """Folder-safe form of a clinic name."""
    return "_".join("".join(c if c.isalnum() else " " for c in name).split()) or "clinic"


def report_path(clinic: str, day: str) -> Path:
    """Each clinic keeps its own folder, one file per day."""
    return REPORT_DIR / clinic_slug(clinic) / f"{day}.md"


def generate(clinic: str, day: str, db: Optional[Database] = None) -> Optional[Path]:
    """Build and save one clinic's report. None when there is no data."""
    db = db or Database()
    rows = db.get_observations(day, clinic)
    if not rows:
        return None
    roles = infer_roles(db.camera_descriptions())
    path = report_path(clinic, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_report(clinic, day, rows, indoor_cameras(roles, clinic)),
        encoding="utf-8",
    )
    return path


def generate_all(day: str, db: Optional[Database] = None) -> List[Path]:
    """Refresh every clinic that has observations for the day."""
    db = db or Database()
    written = []
    for clinic in db.observed_clinics(day):
        path = generate(clinic, day, db)
        if path:
            written.append(path)
    return written


def available_reports() -> Dict[str, List[str]]:
    """{clinic folder name: [days, newest first]} from what is on disk."""
    found: Dict[str, List[str]] = {}
    if not REPORT_DIR.exists():
        return found
    for folder in sorted(REPORT_DIR.iterdir()):
        if not folder.is_dir():
            continue
        days = sorted((f.stem for f in folder.glob("*.md")), reverse=True)
        if days:
            found[folder.name] = days
    return found


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="daily clinic report")
    parser.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"),
                        help="date to report on (YYYY-MM-DD), default today")
    parser.add_argument("--clinic", default="all", help="one clinic, or all")
    parser.add_argument("--list-days", action="store_true",
                        help="show which days have data")
    parser.add_argument("--quiet", action="store_true",
                        help="write the files without printing them")
    args = parser.parse_args(argv)

    db = Database()
    if args.list_days:
        days = db.observed_days()
        print("days with patrol data:" if days else "no patrol data recorded yet")
        for day in days:
            print(f"  {day}  ({len(db.observed_clinics(day))} clinics)")
        return 0

    clinics = (
        db.observed_clinics(args.day)
        if args.clinic == "all"
        else [args.clinic]
    )
    if not clinics:
        print(f"no observations recorded for {args.day}.")
        print("Run a patrol first:  patrol.bat 60")
        return 1

    for clinic in clinics:
        path = generate(clinic, args.day, db)
        if path is None:
            print(f"no data for {clinic} on {args.day}")
            continue
        if args.quiet:
            print(f"wrote {path}")
        else:
            print(f"\n{path.read_text(encoding='utf-8')}\n{'-' * 72}\nsaved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
