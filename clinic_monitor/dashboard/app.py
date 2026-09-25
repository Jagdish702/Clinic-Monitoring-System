"""
Stage 6 - dashboard.

A small Flask app that lists alerts newest-first with severity / clinic
filters and the evidence screenshot for each event.

    python dashboard/app.py        (or: python -m dashboard.app)
    -> http://127.0.0.1:8000
"""

from __future__ import annotations

import csv
import io
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    send_from_directory, session, url_for,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import config  # noqa: E402
import report as reporting  # noqa: E402
from analysis import incidents as incidents_lib  # noqa: E402
from analysis import scoring  # noqa: E402
from analysis.camera_role import indoor_cameras, infer_roles  # noqa: E402
from dashboard.render import markdown_to_html  # noqa: E402
from dashboard.workbook import build_offline_workbook, build_workbook  # noqa: E402
from storage.database import Database, ignored_clause  # noqa: E402

SEVERITIES = ("High", "Medium", "Low")

# One color per category, for the category-distribution pie chart - grouped
# into color families by analysis.incidents.CATEGORY_GROUPS (Staff behavior:
# purple/blue, Clinic infrastructure: green/teal, Camera-related: gold,
# Emergency: red) so the legend still reads as coherent groups even with
# every individual category as its own slice.
_CATEGORY_COLORS: Dict[str, str] = {
    "staff_apron": "#6c5ce7",
    "staff_grooming": "#a29bfe",
    "staff_badge": "#4834d4",
    "staff_head_cover": "#7f78d2",
    "ppe_sample_collection": "#341f97",
    "staff_phone": "#535c68",
    "staff_eating": "#8395a7",
    "parking_area": "#00b894",
    "compound_wall": "#00cec9",
    "reception_cleanliness": "#20bf6b",
    "medical_waste": "#0fb9b1",
    "sample_area_hygiene": "#26de81",
    "pharmacy_organization": "#10ac84",
    "hand_sanitizer": "#55efc4",
    "camera_health": "#e1b12c",
    "emergency": "#d82525",
}
_CATEGORY_COLOR_FALLBACK = "#808080"


