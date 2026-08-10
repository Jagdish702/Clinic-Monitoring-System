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
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
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
    @app.route("/api/events")
    def api_events():
        severity, clinic, camera, limit, since = _filters()
        events = database.get_events(
            severity=severity,
            clinic_name=clinic,
            camera_name=camera,
            since_epoch=since,
            limit=limit,
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
