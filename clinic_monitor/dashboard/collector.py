"""
Stage 6b - the collector: one small, separate service that lets many patrol
VMs (one per cluster) fan their writes into a single dashboard's database.

Deliberately a second process on its own port, not a route bolted onto
dashboard/app.py. The dashboard binds 127.0.0.1 on purpose - it has no
authentication of its own, and is only ever reached through an SSH tunnel or
an authenticating proxy (see DEPLOY_GCP.md). This service is the opposite:
it must be reachable from other VMs on the internal network, so it carries
its own bearer-token check on every request instead of inheriting the
dashboard's "hide it behind localhost" model.

Each route is a thin wrapper around the exact same Database methods every
patrol VM already calls locally (insert_event, insert_observation,
record_clinic_status) - a pushed write and a local one are indistinguishable
once they land here, and go through no other code path.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime
from typing import Any, Dict, Tuple

from flask import Flask, jsonify, request

import config
from storage.database import Database

log = logging.getLogger(__name__)


def _authorized() -> bool:
    header = request.headers.get("Authorization", "")
    given = header[7:] if header.startswith("Bearer ") else ""
    # Constant-time compare: this endpoint is reachable from every other VM
    # on the network, not just localhost, so a timing side-channel on the
    # token check is a real (if small) risk here in a way it never was for
    # the dashboard itself.
    return bool(config.COLLECTOR_TOKEN) and hmac.compare_digest(given, config.COLLECTOR_TOKEN)


def create_app(db: "Database | None" = None) -> Flask:
    if not config.COLLECTOR_TOKEN:
        raise RuntimeError(
            "CM_COLLECTOR_TOKEN is not set - refusing to start an unauthenticated "
            "write endpoint. Set it to a random shared secret before running the "
            "collector."
        )

    app = Flask(__name__)
    # push=False: a write landing here is already the final copy - see the
    # comment on Database.__init__ for why this must never re-push.
    database = db or Database(push=False)

    @app.before_request
    def _check_auth():
        if not _authorized():
            return jsonify(error="unauthorized"), 401

    @app.route("/collect/events", methods=["POST"])
    def collect_event():
        body = request.get_json(silent=True) or {}
        required = ("clinic_name", "camera_name", "description", "severity")
        missing = [k for k in required if not body.get(k)]
        if missing:
            return jsonify(error=f"missing fields: {', '.join(missing)}"), 400
        try:
            row_id = database.insert_event(body)
        except Exception as exc:                      # pragma: no cover - defensive
            log.error("collector: failed to insert pushed event: %s", exc)
            return jsonify(error="insert failed"), 500
        return jsonify(id=row_id), 201

    @app.route("/collect/observations", methods=["POST"])
    def collect_observation():
        body = request.get_json(silent=True) or {}
        required = ("day", "clinic_name", "camera_name")
        missing = [k for k in required if not body.get(k)]
        if missing:
            return jsonify(error=f"missing fields: {', '.join(missing)}"), 400
        try:
            row_id = database.insert_observation(body)
        except Exception as exc:                      # pragma: no cover - defensive
            log.error("collector: failed to insert pushed observation: %s", exc)
            return jsonify(error="insert failed"), 500
        return jsonify(id=row_id), 201

    @app.route("/collect/clinic_status", methods=["POST"])
    def collect_clinic_status():
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        required = ("clinic_name", "status", "when")
        missing = [k for k in required if not body.get(k)]
        if missing:
            return jsonify(error=f"missing fields: {', '.join(missing)}"), 400
        try:
            when = datetime.fromisoformat(body["when"])
        except ValueError:
            return jsonify(error="'when' must be an ISO timestamp"), 400
        try:
            database.record_clinic_status(
                body["clinic_name"],
                body["status"],
                when,
                body.get("reason", ""),
                state=body.get("state"),
                cluster=body.get("cluster"),
            )
        except Exception as exc:                      # pragma: no cover - defensive
            log.error("collector: failed to record pushed clinic_status: %s", exc)
            return jsonify(error="insert failed"), 500
        return jsonify(ok=True), 201

    @app.route("/collect/health", methods=["GET"])
    def health():
        # Still goes through the same before_request auth check as every
        # other route - a probe script needs the token too, same as a
        # patrol VM does. Keeping one auth path for the whole service is
        # simpler than carving out an exception for this one route.
        return jsonify(status="ok"), 200

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    config.ensure_directories()
    app = create_app()
    print(f"collector -> http://{config.COLLECTOR_HOST}:{config.COLLECTOR_PORT}")
    app.run(host=config.COLLECTOR_HOST, port=config.COLLECTOR_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