def create_app(db_path: Optional[Path] = None) -> Flask:
    app = Flask(__name__)
    app.config["SCREENSHOT_DIR"] = str(Path(config.SCREENSHOT_DIR).resolve())
    app.config["DB_PATH"] = str(db_path or config.DB_PATH)
    database = Database(Path(app.config["DB_PATH"]))
    auth.init_auth(app, database)

    def _scope_state_cluster(state: str, cluster: str):
        """For a logged-in State/Cluster Manager, force the query's state/
        cluster filter to their own scope, regardless of what was asked for.
        Forcing just the more specific one (cluster for a cluster_manager,
        state for a state_manager) is sufficient - state/cluster are ANDed
        together everywhere downstream, so an additional coarser filter can
        only narrow a result further, never broaden past what the forced
        one already pins. A no-op when auth is disabled or the user is
        Admin/Command Center."""
        user = auth.current_user(database)
        if not user or user["role"] in auth.UNRESTRICTED_ROLES:
            return state, cluster
        if user["role"] == "state_manager":
            return user["state"], cluster
        if user["role"] == "cluster_manager":
            return state, user["cluster"]
        return "all", "all"  # unrecognized role - fail closed, not open

    def _filters():
        severity = request.args.get("severity", "all")
        clinic = request.args.get("clinic", "all")
        camera = request.args.get("camera", "all")
        # "all" at this level, same convention as clinic/camera above, means
        # "every state" / "every cluster in the selected state" - not a
        # literal value to match against.
        state = request.args.get("state", "all")
        cluster = request.args.get("cluster", "all")
        state, cluster = _scope_state_cluster(state, cluster)
        try:
            limit = min(int(request.args.get("limit", config.DASHBOARD_PAGE_SIZE)), 500)
        except ValueError:
            limit = config.DASHBOARD_PAGE_SIZE
        hours = request.args.get("hours")
        since = None
        if hours:
            try:
                since = time.time() - float(hours) * 3600
            except ValueError:
                since = None
        return severity, clinic, camera, state, cluster, limit, since

    # -- auth ---------------------------------------------------------------#
    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        error = None
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            user = database.get_user_by_email(email)
            if user and auth.verify_password(user["password_hash"], password):
                session.clear()
                session["user_id"] = user["id"]
                dest = request.args.get("next") or url_for("index")
                return redirect(dest)
            error = "Incorrect email or password."
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login_page"))

    # -- pages ------------------------------------------------------------- #
    @app.route("/")
    def index():
        severity, clinic, camera, state, cluster, limit, since = _filters()
        events = _annotate(
            database.get_events(
                severity=severity,
                clinic_name=clinic,
                camera_name=camera,
                state=state,
                cluster=cluster,
                since_epoch=since,
                limit=limit,
            )
        )
        total_matching = database.count_events(
            severity=severity, clinic_name=clinic, camera_name=camera,
            state=state, cluster=cluster, since_epoch=since,
        )
        # Each level narrowed by the one above it - state=all/cluster=all
        # (query param unset) is treated as "no filter" by group_counts(), so
        # a fresh page load with nothing picked yet shows every clinic exactly
        # as it did before this feature existed. states comes back empty on
        # a deployment that has never set CM_STATE_NAME/CM_CLUSTER_NAME
        # anywhere, and the template hides the whole state/cluster row in
        # that case - not a partial, confusing hierarchy with one entry.
        states = database.group_counts("state")
        clusters = (
            database.group_counts("cluster", state=state) if state != "all" else []
        )
        clinics = database.group_counts(
            "clinic_name",
            state=None if state == "all" else state,
            cluster=None if cluster == "all" else cluster,
        )

        day = _today()
        summary = database.dashboard_summary(day)
        # Opened/closed/offline (report.daily_summary()'s own three-way
        # status, not clinics_online/offline above which is about device
        # connectivity, not whether staff were ever seen) - fleet-wide, no
        # state/cluster filter, since this is the main dashboard's own
        # stat card. Computed once per real page load (index() isn't on
        # the 5s auto-refresh poll - only /api/events is), same cost class
        # as dashboard_summary() just above it.
        day_rows = reporting.daily_summary(day, db=database)
        summary["clinics_opened_today"] = sum(1 for r in day_rows if r["status"] == "opened")
        summary["clinics_closed_today"] = sum(1 for r in day_rows if r["status"] == "closed")
        summary["clinics_checked_today"] = len(day_rows)

        return render_template(
            "index.html",
            events=events,
            total_matching=total_matching,
            counts=database.counts_by_severity(),
            summary=summary,
            states=states,
            clusters=clusters,
            clinics=clinics,
            severity=severity,
            clinic=clinic,
            state=state,
            cluster=cluster,
            severities=SEVERITIES,
            expected_open=config.EXPECTED_OPEN,
            refresh=config.DASHBOARD_REFRESH_SEC,
        )

    @app.route("/scores")
    def scores_page():
        window = request.args.get("window", "Today")
        if window not in scoring.WINDOWS:
            window = "Today"
        state = request.args.get("state", "all")
        cluster = request.args.get("cluster", "all")
        state, cluster = _scope_state_cluster(state, cluster)

        # Computed once, ungrouped - the State -> Cluster -> Clinic tree
        # below groups this in Python instead of recomputing scores per
        # group, so the tree costs a handful of small group_counts() lookups
        # (one per state, one per cluster) on top of it, not a rescan.
        all_scores = scoring.clinic_scores(database, window=window)

        states = database.group_counts("state")
        clusters = database.group_counts("cluster", state=None if state == "all" else state)
        clinics = database.group_counts(
            "clinic_name",
            state=None if state == "all" else state,
            cluster=None if cluster == "all" else cluster,
        )

        # One State -> Cluster -> Clinic breakdown, narrowed to whatever the
        # sidebar currently has selected. Each level's average rolls up from
        # its own children (group_average), not a separate recompute.
        tree = []
        for srow in states:
            sname = srow["value"]
            if state != "all" and sname != state:
                continue
            cluster_nodes = []
            for crow in database.group_counts("cluster", state=sname):
                cname = crow["value"]
                if cluster != "all" and cname != cluster:
                    continue
                clinic_names = {
                    c["value"] for c in
                    database.group_counts("clinic_name", state=sname, cluster=cname)
                }
                clinic_rows = sorted(
                    (
                        {"clinic_name": name, **cats}
                        for name, cats in all_scores.items()
                        if name in clinic_names
                    ),
                    key=lambda r: (r["overall"] is None, r["overall"] or 0),
                )
                cluster_scores = {r["clinic_name"]: r for r in clinic_rows}
                cluster_nodes.append({
                    "name": cname,
                    "average": scoring.group_average(cluster_scores),
                    "clinics": clinic_rows,
                })
            cluster_nodes.sort(key=lambda c: (c["average"] is None, c["average"] or 0))
            state_scores = {
                r["clinic_name"]: r for c in cluster_nodes for r in c["clinics"]
            }
            tree.append({
                "name": sname,
                "average": scoring.group_average(state_scores),
                "clusters": cluster_nodes,
            })
        tree.sort(key=lambda s: (s["average"] is None, s["average"] or 0))

        # Fast Moving: top 3 clinics with a High/Medium ticket opened in the
        # last 5 minutes - "what's happening right now". Slow Moving:
        # every clinic with one opened in the last hour, uncapped - serious
        # problems that have stayed open rather than resolving quickly.
        # Both are the same query at a different window (see
        # analysis.incidents.most_problematic_clinics's docstring), ranked
        # by severity then by how recently the ticket was created.
        fast_moving = incidents_lib.most_problematic_clinics(
            database, window_seconds=5 * 60, limit=3
        )
        slow_moving = incidents_lib.most_problematic_clinics(
            database, window_seconds=60 * 60
        )

        return render_template(
            "scores.html",
            window=window,
            windows=list(scoring.WINDOWS.keys()),
            state=state,
            cluster=cluster,
            states=states,
            clusters=clusters,
            clinics=clinics,
            tree=tree,
            fast_moving=fast_moving,
            slow_moving=slow_moving,
            refresh=config.DASHBOARD_REFRESH_SEC,
        )

    # -- api --------------------------------------------------------------- #
    # Camera roles change rarely and cost a scan of the descriptions, so they
    # are worked out once in a while rather than on every poll.
    _roles = {"data": {}, "at": 0.0}

    def _camera_roles():
        if time.time() - _roles["at"] > 120:
            _roles["data"] = infer_roles(database.camera_descriptions())
            _roles["at"] = time.time()
        return _roles["data"]

    # A clinic/cluster/state page scores every clinic in scope across six
    # windows, fresh, on every single request - the roles/hours caches
    # above only share work within one request, not across them. On a
    # state page (all of a state's clinics, potentially dozens) that's
    # several seconds of real query and render work every time, even for
    # the same page loaded twice in a row (a refresh, the sidebar arrow,
    # someone else opening the same cluster). Caching the rendered page
    # itself for a short while - long enough to make repeat visits
    # instant, short enough that a monitoring dashboard already built
    # around "roughly one patrol lap" accuracy never shows meaningfully
    # stale data - covers all of that at once instead of chasing every
    # remaining query one at a time.
    _PAGE_CACHE_TTL = 45
    _page_cache: dict = {}

    def _cached_page(key: str, build):
        now = time.time()
        hit = _page_cache.get(key)
        if hit and now - hit[0] < _PAGE_CACHE_TTL:
            return hit[1]
        html = build()
        _page_cache[key] = (now, html)
        return html

    def _with_low_pct(metrics: dict) -> dict:
        """
        Add "low_pct" (the remainder of the High/Medium split) to one
        timeline_metrics window entry, for the severity-distribution pie
        chart - high_pct + medium_pct + low_pct always sums to 100 for a
        window with any data. Rounding two independently-rounded
        percentages can push the remainder a hair below 0 (e.g. 60.0 +
        40.1), so it's floored at 0 rather than shown as a small negative
        slice.
        """
        high, medium = metrics.get("high_pct"), metrics.get("medium_pct")
        if high is None or medium is None:
            metrics["low_pct"] = None
        else:
            metrics["low_pct"] = round(max(0.0, 100.0 - high - medium), 1)
        return metrics

    def _category_pie(concerns: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Turn a top_concerns()-shaped list into a category-distribution pie
        chart: every category present (not just the top few), as one
        CSS conic-gradient stop list plus a legend with each category's
        share to one decimal. "normal" is never in ``concerns`` in the
        first place (top_concerns() already excludes it - see its own
        docstring) so this is concern categories only, same as "Top areas
        of concern" right above it.
        """
        total = sum(c["count"] for c in concerns)
        if not total:
            return {"gradient": None, "legend": []}
        legend = []
        stops = []
        cursor = 0.0
        for c in concerns:
            pct = round(100 * c["count"] / total, 1)
            color = _CATEGORY_COLORS.get(c["category"], _CATEGORY_COLOR_FALLBACK)
            start = cursor
            cursor += 100 * c["count"] / total
            stops.append(f"{color} {start:.4f}% {cursor:.4f}%")
            legend.append({
                "category": c["category"], "category_group": c["category_group"],
                "count": c["count"], "pct": pct, "color": color,
            })
        return {"gradient": "conic-gradient(" + ", ".join(stops) + ")", "legend": legend}

    def _clinic_location(clinic_name: str):
        """
        One clinic's (state, cluster), the most recent tag from either
        observations or clinic_status. A scoped version of
        scoring.clinic_locations() for callers (clinic_page()) that only
        need one clinic's answer - that function computes the whole fleet's
        map and sorts it, which is wasted work when only one entry is ever
        read from the result.
        """
        row = database.conn.execute(
            "SELECT state, cluster FROM ("
            "  SELECT state, cluster, ts_epoch FROM observations "
            "  WHERE clinic_name = ? AND state IS NOT NULL AND state != '' "
            "  AND cluster IS NOT NULL AND cluster != '' "
            "  UNION ALL "
            "  SELECT state, cluster, ts_epoch FROM clinic_status "
            "  WHERE clinic_name = ? AND state IS NOT NULL AND state != '' "
            "  AND cluster IS NOT NULL AND cluster != ''"
            ") ORDER BY ts_epoch DESC LIMIT 1",
            (clinic_name, clinic_name),
        ).fetchone()
        return (row["state"], row["cluster"]) if row else (None, None)

    def _annotate(events):
        """
        Tag each event with its camera's role, and drop the open/closed verdict
        from outdoor cameras.

        An outdoor camera cannot see whether the clinic is open. Measured here,
        the outdoor camera reported "Closed" while the indoor one reported
        "Open" at the same minute, at four different clinics - so showing both
        with equal weight actively misleads. The stored value is untouched;
        only the display drops it.
        """
        roles = _camera_roles()
        for event in events:
            role = roles.get((event["clinic_name"], event["camera_name"]))
            event["camera_role"] = role
            if role == "outdoor" and event.get("clinic_status"):
                event["clinic_status_raw"] = event["clinic_status"]
                event["clinic_status"] = None
            # A real date and time, not the raw stored ISO string (which
            # carries whatever offset it was written with) - ts_epoch is an
            # actual Unix timestamp, so this reads correctly regardless.
            if event.get("ts_epoch") is not None:
                event["timestamp_display"] = datetime.fromtimestamp(
                    event["ts_epoch"]
                ).strftime("%Y-%m-%d %H:%M")
        return events

    def _build_clinic_page(clinic_name: str, day: str, concern_window: str) -> str:
        today = datetime.now().date()
        try:
            recent_events_day = date.fromisoformat(day)
        except ValueError:
            recent_events_day = today
        # Named distinctly from the "day"/"day_start" locals the
        # deviations loop below uses for its own per-iteration purpose -
        # sharing a name with those got silently clobbered here before.
        recent_events_since = datetime.combine(
            recent_events_day, datetime.min.time()
        ).timestamp()
        recent_events_until = datetime.combine(
            recent_events_day + timedelta(days=1), datetime.min.time()
        ).timestamp()

        # Camera Availability, "X/Y": Y is the best available proxy for "how
        # many cameras this clinic has" - there is no authoritative total
        # anywhere in the system (patrol reads the live tile layout off the
        # phone each visit and never persists it) - so it's the distinct
        # cameras actually seen over the last 7 days. X is how many of those
        # had a usable reading today.
        day_strs = [(today - timedelta(days=i)).isoformat() for i in range(7)]
        placeholders = ", ".join("?" for _ in day_strs)
        all_cameras = {
            row["camera_name"]
            for row in database.conn.execute(
                "SELECT DISTINCT camera_name FROM observations "
                f"WHERE clinic_name = ? AND day IN ({placeholders})",
                [clinic_name, *day_strs],
            )
        }
        today_rows = database.get_observations(today.isoformat(), clinic_name)
        usable_today = {r["camera_name"] for r in today_rows if reporting._usable(r)}
        camera_available = len(usable_today)
        camera_total = len(all_cameras | usable_today)

        # Opening/closing deviation, last 14 days - reuses the same per-day
        # primitives report.py and scoring.py already have; nothing here
        # aggregates across days on its own elsewhere yet.
        indoor = indoor_cameras(_camera_roles(), clinic_name)
        deviations = []
        for i in range(14):
            day = today - timedelta(days=i)
            rows = database.get_observations(day.isoformat(), clinic_name)
            hours = reporting.operating_hours(rows, indoor) if rows else None
            entry = {
                "day": day.isoformat(), "opened": None, "open_deviation": None,
                "closed": None, "close_deviation": None, "observed": False,
            }
            if hours:
                day_start = datetime.combine(day, datetime.min.time())
                if hours["opened"]:
                    target = reporting._expected_at(day_start, config.EXPECTED_OPEN)
                    entry["opened"] = hours["opened"].strftime("%H:%M")
                    entry["open_deviation"] = round(
                        (hours["opened"] - target).total_seconds() / 60
                    )
                    entry["observed"] = True
                if hours["closed"]:
                    target = reporting._expected_at(day_start, config.EXPECTED_CLOSE)
                    entry["closed"] = hours["closed"].strftime("%H:%M")
                    entry["close_deviation"] = round(
                        (hours["closed"] - target).total_seconds() / 60
                    )
                    entry["observed"] = True
            deviations.append(entry)

        # This clinic's own recent events (its slice of the main feed) -
        # "all the data for this clinic in one place" means pulling it in
        # here rather than sending someone to another page to piece it
        # together themselves. Kept separate from latest_pool below:
        # this one is scoped to whatever day the picker under the table
        # is set to, latest_pool never is.
        recent_events = _annotate(
            database.get_events(
                clinic_name=clinic_name,
                since_epoch=recent_events_since, until_epoch=recent_events_until,
                limit=200,
            )
        )
        recent_events_days = database.observed_days(limit=30)
        if recent_events_day.isoformat() not in recent_events_days:
            recent_events_days.insert(0, recent_events_day.isoformat())

        # Every window's metrics for just this clinic, oldest window last -
        # the "start with the Timelines x Metrics table" view. Scoped to the
        # clinic's own state/cluster (not the whole fleet) so six windows'
        # worth of scoring only ever costs one cluster's worth of rows, not
        # the whole fleet's, six times over.
        loc_state, loc_cluster = _clinic_location(clinic_name)
        # _camera_roles() is the same 120s-cached lookup _annotate() already
        # uses. Without passing it through, each of the six clinic_scores()
        # calls below would fall back to its own fresh
        # infer_roles(db.camera_descriptions()) - a fleet-wide scan of the
        # events table, run six times over for a value that does not depend
        # on the window at all. This (plus the same fix in _group_page) is
        # what made opening a clinic/cluster/state page slow.
        roles = _camera_roles()
        # Same idea, one step further: every WINDOWS entry's days are a
        # subset of the 30-day one, so today's opening/closing time would
        # otherwise be recomputed from scratch once per window that
        # includes it (Today, 3D, 7D, 14D and 30D all do) - build it once
        # here instead.
        days_30, _, _ = scoring._window(0, 30)
        hours_by_day = scoring._operating_hours_by_day(
            database, days_30, roles, state=loc_state, cluster=loc_cluster
        )
        timeline_metrics = [
            _with_low_pct({
                "window": w,
                **(
                    scoring.clinic_scores(
                        database, window=w, state=loc_state, cluster=loc_cluster,
                        roles=roles, hours_by_day=hours_by_day,
                    ).get(clinic_name)
                    # Every key present and None, not an empty dict - the
                    # same fix group_metrics() needed for a cluster/state
                    # page with no scoreable clinics (see its own comment):
                    # a window this clinic has no score in still has to
                    # look like a row with nothing to show, not one
                    # missing the columns entirely, which the template's
                    # m.timeliness (etc.) access crashes on.
                    or {key: None for key in scoring._METRIC_KEYS}
                ),
            })
            for w in scoring.WINDOWS
        ]

        # Top areas of concern, and the High/Medium case lists, over the
        # last 30 days - a fixed, recent-enough window rather than "ever",
        # so a problem resolved months ago doesn't crowd out what actually
        # needs attention now. Unresolved only - a case someone already
        # closed out isn't something to look at right now.
        top_concerns = incidents_lib.top_concerns(database, clinic_name, window="30D")
        # One pie per window, same Today/Yesterday/3D/7D/14D/30D breakdown
        # as the severity-distribution row above. Every category, not just
        # the top 5 - top_concerns() itself already excludes "normal" and
        # sorts most-frequent-first, exactly the order a pie chart wants
        # its slices in.
        category_pies = [
            {
                "window": w,
                **_category_pie(
                    incidents_lib.top_concerns(
                        database, clinic_name, window=w, limit=100
                    )
                ),
            }
            for w in scoring.WINDOWS
        ]
        high_cases = incidents_lib.list_incidents(
            database, clinic=clinic_name, severity="High", status="open",
            window=concern_window,
        )
        medium_cases = incidents_lib.list_incidents(
            database, clinic=clinic_name, severity="Medium", status="open",
            window=concern_window,
        )

        # Latest view: the most recent screenshot per camera. Its own pull,
        # deliberately NOT the day-scoped recent_events above - this always
        # means the true latest, regardless of what day the picker under
        # the Recent events table is set to.
        latest_pool = _annotate(database.get_events(clinic_name=clinic_name, limit=20))
        latest_views = []
        seen_cameras = set()
        for ev in latest_pool:
            cam = ev.get("camera_name")
            if not ev.get("screenshot_path") or cam in seen_cameras:
                continue
            seen_cameras.add(cam)
            latest_views.append(ev)

        return render_template(
            "clinic.html",
            clinic_name=clinic_name,
            clinic_state=loc_state,
            clinic_cluster=loc_cluster,
            camera_available=camera_available,
            camera_total=camera_total,
            expected_open=config.EXPECTED_OPEN,
            expected_close=config.EXPECTED_CLOSE,
            deviations=deviations,
            recent_events=recent_events,
            recent_events_day=recent_events_day.isoformat(),
            recent_events_days=recent_events_days,
            timeline_metrics=timeline_metrics,
            top_concerns=top_concerns,
            category_pies=category_pies,
            high_cases=high_cases,
            medium_cases=medium_cases,
            concern_window=concern_window,
            concern_windows=list(scoring.WINDOWS.keys()),
            latest_views=latest_views,
            refresh=config.DASHBOARD_REFRESH_SEC,
        )

    # path: converter, not the default string one - a real device-list
    # artifact tracked as a "clinic_name" ("iDS-7104HQHI-M1/S(FW1789907)")
    # contains a literal "/", which the default converter refuses to match
    # at all (a 404, not a wrong match) - found via the new cluster/state
    # pages linking straight to it.
    @app.route("/clinic/<path:clinic_name>")
    def clinic_page(clinic_name: str):
        user = auth.current_user(database)
        if user:
            auth.enforce_scope(database, user, clinic_name=clinic_name)
        day = request.args.get("day") or _today()
        concern_window = request.args.get("window", "30D")
        if concern_window not in scoring.WINDOWS:
            concern_window = "30D"
        return _cached_page(
            f"clinic:{clinic_name}:{day}:{concern_window}",
            lambda: _build_clinic_page(clinic_name, day, concern_window)
        )

    def _group_page(
        level: str, name: str, state: Optional[str], cluster: Optional[str], day: str,
        concern_window: str,
    ):
        """
        Shared by /state/<name> and /cluster/<name> - the same sections a
        clinic's own page has, aggregated across every clinic in scope
        instead of read straight off one clinic's rows. Exactly one of
        state/cluster is set by the caller; passing just one to every
        filter below (clinic_scores(), list_incidents(), get_events(),
        group_counts()) already narrows correctly without needing to look
        up which state a cluster belongs to.
        """
        today = datetime.now().date()
        try:
            recent_events_day = date.fromisoformat(day)
        except ValueError:
            recent_events_day = today
        recent_events_since = datetime.combine(
            recent_events_day, datetime.min.time()
        ).timestamp()
        recent_events_until = datetime.combine(
            recent_events_day + timedelta(days=1), datetime.min.time()
        ).timestamp()

        clinic_names = [
            r["value"] for r in database.group_counts("clinic_name", state=state, cluster=cluster)
        ]

        # Every window's metrics, averaged across every clinic in scope -
        # the same Timelines x Metrics view a clinic's own page has, one
        # row per clinic rolled up into one row for the group. Roles and
        # opening/closing hours passed through once (see clinic_page()'s
        # comments on the same pattern) so six windows cost one camera-role
        # scan and one operating-hours pass, not six of each.
        roles = _camera_roles()
        days_30, _, _ = scoring._window(0, 30)
        hours_by_day = scoring._operating_hours_by_day(
            database, days_30, roles, state=state, cluster=cluster
        )
        # Kept ungrouped (per clinic), not just the group_metrics() average,
        # so "Today" can drive the most-problematic rankings below without
        # a second scoring pass over the same window.
        scores_by_window = {
            w: scoring.clinic_scores(
                database, window=w, state=state, cluster=cluster,
                roles=roles, hours_by_day=hours_by_day,
            )
            for w in scoring.WINDOWS
        }
        timeline_metrics = [
            _with_low_pct({
                "window": w,
                **scoring.group_metrics(scores_by_window[w]),
            })
            for w in scoring.WINDOWS
        ]

        top_concerns = incidents_lib.top_concerns(
            database, state=state, cluster=cluster, window="30D"
        )
        # One pie per window, same breakdown as clinic_page()'s.
        category_pies = [
            {
                "window": w,
                **_category_pie(
                    incidents_lib.top_concerns(
                        database, state=state, cluster=cluster, window=w, limit=100
                    )
                ),
            }
            for w in scoring.WINDOWS
        ]
        # Unresolved only - see clinic_page()'s comment on the same choice.
        high_cases = incidents_lib.list_incidents(
            database, state=state, cluster=cluster, severity="High",
            status="open", window=concern_window,
        )
        medium_cases = incidents_lib.list_incidents(
            database, state=state, cluster=cluster, severity="Medium",
            status="open", window=concern_window,
        )

        # A generous pull (not just the 12 shown below) so "one screenshot
        # per clinic" has enough recent history to find one for as many
        # clinics as possible, capped so the section stays a quick visual
        # scan rather than one thumbnail per clinic in a 100-clinic state.
        # Deliberately NOT scoped to recent_events_day - "Latest view" means
        # the actual latest, not frozen to whatever day someone has the
        # Recent events table set to below.
        latest_pool = _annotate(
            database.get_events(state=state, cluster=cluster, limit=200)
        )
        latest_views = []
        seen_clinics = set()
        for ev in latest_pool:
            cn = ev.get("clinic_name")
            if not ev.get("screenshot_path") or cn in seen_clinics:
                continue
            seen_clinics.add(cn)
            latest_views.append(ev)
            if len(latest_views) >= 12:
                break

        # Recent events, one calendar day at a time - the day picker below
        # the table drives this via since/until epoch bounds, independent
        # of latest_views above (which is always the true latest regardless
        # of what day this is set to).
        recent_events = _annotate(
            database.get_events(
                state=state, cluster=cluster,
                since_epoch=recent_events_since, until_epoch=recent_events_until,
                limit=200,
            )
        )
        recent_events_days = database.observed_days(limit=30)
        if recent_events_day.isoformat() not in recent_events_days:
            recent_events_days.insert(0, recent_events_day.isoformat())

        # Clinic-wise status today - the group-level equivalent of a
        # clinic's own 14-day Opening/Closing table. One clinic's history
        # over time doesn't generalize to many clinics at once, but "how is
        # every clinic doing today" does. Scoped and roles reused (see the
        # comments above) instead of summarizing the whole fleet and
        # filtering down to this group's clinics afterward.
        day_rows = reporting.daily_summary(
            today.isoformat(), db=database, state=state, cluster=cluster, roles=roles
        )
        clinics_open_today = sum(1 for r in day_rows if r["status"] == "opened")
        clinics_offline_today = sum(1 for r in day_rows if r["status"] == "offline")

        # Cluster page: which clinics it contains. State page: which
        # clusters it contains - both just a count-and-link list, so
        # drilling from a state into one cluster (and from there into one
        # clinic) never needs the sidebar's filter tree.
        children = (
            database.group_counts("clinic_name", state=state, cluster=cluster)
            if level == "Cluster"
            else database.group_counts("cluster", state=state)
        )

        # A cluster page's own "most problematic clinics", two ways - by
        # today's overall score (the same number the Timelines x Metrics
        # table already shows, just per clinic instead of averaged away),
        # and by raw count of High/Medium incidents over the last 30 days
        # (from the case lists just above - a clinic hit repeatedly reads
        # as more problematic here even if each individual incident was
        # brief). Today's per-clinic scores were already computed above
        # for the group average, so this is free - no extra query.
        most_problematic_by_score: List[Dict[str, Any]] = []
        most_problematic_by_incidents: List[Dict[str, Any]] = []
        if level == "Cluster":
            today_scores = scores_by_window["Today"]
            ranked = sorted(
                (
                    (c, v["overall"]) for c, v in today_scores.items()
                    if v["overall"] is not None
                ),
                key=lambda item: item[1],
            )
            most_problematic_by_score = [
                {"clinic_name": c, "overall": overall} for c, overall in ranked[:5]
            ]

            incident_counts: Dict[str, int] = {}
            for i in high_cases + medium_cases:
                incident_counts[i["clinic_name"]] = (
                    incident_counts.get(i["clinic_name"], 0) + 1
                )
            most_problematic_by_incidents = [
                {"clinic_name": c, "count": n}
                for c, n in sorted(incident_counts.items(), key=lambda kv: -kv[1])[:5]
            ]

        # A state page's cluster list, worst-first instead of alphabetical -
        # each clinic's own row in day_rows already carries its cluster tag
        # (from daily_summary(), computed above), so this reuses that
        # instead of a fresh fleet-wide clinic_locations() lookup.
        if level == "State":
            today_scores = scores_by_window["Today"]
            clinic_cluster = {r["clinic"]: r["cluster"] for r in day_rows if r["cluster"]}
            cluster_scores: Dict[str, List[float]] = {}
            for clinic_name_, v in today_scores.items():
                if v["overall"] is None:
                    continue
                cl = clinic_cluster.get(clinic_name_)
                if not cl:
                    continue
                cluster_scores.setdefault(cl, []).append(v["overall"])
            cluster_avg = {
                cl: sum(vals) / len(vals) for cl, vals in cluster_scores.items()
            }
            # No score today (a brand-new cluster, or a quiet one) sorts
            # after every scored cluster, alphabetically among themselves,
            # rather than landing at the top by an accidental empty-first
            # sort or vanishing from the list.
            children = sorted(
                children,
                key=lambda c: (
                    cluster_avg.get(c["value"]) is None,
                    cluster_avg.get(c["value"], 0.0),
                    c["value"],
                ),
            )

        return render_template(
            "group.html",
            level=level,
            name=name,
            children=children,
            clinics_total=len(clinic_names),
            clinics_open_today=clinics_open_today,
            clinics_offline_today=clinics_offline_today,
            timeline_metrics=timeline_metrics,
            most_problematic_by_score=most_problematic_by_score,
            most_problematic_by_incidents=most_problematic_by_incidents,
            top_concerns=top_concerns,
            category_pies=category_pies,
            high_cases=high_cases,
            medium_cases=medium_cases,
            concern_window=concern_window,
            concern_windows=list(scoring.WINDOWS.keys()),
            latest_views=latest_views,
            day_rows=day_rows,
            recent_events=recent_events,
            recent_events_day=recent_events_day.isoformat(),
            recent_events_days=recent_events_days,
            refresh=config.DASHBOARD_REFRESH_SEC,
        )

    # path: converter, not the default string one - a real deployed cluster
    # name ("Balasore/Bhadrak") contains a literal "/", which the default
    # converter refuses to match at all (a 404, not a wrong match).
    @app.route("/state/<path:state_name>")
    def state_page(state_name: str):
        user = auth.current_user(database)
        if user:
            auth.enforce_scope(database, user, state=state_name)
        day = request.args.get("day") or _today()
        concern_window = request.args.get("window", "30D")
        if concern_window not in scoring.WINDOWS:
            concern_window = "30D"
        return _cached_page(
            f"state:{state_name}:{day}:{concern_window}",
            lambda: _group_page(
                "State", state_name, state_name, None, day, concern_window
            ),
        )

    @app.route("/cluster/<path:cluster_name>")
    def cluster_page(cluster_name: str):
        user = auth.current_user(database)
        if user:
            auth.enforce_scope(database, user, cluster=cluster_name)
        day = request.args.get("day") or _today()
        concern_window = request.args.get("window", "30D")
        if concern_window not in scoring.WINDOWS:
            concern_window = "30D"
        return _cached_page(
            f"cluster:{cluster_name}:{day}:{concern_window}",
            lambda: _group_page(
                "Cluster", cluster_name, None, cluster_name, day, concern_window
            ),
        )

    @app.route("/incidents")
    def incidents_page():
        window = request.args.get("window", "all")
        if window != "all" and window not in scoring.WINDOWS:
            window = "all"
        # Defaults to unresolved - a resolved case isn't something to look
        # at right now; the filter still lets someone switch to "All" or
        # "Resolved" to look back.
        status = request.args.get("status", "open")
        severity = request.args.get("severity", "all")
        group = request.args.get("group", "all")
        clinic = request.args.get("clinic", "all")
        state = request.args.get("state", "all")
        cluster = request.args.get("cluster", "all")
        state, cluster = _scope_state_cluster(state, cluster)
        sort = request.args.get("sort", "last_seen")
        if sort not in incidents_lib.SORT_KEYS:
            sort = "last_seen"
        direction = request.args.get("dir", "desc")
        if direction not in ("asc", "desc"):
            direction = "desc"

        rows = incidents_lib.list_incidents(
            database,
            window=None if window == "all" else window,
            status=None if status == "all" else status,
            severity=None if severity == "all" else severity,
            group=None if group == "all" else group,
            clinic=None if clinic == "all" else clinic,
            state=None if state == "all" else state,
            cluster=None if cluster == "all" else cluster,
            sort=sort,
            direction=direction,
        )

        states = database.group_counts("state")
        clusters = database.group_counts("cluster", state=None if state == "all" else state)
        clinics = database.group_counts(
            "clinic_name",
            state=None if state == "all" else state,
            cluster=None if cluster == "all" else cluster,
        )
        groups = sorted(set(incidents_lib.CATEGORY_GROUPS.values()))

        return render_template(
            "incidents.html",
            rows=rows,
            window=window, windows=list(scoring.WINDOWS.keys()),
            status=status, severity=severity, group=group, clinic=clinic,
            state=state, cluster=cluster, sort=sort, direction=direction,
            groups=groups, severities=SEVERITIES,
            states=states, clusters=clusters, clinics=clinics,
            refresh=config.DASHBOARD_REFRESH_SEC,
        )

    @app.route("/incidents/<int:incident_id>")
    def incident_detail(incident_id: int):
        row = incidents_lib.get_incident(database, incident_id)
        if row is None:
            abort(404)
        user = auth.current_user(database)
        if user:
            auth.enforce_scope(database, user, clinic_name=row["clinic_name"])
        comments = database.list_incident_comments(incident_id)
        for c in comments:
            c["created_str"] = datetime.fromtimestamp(c["created_at"]).strftime(
                "%Y-%m-%d %H:%M"
            )
        return render_template(
            "incident_detail.html",
            incident=row,
            comments=comments,
            refresh=config.DASHBOARD_REFRESH_SEC,
        )

    @app.route("/incidents/<int:incident_id>/comment", methods=["POST"])
    @auth.login_required(lambda: database)
    def incident_add_comment(incident_id: int):
        row = incidents_lib.get_incident(database, incident_id)
        if row is None:
            abort(404)
        user = auth.current_user(database)
        auth.enforce_scope(database, user, clinic_name=row["clinic_name"])
        text = (request.form.get("text") or "").strip()
        if text:
            database.add_incident_comment(
                incident_id, user["id"], user["name"], user["role"], text
            )
        return redirect(url_for("incident_detail", incident_id=incident_id))

    @app.route("/incidents/<int:incident_id>/address", methods=["POST"])
    @auth.role_required("cluster_manager", database_getter=lambda: database)
    def incident_mark_addressed(incident_id: int):
        row = incidents_lib.get_incident(database, incident_id)
        if row is None:
            abort(404)
        user = auth.current_user(database)
        auth.enforce_scope(database, user, clinic_name=row["clinic_name"])
        database.mark_incident_addressed(incident_id, user["name"])
        return redirect(url_for("incident_detail", incident_id=incident_id))

    @app.route("/incidents/<int:incident_id>/close", methods=["POST"])
    @auth.role_required("command_center", "admin", database_getter=lambda: database)
    def incident_close(incident_id: int):
        row = incidents_lib.get_incident(database, incident_id)
        if row is None:
            abort(404)
        user = auth.current_user(database)
        database.close_incident(incident_id, user["name"])
        return redirect(url_for("incident_detail", incident_id=incident_id))

    @app.route("/incidents/<int:incident_id>/reopen", methods=["POST"])
    @auth.role_required("command_center", "admin", database_getter=lambda: database)
    def incident_reopen(incident_id: int):
        row = incidents_lib.get_incident(database, incident_id)
        if row is None:
            abort(404)
        database.reopen_incident(incident_id)
        return redirect(url_for("incident_detail", incident_id=incident_id))

    # -- admin: user provisioning -------------------------------------------#
    @app.route("/admin/users")
    @auth.role_required("admin", database_getter=lambda: database)
    def admin_users_page():
        return render_template(
            "admin_users.html", users=database.list_users(), result=None,
        )

    @app.route("/admin/users/upload", methods=["POST"])
    @auth.role_required("admin", database_getter=lambda: database)
    def admin_users_upload():
        """
        Admin's CSV: State, Cluster, Cluster Manager name/Email/Phone/PW,
        State Manager name/Email/Phone/PW - one row per cluster, so a
        state's own manager repeats across every one of its clusters' rows
        (harmless: upsert_users_from_rows matches by email, so a repeated
        row just re-applies the same account). A password cell is required
        to touch that row's account at all - a blank one means "don't
        create or change this account from this row," never "blank the
        existing password."  The plaintext value from the upload is hashed
        immediately below and never stored or logged anywhere.
        """
        file = request.files.get("csv_file")
        error = None
        result = None
        if not file or not file.filename:
            error = "Choose a CSV file first."
        else:
            text = file.stream.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            # Header lookup tolerant of stray whitespace, exact casing not
            # required beyond matching the documented column names.
            def cell(row: dict, key: str) -> str:
                for k, v in row.items():
                    if k and k.strip() == key:
                        return (v or "").strip()
                return ""

            rows: List[Dict[str, Any]] = []
            for r in reader:
                state = cell(r, "State")
                cluster = cell(r, "Cluster")
                cm_email = cell(r, "Cluster Manager Email").lower()
                cm_pw = cell(r, "Cluster Manager PW")
                if cm_email and cm_pw:
                    rows.append({
                        "name": cell(r, "Cluster Manager name") or cm_email,
                        "email": cm_email,
                        "phone": cell(r, "Cluster Manager Phone"),
                        "password_hash": auth.hash_password(cm_pw),
                        "role": "cluster_manager",
                        "state": state,
                        "cluster": cluster,
                    })
                sm_email = cell(r, "State Manager Email").lower()
                sm_pw = cell(r, "State Manager PW")
                if sm_email and sm_pw:
                    rows.append({
                        "name": cell(r, "State Manager name") or sm_email,
                        "email": sm_email,
                        "phone": cell(r, "State Manager Phone"),
                        "password_hash": auth.hash_password(sm_pw),
                        "role": "state_manager",
                        "state": state,
                        "cluster": None,
                    })
            if not rows:
                error = "No usable rows found - check the column headers match exactly."
            else:
                result = database.upsert_users_from_rows(rows)
        return render_template(
            "admin_users.html", users=database.list_users(),
            result=result, error=error,
        )

    @app.route("/api/events")
    def api_events():
        severity, clinic, camera, state, cluster, limit, since = _filters()
        events = _annotate(
            database.get_events(
                severity=severity,
                clinic_name=clinic,
                camera_name=camera,
                state=state,
                cluster=cluster,
                since_epoch=since,
                limit=limit,
            )
        )
        total = database.count_events(
            severity=severity, clinic_name=clinic, camera_name=camera,
            state=state, cluster=cluster, since_epoch=since,
        )
        return jsonify({"count": len(events), "total": total, "events": events})

    @app.route("/api/stats")
    def api_stats():
        return jsonify(
            {
                "counts": database.counts_by_severity(),
                "last_24h": database.counts_by_severity(since_epoch=time.time() - 86400),
                "clinics": database.distinct("clinic_name"),
                "cameras": database.distinct("camera_name"),
            }
        )

    @app.route("/api/events/<int:event_id>/ack", methods=["POST"])
    def api_ack(event_id: int):
        if not database.get_event(event_id):
            abort(404)
        database.acknowledge(event_id)
        return jsonify({"ok": True, "id": event_id})

    # -- daily reports ------------------------------------------------------ #
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @app.route("/api/reports")
    def api_reports():
        """
        Every clinic worth showing, whether or not a report exists yet.

        Clinics that were patrolled today but never had a report generated
        still appear, marked as needing an update - otherwise a clinic with
        fresh data would be invisible until someone ran the CLI.
        """
        day = request.args.get("day") or _today()
        on_disk = reporting.available_reports()
        observed = database.observed_clinics(day)
        locations = scoring.clinic_locations(database)

        clinics = []
        seen = set()
        for name in observed:
            slug = reporting.clinic_slug(name)
            seen.add(slug)
            state, cluster = locations.get(name, (None, None))
            clinics.append(
                {
                    "name": name,
                    "slug": slug,
                    "state": state or "",
                    "cluster": cluster or "",
                    "has_report": day in on_disk.get(slug, []),
                    "days": on_disk.get(slug, []),
                    "observed_today": True,
                }
            )
        # Clinics with older reports but no data today still deserve a listing.
        for slug, days in on_disk.items():
            if slug in seen:
                continue
            name = slug.replace("_", " ")
            state, cluster = locations.get(name, (None, None))
            clinics.append(
                {
                    "name": name,
                    "slug": slug,
                    "state": state or "",
                    "cluster": cluster or "",
                    "has_report": day in days,
                    "days": days,
                    "observed_today": False,
                }
            )

        # State -> Cluster -> Clinic, so the fleet reads the same way here as
        # everywhere else it's grouped (the /scores breakdown, the sidebar
        # chips) instead of one flat alphabetical list.
        clinics.sort(key=lambda c: (c["state"] or "￿", c["cluster"] or "￿",
                                     c["name"]))
        return jsonify(
            {"day": day, "count": len(clinics), "clinics": clinics,
             "days_with_data": database.observed_days(limit=14)}
        )

    @app.route("/api/clinic_status")
    def api_clinic_status():
        """
        Every clinic's opened/closed/offline status for one day, grouped
        by state -> cluster - the "Clinics Opened Today" stat card's
        click-through. Same three-way status report.daily_summary()
        already computes for the cluster/state pages' own stat cards
        (see its docstring for why "offline" is kept distinct from
        "closed" - a clinic whose NVR dropped off the network looks
        identical to a shut one through this system, and calling it
        closed would blame the staff for a broken router).
        """
        day = request.args.get("day") or _today()
        rows = reporting.daily_summary(day, db=database)
        rows.sort(key=lambda r: (r["state_name"] or "￿", r["cluster"] or "￿", r["clinic"]))

        status_days = [
            r["d"]
            for r in database.conn.execute(
                "SELECT DISTINCT day AS d FROM clinic_status ORDER BY d DESC LIMIT 14"
            ).fetchall()
        ]
        days = sorted(set(database.observed_days(limit=14)) | set(status_days),
                      reverse=True)

        return jsonify(
            {
                "day": day,
                "opened": [r for r in rows if r["status"] == "opened"],
                "closed": [r for r in rows if r["status"] == "closed"],
                "offline": [r for r in rows if r["status"] == "offline"],
                "total": len(rows),
                "days_with_data": days,
            }
        )

    def _offline_data(day: str):
        """
        Outages and camera issues for one day, each tagged with state/
        cluster - shared by the JSON API and the xlsx/csv downloads below
        so the two never drift apart on what counts as "offline that day".
        """
        outages = database.offline_periods(day)

        # Cameras that were faulty on every check of the day.
        hide, hide_params = ignored_clause()
        rows = database.conn.execute(
            "SELECT clinic_name, camera_name,"
            " COUNT(*) AS checks,"
            " SUM(CASE WHEN health_status IN ('no_signal','frozen','obstructed')"
            "     THEN 1 ELSE 0 END) AS bad,"
            " MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen"
            " FROM observations WHERE day = ?"
            + (f" AND {hide}" if hide else "")
            + " GROUP BY clinic_name, camera_name"
            " HAVING bad > 0 ORDER BY clinic_name, camera_name",
            (day, *hide_params),
        ).fetchall()
        cameras = [
            {
                "clinic_name": r["clinic_name"],
                "camera_name": r["camera_name"],
                "checks": r["checks"],
                "bad": r["bad"],
                "all_day": r["bad"] == r["checks"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]

        # State/cluster per clinic, so the page can group State -> Cluster
        # -> Clinic the same way Reports does.
        locations = scoring.clinic_locations(database)
        for row in outages:
            state, cluster = locations.get(row["clinic_name"], (None, None))
            row["state"], row["cluster"] = state, cluster
        for row in cameras:
            state, cluster = locations.get(row["clinic_name"], (None, None))
            row["state"], row["cluster"] = state, cluster
        return outages, cameras

    @app.route("/api/offline")
    def api_offline():
        """
        What was unreachable today, at two levels.

        A clinic can be down in two different ways, and both matter: the whole
        device unreachable (the patrol cannot even open it), or the device fine
        but individual camera channels dead. The second is easy to miss because
        the clinic still appears in every report.
        """
        day = request.args.get("day") or _today()
        outages, cameras = _offline_data(day)
        summary = database.clinic_status_summary(day)
        contacts = config.load_cluster_contacts()

        # A day where every clinic failed has no observations at all, so the
        # day list has to come from the status log as well or that day would
        # be unselectable - exactly the day someone wants to look at.
        status_days = [
            r["d"]
            for r in database.conn.execute(
                "SELECT DISTINCT day AS d FROM clinic_status ORDER BY d DESC LIMIT 14"
            ).fetchall()
        ]
        days = sorted(set(database.observed_days(limit=14)) | set(status_days),
                      reverse=True)

        return jsonify(
            {
                "day": day,
                "outages": outages,
                "clinics": summary,
                "cameras": cameras,
                "contacts": contacts,
                "days_with_data": days,
            }
        )

    @app.route("/api/offline/csv")
    def api_offline_csv():
        """One CSV per day, outages and camera issues in one file (they
        have different columns, so a "Section" column tells them apart -
        the xlsx download keeps them on separate sheets instead)."""
        day = request.args.get("day") or _today()
        outages, cameras = _offline_data(day)

        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(["Section", "State", "Cluster", "Clinic", "Camera",
                         "Offline from", "Back at", "Status", "Duration (min)",
                         "Checks", "Bad checks", "Reason"])
        for row in outages:
            writer.writerow([
                "Outage", row.get("state", ""), row.get("cluster", ""),
                row["clinic_name"], "",
                (row.get("from") or "")[11:16], "Still offline" if row.get("ongoing")
                else (row.get("to") or "")[11:16],
                "Ongoing" if row.get("ongoing") else "Resolved",
                round(row.get("minutes") or 0), row.get("checks", ""), "",
                row.get("reason") or "",
            ])
        for row in cameras:
            writer.writerow([
                "Camera issue", row.get("state", ""), row.get("cluster", ""),
                row["clinic_name"], row["camera_name"], "", "",
                "Dead all day" if row["all_day"] else "Some checks bad",
                "", row.get("checks", ""), row.get("bad", ""), "",
            ])

        return Response(
            buffer.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="offline-clinics-{day}.csv"'
            },
        )

    @app.route("/api/offline/xlsx")
    def api_offline_xlsx():
        """One Excel workbook per day: clinics unreachable, and cameras
        that reported no signal at least once - see build_offline_workbook()."""
        day = request.args.get("day") or _today()
        outages, cameras = _offline_data(day)
        try:
            book = build_offline_workbook(day, outages, cameras)
        except RuntimeError as exc:            # openpyxl missing on this host
            return jsonify({"ok": False, "day": day, "error": str(exc)}), 501

        buffer = io.BytesIO()
        book.save(buffer)
        return Response(
            buffer.getvalue(),
            mimetype=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition":
                    f'attachment; filename="offline-clinics-{day}.xlsx"'
            },
        )

    @app.route("/api/reports/csv")
    def api_reports_csv():
        """One CSV per day: clinic, date, opening, closing, status."""
        day = request.args.get("day") or _today()
        rows = reporting.daily_summary(day, db=database)

        buffer = io.StringIO()
        # QUOTE_ALL keeps a clinic name with a comma in it from splitting into
        # two columns, whatever spreadsheet opens the file.
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(["State", "Cluster", "Clinic", "Date", "Opening time",
                         "Closing time", "Status", "Offline minutes", "Checks"])
        for row in rows:
            writer.writerow([
                row.get("state_name", ""), row.get("cluster", ""), row["clinic"],
                row["date"], row["opening_time"], row["closing_time"],
                row["status"], row["offline_minutes"], row["checks"],
            ])

        # utf-8-sig: Excel reads a plain UTF-8 CSV as the local codepage and
        # mangles any non-ASCII clinic name. The BOM tells it otherwise.
        return Response(
            buffer.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="clinic-daily-report-{day}.csv"'
            },
        )

    @app.route("/api/reports/xlsx")
    def api_reports_xlsx():
        """One Excel workbook per day: clinic, date, opening, closing, status."""
        day = request.args.get("day") or _today()
        try:
            book = build_workbook(day, reporting.daily_summary(day, db=database))
        except RuntimeError as exc:            # openpyxl missing on this host
            return jsonify({"ok": False, "day": day, "error": str(exc)}), 501

        buffer = io.BytesIO()
        book.save(buffer)
        return Response(
            buffer.getvalue(),
            mimetype=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition":
                    f'attachment; filename="clinic-daily-report-{day}.xlsx"'
            },
        )

    @app.route("/api/reports/<slug>")
    def api_report(slug: str):
        day = request.args.get("day") or _today()
        path = config.BASE_DIR / "reports" / slug / f"{day}.md"
        try:
            # Never let a crafted slug walk out of the reports directory.
            path.resolve().relative_to((config.BASE_DIR / "reports").resolve())
        except ValueError:
            abort(404)
        if not path.is_file():
            return jsonify({"slug": slug, "day": day, "found": False,
                            "html": "", "markdown": ""}), 404
        text = path.read_text(encoding="utf-8")
        return jsonify(
            {
                "slug": slug,
                "day": day,
                "found": True,
                "markdown": text,
                "html": markdown_to_html(text),
                "updated": datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
        )

    @app.route("/api/reports/generate", methods=["POST"])
    def api_generate_reports():
        """Rebuild every clinic's report for the day from stored observations."""
        day = request.args.get("day") or _today()
        try:
            written = reporting.generate_all(day, db=database)
        except Exception as exc:                      # surface, never 500 blindly
            return jsonify({"ok": False, "day": day, "error": str(exc)}), 500
        return jsonify(
            {
                "ok": True,
                "day": day,
                "generated": len(written),
                "clinics": [p.parent.name for p in written],
            }
        )

    # -- static evidence ---------------------------------------------------- #
    @app.route("/screenshots/<path:relative_path>")
    def screenshot(relative_path: str):
        root = Path(app.config["SCREENSHOT_DIR"])
        # send_from_directory rejects traversal outside the root.
        return send_from_directory(root, relative_path)

    @app.teardown_appcontext
    def _close_db(_exc):  # pragma: no cover - per-request cleanup
        pass

    return app


def main() -> None:
    config.ensure_directories()
    app = create_app()
    print(f"dashboard -> http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
