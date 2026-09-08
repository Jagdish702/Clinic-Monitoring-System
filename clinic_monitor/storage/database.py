"""
Stage 5 - SQLite storage.

One table, ``events``. WAL mode is enabled so the dashboard can read while the
monitor writes. Connections are per-thread because sqlite3 objects are not
shareable across threads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import config
from storage import collector_client

log = logging.getLogger(__name__)


def ignored_clause(column: str = "camera_name") -> tuple:
    """
    SQL that hides the channels config says to ignore, plus its parameters.

    Applied on the way out rather than on the way in: the rows written before
    a camera was ignored stay on disk, so the list can be changed back without
    losing history.
    """
    ignored = sorted(config.IGNORED_CAMERAS)
    if not ignored:
        return "", []
    placeholders = ", ".join("?" for _ in ignored)
    return f"{column} NOT IN ({placeholders})", list(ignored)


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    ts_epoch            REAL    NOT NULL,
    clinic_name         TEXT    NOT NULL,
    camera_name         TEXT    NOT NULL,
    description         TEXT    NOT NULL,
    severity            TEXT    NOT NULL,
    confidence          REAL,
    screenshot_path     TEXT,
    clinic_status       TEXT,
    reason              TEXT,
    staff_present       INTEGER DEFAULT 0,
    patient_present     INTEGER DEFAULT 0,
    unusual_activity    INTEGER DEFAULT 0,
    immediate_attention INTEGER DEFAULT 0,
    person_count        INTEGER DEFAULT 0,
    motion_score        REAL,
    detections          TEXT,
    source              TEXT    NOT NULL DEFAULT 'gemini',
    acknowledged        INTEGER NOT NULL DEFAULT 0,
    -- Which cluster (one Hik-Connect account, one emulator) and which state
    -- it belongs to - stamped from this VM's own CM_STATE_NAME/
    -- CM_CLUSTER_NAME, not supplied by callers. A single VM is always
    -- exactly one cluster, so there is nothing to get wrong or forget here.
    state               TEXT,
    cluster             TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts       ON events (ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_events_severity ON events (severity, ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_events_clinic   ON events (clinic_name, ts_epoch DESC);
-- idx_events_cluster is created in _migrate(), not here: on a database that
-- predates the state/cluster columns, CREATE TABLE IF NOT EXISTS above is a
-- no-op (the table already exists), so an index on those columns right here
-- would run before _migrate() has had a chance to add them.

-- One row per camera per patrol visit, whether or not anything happened.
-- `events` only records things worth alerting on; a daily report needs the
-- quiet observations too, otherwise "no activity between 13:00 and 14:00"
-- cannot be told apart from "nobody looked".
CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    ts_epoch        REAL    NOT NULL,
    day             TEXT    NOT NULL,          -- YYYY-MM-DD, for grouping
    clinic_name     TEXT    NOT NULL,
    camera_name     TEXT    NOT NULL,
    frames          INTEGER DEFAULT 0,
    motion_frames   INTEGER DEFAULT 0,
    max_persons     INTEGER DEFAULT 0,
    health_status   TEXT,
    brightness      REAL,
    detail          REAL,
    edge_ratio      REAL,
    flat_ratio      REAL,
    frame_change    REAL,
    clinic_status   TEXT,
    severity        TEXT,
    unusual         INTEGER DEFAULT 0,
    description     TEXT,
    -- Gemini's own read on whether anybody is in shot. Kept alongside the
    -- YOLO person count because the two fail differently: YOLO misses a
    -- person seated at a desk across a wide room, which is most of what a
    -- consulting-room camera shows.
    staff_present   INTEGER DEFAULT 0,
    patient_present INTEGER DEFAULT 0,
    source          TEXT    NOT NULL DEFAULT 'patrol',
    state           TEXT,
    cluster         TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_day     ON observations (day, clinic_name, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_obs_clinic  ON observations (clinic_name, ts_epoch);
-- idx_obs_cluster is created in _migrate() - same reason as idx_events_cluster above.

-- Whether the clinic's device could be reached at all, recorded on every
-- visit. Without this a skipped clinic leaves no trace: the patrol prints
-- "skipped: Device Offline" and moves on, so afterwards there is no way to say
-- when a site went down or came back. Both states are stored, because an
-- offline period is only bounded once a later visit finds it online again.
CREATE TABLE IF NOT EXISTS clinic_status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    ts_epoch    REAL    NOT NULL,
    day         TEXT    NOT NULL,
    clinic_name TEXT    NOT NULL,
    status      TEXT    NOT NULL,          -- 'online' | 'offline'
    reason      TEXT,
    state       TEXT,
    cluster     TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_day ON clinic_status (day, clinic_name, ts_epoch);
"""

