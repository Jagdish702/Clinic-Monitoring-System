"""
Role-based login for the dashboard: Admin, Command Center, State Manager,
Cluster Manager. Off by default (config.AUTH_ENABLED=False) - every function
here is only ever wired into dashboard/app.py's before_request hook when a
VM's .env explicitly turns it on, so a deployment that never sets
CM_AUTH_ENABLED behaves exactly as it did before this file existed.

Sessions are Flask's built-in signed cookie (itsdangerous, bundled with
Flask - no new dependency), not a server-side store: stateless, survives a
restart, and needs nothing beyond app.secret_key. Passwords are hashed with
werkzeug.security (also already a Flask dependency) - a plaintext password
is only ever held in memory for the instant it takes to hash it, never
written to the database or logged.

No self-service password change anywhere: Admin is the only one who can
create or reset an account (via the CSV upload), by design - see
dashboard/app.py's /admin/users routes.
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable, Dict, Optional

from flask import abort, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config

ROLES = ("admin", "command_center", "state_manager", "cluster_manager")
# Roles that see the whole fleet, unrestricted - no scope check ever applies.
UNRESTRICTED_ROLES = ("admin", "command_center")


def hash_password(plaintext: str) -> str:
    return generate_password_hash(plaintext)


def verify_password(password_hash: str, plaintext: str) -> bool:
    return check_password_hash(password_hash, plaintext)


def bootstrap_admin(database) -> None:
    """
    Seed the first Admin account from CM_ADMIN_EMAIL/CM_ADMIN_PASSWORD, but
    only if the users table is empty - there is no self-registration, so
    without this a fresh deployment with AUTH_ENABLED=true would have no way
    to create the first account at all.
    """
    if database.list_users():
        return
    if not (config.ADMIN_EMAIL and config.ADMIN_PASSWORD):
        return
    database.create_user(
        name="Admin",
        email=config.ADMIN_EMAIL,
        password_hash=hash_password(config.ADMIN_PASSWORD),
        role="admin",
    )


def init_auth(app, database) -> None:
    """
    Wire authentication into a Flask app - called once from
    dashboard/app.py's create_app(). A no-op before_request is registered
    even when AUTH_ENABLED is false, so enabling the flag later never needs
    an app restart-and-reload of route registration, only a config change.
    """
    if config.AUTH_ENABLED:
        if not config.SECRET_KEY:
            raise RuntimeError(
                "CM_AUTH_ENABLED is true but CM_SECRET_KEY is not set - "
                "refusing to start with unsigned sessions. Set CM_SECRET_KEY "
                "to a random value before enabling auth."
            )
        app.secret_key = config.SECRET_KEY
        bootstrap_admin(database)

    @app.before_request
    def _require_login():
        if not config.AUTH_ENABLED:
            return None
        # Reachable without a session even when auth is enabled: the login
        # page itself, its own static assets (the logo above all - the
        # login page cannot show a logo it isn't allowed to fetch), and
        # screenshots (embedded directly in escalation emails, which have
        # no session to send).
        if (
            request.path == "/login"
            or request.path.startswith("/static/")
            or request.path.startswith("/screenshots/")
        ):
            return None
        user = current_user(database)
        if user is None:
            return redirect(url_for("login_page", next=request.path))
        return None

    app.jinja_env.globals["current_user_ctx"] = lambda: getattr(g, "user", None)


def current_user(database) -> Optional[Dict[str, Any]]:
    """The logged-in user's row, cached on flask.g for the rest of this
    request. Returns None when auth is disabled, no session cookie is
    present, or the session references a since-deleted account."""
    if not config.AUTH_ENABLED:
        return None
    if hasattr(g, "user"):
        return g.user
    user_id = session.get("user_id")
    user = database.get_user(user_id) if user_id else None
    g.user = user
    return user


def login_required(database_getter: Callable[[], Any]) -> Callable:
    """Decorator factory: database_getter is a zero-arg callable returning
    the app's Database instance (dashboard/app.py's routes are closures over
    one already, so this is usually just `lambda: database`)."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            if config.AUTH_ENABLED and current_user(database_getter()) is None:
                return redirect(url_for("login_page", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def role_required(*roles: str, database_getter: Callable[[], Any]) -> Callable:
    """Decorator factory restricting a route to specific roles. A no-op
    (always passes) when auth is disabled, matching every other gate here."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not config.AUTH_ENABLED:
                return view(*args, **kwargs)
            user = current_user(database_getter())
            if user is None:
                return redirect(url_for("login_page", next=request.path))
            if user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _resolve_clinic_location(database, clinic_name: str) -> tuple:
    """One clinic's (state, cluster) - same query as dashboard/app.py's own
    _clinic_location(), duplicated here rather than imported since that one
    is a closure nested inside create_app(), not a standalone function."""
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


def enforce_scope(
    database,
    user: Dict[str, Any],
    state: Optional[str] = None,
    cluster: Optional[str] = None,
    clinic_name: Optional[str] = None,
) -> None:
    """
    The one choke point every scoped route calls. Aborts with 403 if the
    given state/cluster/clinic falls outside what this user is allowed to
    see - never silently narrows or returns an empty page, so a manager
    hitting a URL outside their scope gets an honest "not allowed," not
    something that looks like a bug.
    """
    if not config.AUTH_ENABLED:
        return
    if user["role"] in UNRESTRICTED_ROLES:
        return

    if clinic_name and not (state or cluster):
        state, cluster = _resolve_clinic_location(database, clinic_name)

    if user["role"] == "state_manager":
        target_state = state
        if cluster and not target_state:
            target_state = config.state_for_cluster(cluster)
        if target_state != user["state"]:
            abort(403)
        return

    if user["role"] == "cluster_manager":
        target_cluster = cluster
        if state and not target_cluster:
            # A bare state-level URL is never within a Cluster Manager's own
            # scope, regardless of which state it is.
            abort(403)
        if target_cluster != user["cluster"]:
            abort(403)
        return

    abort(403)
