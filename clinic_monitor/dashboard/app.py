"""
Stage 6 - dashboard.

A small Flask app that lists alerts newest-first with severity / clinic
filters and the evidence screenshot for each event.

    python dashboard/app.py        (or: python -m dashboard.app)
    -> http://127.0.0.1:8000
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import report as reporting  # noqa: E402
from analysis.camera_role import infer_roles  # noqa: E402
from dashboard.render import markdown_to_html  # noqa: E402
from storage.database import Database  # noqa: E402

SEVERITIES = ("High", "Medium", "Low")


def create_app(db_path: Optional[Path] = None) -> Flask:
    app = Flask(__name__)
    app.config["SCREENSHOT_DIR"] = str(Path(config.SCREENSHOT_DIR).resolve())
    app.config["DB_PATH"] = str(db_path or config.DB_PATH)
    database = Database(Path(app.config["DB_PATH"]))

    def _filters():
        severity = request.args.get("severity", "all")
        clinic = request.args.get("clinic", "all")
        camera = request.args.get("camera", "all")
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
        return severity, clinic, camera, limit, since

    # -- pages ------------------------------------------------------------- #
    @app.route("/")
    def index():
        severity, clinic, camera, limit, since = _filters()
        events = database.get_events(
            severity=severity,
            clinic_name=clinic,
            camera_name=camera,
            since_epoch=since,
            limit=limit,
        )
        return render_template(
            "index.html",
            events=events,
            counts=database.counts_by_severity(),
            clinics=database.distinct("clinic_name"),
            severity=severity,
            clinic=clinic,
            severities=SEVERITIES,
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
        return events

    @app.route("/api/events")
    def api_events():
        severity, clinic, camera, limit, since = _filters()
        events = _annotate(
            database.get_events(
                severity=severity,
                clinic_name=clinic,
                camera_name=camera,
                since_epoch=since,
                limit=limit,
            )
        )
        return jsonify({"count": len(events), "events": events})

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

        clinics = []
        seen = set()
        for name in observed:
            slug = reporting.clinic_slug(name)
            seen.add(slug)
            clinics.append(
                {
                    "name": name,
                    "slug": slug,
                    "has_report": day in on_disk.get(slug, []),
                    "days": on_disk.get(slug, []),
                    "observed_today": True,
                }
            )
        # Clinics with older reports but no data today still deserve a listing.
        for slug, days in on_disk.items():
            if slug in seen:
                continue
            clinics.append(
                {
                    "name": slug.replace("_", " "),
                    "slug": slug,
                    "has_report": day in days,
                    "days": days,
                    "observed_today": False,
                }
            )

        clinics.sort(key=lambda c: (not c["observed_today"], c["name"]))
        return jsonify(
            {"day": day, "count": len(clinics), "clinics": clinics,
             "days_with_data": database.observed_days(limit=14)}
        )

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
        outages = database.offline_periods(day)
        summary = database.clinic_status_summary(day)

        # Cameras that were faulty on every check of the day.
        rows = database.conn.execute(
            "SELECT clinic_name, camera_name,"
            " COUNT(*) AS checks,"
            " SUM(CASE WHEN health_status IN ('no_signal','frozen','obstructed')"
            "     THEN 1 ELSE 0 END) AS bad,"
            " MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen"
            " FROM observations WHERE day = ?"
            " GROUP BY clinic_name, camera_name"
            " HAVING bad > 0 ORDER BY clinic_name, camera_name",
            (day,),
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
                "days_with_data": days,
            }
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