OBSERVATION_COLUMNS = (
    "timestamp", "ts_epoch", "day", "clinic_name", "camera_name",
    "frames", "motion_frames", "max_persons", "health_status",
    "brightness", "detail", "edge_ratio", "flat_ratio", "frame_change",
    "clinic_status", "severity", "unusual", "description",
    "staff_present", "patient_present", "source", "state", "cluster",
)

EVENT_COLUMNS = (
    "timestamp",
    "ts_epoch",
    "clinic_name",
    "camera_name",
    "description",
    "severity",
    "confidence",
    "screenshot_path",
    "clinic_status",
    "reason",
    "staff_present",
    "patient_present",
    "unusual_activity",
    "immediate_attention",
    "person_count",
    "motion_score",
    "detections",
    "source",
    "state",
    "cluster",
)


class Database:
    """Small helper around the ``events`` table."""

    def __init__(self, path: Optional[Path] = None, push: bool = True) -> None:
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # False for the collector's own database (dashboard/collector.py):
        # a write landing there is already the final copy. Pushing it onward
        # would try to reach the collector from inside its own request
        # handler - at best a wasted round trip, at worst a self-loop if a
        # collector VM's .env is ever accidentally given its own
        # CM_COLLECTOR_URL. Relying on operators to never set that env var
        # is not a safeguard; this flag is.
        self._push = push
        self.init_schema()

    # -- connection handling ---------------------------------------------- #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._connect()
            self._local.conn = existing
        return existing

    def init_schema(self) -> None:
        with self.conn as conn:
            conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """
        Add columns the schema has grown since a database was created.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table exactly as it
        was, so a new column in SCHEMA never reaches a database that already
        has rows in it - which is every deployed one.
        """
        wanted = {
            "observations": {
                "staff_present": "INTEGER DEFAULT 0",
                "patient_present": "INTEGER DEFAULT 0",
                "state": "TEXT",
                "cluster": "TEXT",
            },
            "events": {
                "state": "TEXT",
                "cluster": "TEXT",
            },
            "clinic_status": {
                "state": "TEXT",
                "cluster": "TEXT",
            },
        }
        for table, columns in wanted.items():
            have = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for name, spec in columns.items():
                if name in have:
                    continue
                log.info("adding %s.%s", table, name)
                with self.conn as conn:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

        # Deferred from SCHEMA to here deliberately: on a database that
        # predates state/cluster, these columns only exist once the loop
        # above has run - an index on them declared in the static SCHEMA
        # script would run first and fail on every existing deployment.
        with self.conn as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_cluster ON events (state, cluster)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_cluster ON observations (state, cluster)"
            )

    def close(self) -> None:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    # -- writes ------------------------------------------------------------ #
    def insert_event(self, event: Dict[str, Any]) -> int:
        # Resolved once, used for both the local row and the pushed copy, so
        # the collector inserts the *originating* VM's state/cluster as-is
        # rather than falling back to its own (blank) identity - a collector
        # aggregates many clusters, so it has none of its own to stamp with.
        state = event.get("state") or config.STATE_NAME
        cluster = event.get("cluster") or config.CLUSTER_NAME
        pushed = {**event, "state": state, "cluster": cluster}

        row = {key: event.get(key) for key in EVENT_COLUMNS}
        row["state"], row["cluster"] = state, cluster
        if isinstance(row.get("detections"), (list, dict)):
            row["detections"] = json.dumps(row["detections"])
        for flag in (
            "staff_present",
            "patient_present",
            "unusual_activity",
            "immediate_attention",
        ):
            row[flag] = int(bool(row.get(flag)))

        placeholders = ", ".join(f":{c}" for c in EVENT_COLUMNS)
        sql = f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({placeholders})"
        with self.conn as conn:
            cursor = conn.execute(sql, row)
            row_id = int(cursor.lastrowid)
        if self._push:
            # Pushed as the resolved-but-otherwise-original event - not
            # `row` - so the collector's own insert_event() does its own
            # serialization fresh, the same as any other caller.
            collector_client.push("events", pushed)
        return row_id

    def insert_observation(self, observation: Dict[str, Any]) -> int:
        state = observation.get("state") or config.STATE_NAME
        cluster = observation.get("cluster") or config.CLUSTER_NAME
        pushed = {**observation, "state": state, "cluster": cluster}

        row = {key: observation.get(key) for key in OBSERVATION_COLUMNS}
        row["state"], row["cluster"] = state, cluster
        for flag in ("unusual", "staff_present", "patient_present"):
            row[flag] = int(bool(row.get(flag)))
        placeholders = ", ".join(f":{c}" for c in OBSERVATION_COLUMNS)
        sql = (
            f"INSERT INTO observations ({', '.join(OBSERVATION_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        with self.conn as conn:
            row_id = int(conn.execute(sql, row).lastrowid)
        if self._push:
            collector_client.push("observations", pushed)
        return row_id

    def get_observations(
        self, day: str, clinic_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Every observation for one day, oldest first (a day is a timeline)."""
        sql = "SELECT * FROM observations WHERE day = ?"
        params: List[Any] = [day]
        if clinic_name and clinic_name.lower() != "all":
            sql += " AND clinic_name = ?"
            params.append(clinic_name)
        hide, hide_params = ignored_clause()
        if hide:
            sql += f" AND {hide}"
            params.extend(hide_params)
        sql += " ORDER BY ts_epoch ASC"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def record_clinic_status(
        self,
        clinic_name: str,
        status: str,
        when: datetime,
        reason: str = "",
        state: Optional[str] = None,
        cluster: Optional[str] = None,
    ) -> None:
        """Note whether a clinic's device answered on this visit."""
        state = state or config.STATE_NAME
        cluster = cluster or config.CLUSTER_NAME
        with self.conn as conn:
            conn.execute(
                "INSERT INTO clinic_status "
                "(timestamp, ts_epoch, day, clinic_name, status, reason, state, cluster) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    when.astimezone().isoformat(timespec="seconds"),
                    when.timestamp(),
                    when.strftime("%Y-%m-%d"),
                    clinic_name,
                    status,
                    reason,
                    state,
                    cluster,
                ),
            )
        if not self._push:
            return
        collector_client.push(
            "clinic_status",
            {
                "clinic_name": clinic_name,
                "status": status,
                "when": when.astimezone().isoformat(timespec="seconds"),
                "reason": reason,
                "state": state,
                "cluster": cluster,
            },
        )

    def offline_periods(self, day: str) -> List[Dict[str, Any]]:
        """
        Stretches where a clinic could not be reached, one entry per outage.

        An outage runs from the first check that found the device offline to
        the first check that found it back. Both ends are only known to within
        one patrol lap, so `checked_from` (the last time it was seen working)
        is carried too - the real failure happened somewhere in between.
        """
        rows = self.conn.execute(
            "SELECT clinic_name, ts_epoch, timestamp, status, reason "
            "FROM clinic_status WHERE day = ? ORDER BY clinic_name, ts_epoch",
            (day,),
        ).fetchall()

        periods: List[Dict[str, Any]] = []
        by_clinic: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            by_clinic[row["clinic_name"]].append(row)

        for clinic, entries in by_clinic.items():
            open_period: Optional[Dict[str, Any]] = None
            last_online: Optional[sqlite3.Row] = None
            for entry in entries:
                if entry["status"] == "offline":
                    if open_period is None:
                        open_period = {
                            "clinic_name": clinic,
                            "from": entry["timestamp"],
                            "from_epoch": entry["ts_epoch"],
                            "last_seen_working": (
                                last_online["timestamp"] if last_online else None
                            ),
                            "reason": entry["reason"],
                            "checks": 0,
                        }
                    open_period["checks"] += 1
                else:
                    if open_period is not None:
                        open_period["to"] = entry["timestamp"]
                        open_period["to_epoch"] = entry["ts_epoch"]
                        open_period["ongoing"] = False
                        periods.append(open_period)
                        open_period = None
                    last_online = entry
            if open_period is not None:
                last = entries[-1]
                open_period["to"] = last["timestamp"]
                open_period["to_epoch"] = last["ts_epoch"]
                open_period["ongoing"] = True
                periods.append(open_period)

        for period in periods:
            minutes = (period["to_epoch"] - period["from_epoch"]) / 60
            period["minutes"] = round(minutes)
        periods.sort(key=lambda p: (-p["minutes"], p["clinic_name"]))
        return periods

    def clinic_status_summary(self, day: str) -> Dict[str, Any]:
        """Per-clinic totals for the day: checks, failures, current state."""
        rows = self.conn.execute(
            "SELECT clinic_name,"
            " COUNT(*) AS checks,"
            " SUM(CASE WHEN status='offline' THEN 1 ELSE 0 END) AS failures,"
            " MAX(ts_epoch) AS last_epoch"
            " FROM clinic_status WHERE day = ? GROUP BY clinic_name",
            (day,),
        ).fetchall()
        summary = {}
        for row in rows:
            latest = self.conn.execute(
                "SELECT status FROM clinic_status WHERE day=? AND clinic_name=?"
                " ORDER BY ts_epoch DESC LIMIT 1",
                (day, row["clinic_name"]),
            ).fetchone()
            summary[row["clinic_name"]] = {
                "checks": row["checks"],
                "failures": row["failures"],
                "current": latest["status"] if latest else "unknown",
            }
        return summary

    def camera_descriptions(self, limit: int = 4000) -> List[Dict[str, Any]]:
        """
        Recent scene descriptions, used to infer whether a camera looks indoors
        or outdoors. Drawn from all history, not one day, because a camera's
        role does not change and more samples make the inference safer.
        """
        hide, hide_params = ignored_clause()
        rows = self.conn.execute(
            "SELECT clinic_name, camera_name, description FROM events "
            "WHERE description IS NOT NULL AND description != '' "
            + (f"AND {hide} " if hide else "")
            + "ORDER BY ts_epoch DESC LIMIT ?",
            (*hide_params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def observed_clinics(self, day: str) -> List[str]:
        hide, hide_params = ignored_clause()
        rows = self.conn.execute(
            "SELECT DISTINCT clinic_name AS c FROM observations WHERE day = ? "
            + (f"AND {hide} " if hide else "")
            + "ORDER BY c",
            (day, *hide_params),
        ).fetchall()
        return [row["c"] for row in rows]

    def observed_days(self, limit: int = 30) -> List[str]:
        hide, hide_params = ignored_clause()
        rows = self.conn.execute(
            "SELECT DISTINCT day AS d FROM observations "
            + (f"WHERE {hide} " if hide else "")
            + "ORDER BY d DESC LIMIT ?",
            (*hide_params, int(limit)),
        ).fetchall()
        return [row["d"] for row in rows]

    def acknowledge(self, event_id: int) -> None:
        with self.conn as conn:
            conn.execute("UPDATE events SET acknowledged = 1 WHERE id = ?", (event_id,))

    # -- reads -------------------------------------------------------------- #
    @staticmethod
    def _event_clauses(
        severity: Optional[str] = None,
        clinic_name: Optional[str] = None,
        camera_name: Optional[str] = None,
        state: Optional[str] = None,
        cluster: Optional[str] = None,
        since_epoch: Optional[float] = None,
    ) -> tuple:
        """Shared WHERE-clause building for get_events/count_events."""
        clauses: List[str] = []
        params: List[Any] = []
        if severity and severity.lower() != "all":
            clauses.append("severity = ?")
            params.append(severity.capitalize())
        if clinic_name and clinic_name.lower() != "all":
            clauses.append("clinic_name = ?")
            params.append(clinic_name)
        if camera_name and camera_name.lower() != "all":
            clauses.append("camera_name = ?")
            params.append(camera_name)
        if state and state.lower() != "all":
            clauses.append("state = ?")
            params.append(state)
        if cluster and cluster.lower() != "all":
            clauses.append("cluster = ?")
            params.append(cluster)
        if since_epoch:
            clauses.append("ts_epoch >= ?")
            params.append(since_epoch)
        hide, hide_params = ignored_clause()
        if hide:
            clauses.append(hide)
            params.extend(hide_params)
        return clauses, params

    def get_events(
        self,
        severity: Optional[str] = None,
        clinic_name: Optional[str] = None,
        camera_name: Optional[str] = None,
        state: Optional[str] = None,
        cluster: Optional[str] = None,
        since_epoch: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Newest first, optionally filtered."""
        clauses, params = self._event_clauses(
            severity, clinic_name, camera_name, state, cluster, since_epoch
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        # Newest visit first, but cameras within a visit keep the order they
        # were written (Camera 01, 02, 03...). They now share one timestamp so
        # they group together, and without the id tiebreak they would come out
        # reversed.
        sql = (
            f"SELECT * FROM events {where} "
            "ORDER BY ts_epoch DESC, id ASC LIMIT ? OFFSET ?"
        )
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_events(
        self,
        severity: Optional[str] = None,
        clinic_name: Optional[str] = None,
        camera_name: Optional[str] = None,
        state: Optional[str] = None,
        cluster: Optional[str] = None,
        since_epoch: Optional[float] = None,
    ) -> int:
        """
        How many rows match a filter set, ignoring limit/offset - used for the
        "Showing X of Y" footer, which needs the true total, not the page size.
        """
        clauses, params = self._event_clauses(
            severity, clinic_name, camera_name, state, cluster, since_epoch
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM events {where}", params).fetchone()
        return int(row["n"])

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def counts_by_severity(self, since_epoch: Optional[float] = None) -> Dict[str, int]:
        sql = "SELECT severity, COUNT(*) AS n FROM events"
        clauses: List[str] = []
        params: List[Any] = []
        if since_epoch:
            clauses.append("ts_epoch >= ?")
            params.append(since_epoch)
        hide, hide_params = ignored_clause()
        if hide:
            clauses.append(hide)
            params.extend(hide_params)
        base_where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql += base_where + " GROUP BY severity"
        counts = {"High": 0, "Medium": 0, "Low": 0}
        for row in self.conn.execute(sql, params):
            counts[row["severity"]] = row["n"]
        counts["Total"] = sum(counts[s] for s in ("High", "Medium", "Low"))
        resolved = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM events{base_where}"
            f"{' AND' if base_where else ' WHERE'} acknowledged = 1",
            params,
        ).fetchone()
        counts["Resolved"] = resolved["n"]
        counts["Active"] = counts["Total"] - counts["Resolved"]
        return counts

    def group_counts(
        self,
        column: str,
        state: Optional[str] = None,
        cluster: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Same narrowing as distinct(), but with an event count per value - the
        number shown next to each state/cluster/clinic row in the sidebar tree.
        """
        if column not in {"clinic_name", "camera_name", "state", "cluster"}:
            raise ValueError(f"cannot group by {column!r}")
        clauses: List[str] = []
        params: List[Any] = []
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
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self.conn.execute(
            f"SELECT {column} AS v, COUNT(*) AS n FROM events {where}"
            f"GROUP BY {column} ORDER BY v",
            params,
        ).fetchall()
        return [{"value": r["v"], "count": r["n"]} for r in rows if r["v"]]

    def dashboard_summary(self, day: str) -> Dict[str, Any]:
        """
        Real numbers for the dashboard's stat-card row: how many clinics and
        cameras answered today, how many alerts fired today, and today's
        average model confidence. Every number here comes from data actually
        written today - a quiet day before any patrol has run reads as zero
        clinics checked, not a fabricated "all clear".
        """
        status = self.clinic_status_summary(day)
        clinics_total = len(status)
        clinics_offline = sum(1 for s in status.values() if s["current"] == "offline")

        hide, hide_params = ignored_clause()
        cam_rows = self.conn.execute(
            "SELECT clinic_name, camera_name,"
            " COUNT(*) AS checks,"
            " SUM(CASE WHEN health_status IN ('no_signal','frozen','obstructed')"
            "     THEN 1 ELSE 0 END) AS bad"
            " FROM observations WHERE day = ?"
            + (f" AND {hide}" if hide else "")
            + " GROUP BY clinic_name, camera_name",
            (day, *hide_params),
        ).fetchall()
        cameras_total = len(cam_rows)
        cameras_inactive = sum(
            1 for r in cam_rows if r["checks"] and r["bad"] == r["checks"]
        )

        day_start = datetime.strptime(day, "%Y-%m-%d").timestamp()
        day_end = day_start + 86400
        clauses = ["ts_epoch >= ?", "ts_epoch < ?"]
        params: List[Any] = [day_start, day_end]
        if hide:
            clauses.append(hide)
            params.extend(hide_params)
        where = " AND ".join(clauses)
        alerts_today = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM events WHERE {where}", params
        ).fetchone()["n"]
        alerts_last_hour = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM events WHERE {where} AND ts_epoch >= ?",
            (*params, time.time() - 3600),
        ).fetchone()["n"]
        avg_confidence = self.conn.execute(
            f"SELECT AVG(confidence) AS avg FROM events "
            f"WHERE {where} AND confidence IS NOT NULL",
            params,
        ).fetchone()["avg"]

        return {
            "clinics_total": clinics_total,
            "clinics_offline": clinics_offline,
            "clinics_online": clinics_total - clinics_offline,
            "cameras_total": cameras_total,
            "cameras_inactive": cameras_inactive,
            "cameras_active": cameras_total - cameras_inactive,
            "alerts_today": alerts_today,
            "alerts_last_hour": alerts_last_hour,
            "avg_confidence": avg_confidence,
        }

    def distinct(
        self,
        column: str,
        state: Optional[str] = None,
        cluster: Optional[str] = None,
    ) -> List[str]:
        """
        Distinct values of one column, optionally narrowed to one state
        and/or one cluster - the state -> cluster -> clinic drill-down chips
        each call this once, each level narrowing on the one above it.
        """
        if column not in {"clinic_name", "camera_name", "state", "cluster"}:
            raise ValueError(f"cannot list distinct values of {column!r}")
        clauses: List[str] = []
        params: List[Any] = []
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
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self.conn.execute(
            f"SELECT DISTINCT {column} AS v FROM events {where}ORDER BY v",
            params,
        ).fetchall()
        # NULL/blank filtered out - a row from before this feature existed,
        # or from a VM that never set CM_STATE_NAME/CM_CLUSTER_NAME, has
        # nothing to show as a chip.
        return [row["v"] for row in rows if row["v"]]

    # -- maintenance -------------------------------------------------------- #
    def purge_older_than(self, days: int) -> List[str]:
        """Delete old rows and return the screenshot paths that were dropped."""
        if days <= 0:
            return []
        cutoff = (datetime.now() - timedelta(days=days)).timestamp()
        rows = self.conn.execute(
            "SELECT screenshot_path FROM events WHERE ts_epoch < ? AND screenshot_path IS NOT NULL",
            (cutoff,),
        ).fetchall()
        with self.conn as conn:
            conn.execute("DELETE FROM events WHERE ts_epoch < ?", (cutoff,))
        return [row["screenshot_path"] for row in rows if row["screenshot_path"]]

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        raw = data.get("detections")
        if raw:
            try:
                data["detections"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data["detections"] = []
        else:
            data["detections"] = []
        for flag in (
            "staff_present",
            "patient_present",
            "unusual_activity",
            "immediate_attention",
            "acknowledged",
        ):
            if flag in data:
                data[flag] = bool(data[flag])
        return data
