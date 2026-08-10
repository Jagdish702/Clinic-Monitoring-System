"""
Insert a handful of demo events so the dashboard can be reviewed before any
hardware is connected.

    python tools/seed_demo.py            # add demo rows
    python tools/seed_demo.py --clear    # remove them again
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from storage.database import Database  # noqa: E402
from storage.logger import EventLogger  # noqa: E402

DEMO_SOURCE = "demo"

DEMO_EVENTS = [
    ("Clinic A", "Reception", "Staff opening clinic.", "Low", "Open",
     "Reception staff arrived and switched on the lights.", True, False, False, False, 1, 0.93),
    ("Clinic A", "Waiting Area", "Six patients waiting, all seated.", "Low", "Open",
     "Normal morning queue.", True, True, False, False, 6, 0.88),
    ("Clinic A", "Consultation", "Clinician examining a patient at the desk.", "Low", "Open",
     "Routine consultation in progress.", True, True, False, False, 2, 0.91),
    ("Clinic A", "Waiting Area", "Waiting area is crowded and people are standing.", "Medium", "Open",
     "More patients than seats; possible long wait.", True, True, True, False, 11, 0.86),
    ("Clinic B", "Entrance", "A person is lying on the floor near the entrance.", "High", "Open",
     "Possible fall or medical emergency - immediate attention required.",
     False, True, True, True, 1, 0.95),
    ("Clinic B", "Reception", "Movement in the reception area after closing hours.", "Medium", "Closed",
     "Activity detected outside opening hours.", False, False, True, False, 1, 0.71),
]


def _demo_frame(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    frame = np.full((360, 640, 3), 40, dtype=np.uint8)
    frame[:, :, 1] = 46
    for _ in range(4):
        x, y = rng.integers(20, 500), rng.integers(20, 260)
        w, h = rng.integers(40, 120), rng.integers(40, 90)
        shade = int(rng.integers(90, 210))
        frame[y : y + h, x : x + w] = shade
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="seed demo dashboard data")
    parser.add_argument("--clear", action="store_true", help="delete demo rows instead")
    args = parser.parse_args()

    config.ensure_directories()
    db = Database()

    if args.clear:
        with db.conn as conn:
            deleted = conn.execute(
                "DELETE FROM events WHERE source = ?", (DEMO_SOURCE,)
            ).rowcount
        print(f"removed {deleted} demo events")
        db.close()
        return 0

    logger = EventLogger(db=db)
    now = datetime.now()
    for index, row in enumerate(DEMO_EVENTS):
        (clinic, camera, description, severity, status, reason,
         staff, patient, unusual, attention, people, conf) = row
        logger.log_event(
            clinic_name=clinic,
            camera_name=camera,
            description=description,
            severity=severity,
            frame=_demo_frame(index),
            confidence=conf,
            clinic_status=status,
            reason=reason,
            staff_present=staff,
            patient_present=patient,
            unusual_activity=unusual,
            immediate_attention=attention,
            person_count=people,
            motion_score=round(0.05 + index * 0.03, 3),
            detections=[{"label": "person", "confidence": conf, "box": [10, 10, 90, 200]}],
            source=DEMO_SOURCE,
            when=now - timedelta(minutes=7 * (len(DEMO_EVENTS) - index)),
        )
    print(f"inserted {len(DEMO_EVENTS)} demo events into {config.DB_PATH}")
    print("run `python dashboard/app.py` and open "
          f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
