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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import config

log = logging.getLogger(__name__)

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
    acknowledged        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts       ON events (ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_events_severity ON events (severity, ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_events_clinic   ON events (clinic_name, ts_epoch DESC);

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
    source          TEXT    NOT NULL DEFAULT 'patrol'
);
CREATE INDEX IF NOT EXISTS idx_obs_day    ON observations (day, clinic_name, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_obs_clinic ON observations (clinic_name, ts_epoch);
"""

OBSERVATION_COLUMNS = (
    "timestamp", "ts_epoch", "day", "clinic_name", "camera_name",
    "frames", "motion_frames", "max_persons", "health_status",
    "brightness", "detail", "edge_ratio", "flat_ratio", "frame_change",
    "clinic_status", "severity", "unusual", "description", "source",
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
)


class Database:
    """Small helper around the ``events`` table."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
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

    def close(self) -> None:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    # -- writes ------------------------------------------------------------ #
    def insert_event(self, event: Dict[str, Any]) -> int:
        row = {key: event.get(key) for key in EVENT_COLUMNS}
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
            return int(cursor.lastrowid)

    def insert_observation(self, observation: Dict[str, Any]) -> int:
        row = {key: observation.get(key) for key in OBSERVATION_COLUMNS}
        row["unusual"] = int(bool(row.get("unusual")))
        placeholders = ", ".join(f":{c}" for c in OBSERVATION_COLUMNS)
        sql = (
            f"INSERT INTO observations ({', '.join(OBSERVATION_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        with self.conn as conn:
            return int(conn.execute(sql, row).lastrowid)

    def get_observations(
        self, day: str, clinic_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Every observation for one day, oldest first (a day is a timeline)."""
        sql = "SELECT * FROM observations WHERE day = ?"
        params: List[Any] = [day]
        if clinic_name and clinic_name.lower() != "all":
            sql += " AND clinic_name = ?"
            params.append(clinic_name)
        sql += " ORDER BY ts_epoch ASC"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def observed_clinics(self, day: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT clinic_name AS c FROM observations WHERE day = ? "
            "ORDER BY c",
            (day,),
        ).fetchall()
        return [row["c"] for row in rows]

    def observed_days(self, limit: int = 30) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT day AS d FROM observations ORDER BY d DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["d"] for row in rows]

    def acknowledge(self, event_id: int) -> None:
        with self.conn as conn:
            conn.execute("UPDATE events SET acknowledged = 1 WHERE id = ?", (event_id,))

    # -- reads -------------------------------------------------------------- #
    def get_events(
        self,
        severity: Optional[str] = None,
        clinic_name: Optional[str] = None,
        camera_name: Optional[str] = None,
        since_epoch: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Newest first, optionally filtered."""
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
        if since_epoch:
            clauses.append("ts_epoch >= ?")
            params.append(since_epoch)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        sql = f"SELECT * FROM events {where} ORDER BY ts_epoch DESC LIMIT ? OFFSET ?"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def counts_by_severity(self, since_epoch: Optional[float] = None) -> Dict[str, int]:
        sql = "SELECT severity, COUNT(*) AS n FROM events"
        params: Sequence[Any] = ()
        if since_epoch:
            sql += " WHERE ts_epoch >= ?"
            params = (since_epoch,)
        sql += " GROUP BY severity"
        counts = {"High": 0, "Medium": 0, "Low": 0}
        for row in self.conn.execute(sql, params):
            counts[row["severity"]] = row["n"]
        counts["Total"] = sum(counts[s] for s in ("High", "Medium", "Low"))
        return counts

    def distinct(self, column: str) -> List[str]:
        if column not in {"clinic_name", "camera_name"}:
            raise ValueError(f"cannot list distinct values of {column!r}")
        rows = self.conn.execute(
            f"SELECT DISTINCT {column} AS v FROM events ORDER BY v"
        ).fetchall()
        return [row["v"] for row in rows]

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
