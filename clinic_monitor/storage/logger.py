"""
Stage 5 - event logger.

Saves the evidence screenshot to disk and writes one row to SQLite. Screenshot
paths are stored relative to ``config.SCREENSHOT_DIR`` so the database stays
portable and the dashboard can serve them safely.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import cv2
import numpy as np

import config
from storage.database import Database

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str) -> str:
    cleaned = _SAFE.sub("_", (text or "unknown").strip())
    return cleaned.strip("_") or "unknown"


class EventLogger:
    """Writes events (screenshot + database row) for the whole pipeline."""

    def __init__(
        self,
        db: Optional[Database] = None,
        screenshot_dir: Optional[Path] = None,
        save_screenshots: Optional[bool] = None,
    ) -> None:
        self.db = db or Database()
        self.screenshot_dir = Path(screenshot_dir or config.SCREENSHOT_DIR)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.save_screenshots = (
            config.SAVE_SCREENSHOTS if save_screenshots is None else save_screenshots
        )
        self._lock = threading.Lock()
        self.events_written = 0

    # -- screenshots ------------------------------------------------------- #
    def save_screenshot(
        self,
        frame: np.ndarray,
        clinic_name: str,
        camera_name: str,
        when: Optional[datetime] = None,
    ) -> Optional[str]:
        """Write the JPEG and return its path relative to the screenshot dir."""
        if not self.save_screenshots or frame is None or frame.size == 0:
            return None
        when = when or datetime.now()
        rel_dir = Path(_slug(clinic_name)) / when.strftime("%Y-%m-%d")
        target_dir = self.screenshot_dir / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{when.strftime('%H%M%S_%f')[:-3]}_{_slug(camera_name)}.jpg"
        full_path = target_dir / filename
        try:
            ok = cv2.imwrite(
                str(full_path),
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), config.SCREENSHOT_JPEG_QUALITY],
            )
        except Exception as exc:
            log.error("failed to write screenshot %s: %s", full_path, exc)
            return None
        if not ok:
            log.error("cv2.imwrite refused to write %s", full_path)
            return None
        return (rel_dir / filename).as_posix()

    def absolute_screenshot_path(self, relative: str) -> Path:
        return self.screenshot_dir / relative

    # -- events ------------------------------------------------------------ #
    def log_event(
        self,
        clinic_name: str,
        camera_name: str,
        description: str,
        severity: str,
        frame: Optional[np.ndarray] = None,
        confidence: Optional[float] = None,
        clinic_status: Optional[str] = None,
        reason: str = "",
        staff_present: bool = False,
        patient_present: bool = False,
        unusual_activity: bool = False,
        immediate_attention: bool = False,
        person_count: int = 0,
        motion_score: Optional[float] = None,
        detections: Optional[Sequence[Any]] = None,
        source: str = "gemini",
        when: Optional[datetime] = None,
    ) -> int:
        """Persist one event and return its row id (0 if the write failed)."""
        when = when or datetime.now()
        screenshot_path = (
            self.save_screenshot(frame, clinic_name, camera_name, when)
            if frame is not None
            else None
        )

        payload: Dict[str, Any] = {
            "timestamp": when.astimezone().isoformat(timespec="seconds"),
            "ts_epoch": when.timestamp(),
            "clinic_name": clinic_name,
            "camera_name": camera_name,
            "description": description,
            "severity": severity,
            "confidence": float(confidence) if confidence is not None else None,
            "screenshot_path": screenshot_path,
            "clinic_status": clinic_status,
            "reason": reason,
            "staff_present": staff_present,
            "patient_present": patient_present,
            "unusual_activity": unusual_activity,
            "immediate_attention": immediate_attention,
            "person_count": person_count,
            "motion_score": motion_score,
            "detections": [
                d.as_dict() if hasattr(d, "as_dict") else d for d in (detections or [])
            ],
            "source": source,
        }

        try:
            with self._lock:
                event_id = self.db.insert_event(payload)
                self.events_written += 1
        except Exception as exc:
            log.error("failed to insert event: %s", exc)
            return 0

        level = logging.WARNING if severity in ("High", "Medium") else logging.INFO
        log.log(
            level,
            "[%s] %s/%s - %s (%s)",
            severity.upper(),
            clinic_name,
            camera_name,
            description,
            source,
        )
        return event_id

    # -- maintenance -------------------------------------------------------- #
    def purge_old_data(self, days: Optional[int] = None) -> int:
        """Drop old rows plus their screenshots. Returns files removed."""
        days = config.SCREENSHOT_RETENTION_DAYS if days is None else days
        removed = 0
        for relative in self.db.purge_older_than(days):
            path = self.absolute_screenshot_path(relative)
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                log.debug("could not delete %s: %s", path, exc)
        if removed:
            log.info("purged %d screenshots older than %d days", removed, days)
        return removed
