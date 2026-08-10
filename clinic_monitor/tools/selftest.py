"""
Offline self-test - exercises the pipeline without a phone or an API key.

Synthetic frames (a moving blob on a static background) are pushed through the
motion gate, then through YOLO and the storage layer if those are available.

    python tools/selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from ai.gemini_analyzer import GeminiAnalyzer, parse_response  # noqa: E402
from capture.frame_reader import (  # noqa: E402
    camera_regions,
    detect_grid,
    split_frame,
)
from motion.motion_detector import MotionDetector  # noqa: E402
from storage.database import Database  # noqa: E402
from storage.logger import EventLogger  # noqa: E402

PASS, FAIL = "  PASS", "  FAIL"


def _frame(offset: int = 0, size=(720, 1280)) -> np.ndarray:
    """Static gradient background plus a bright block that moves with offset."""
    h, w = size
    frame = np.tile(np.linspace(30, 90, w, dtype=np.uint8), (h, 1))
    frame = np.stack([frame] * 3, axis=-1)
    if offset >= 0:
        x = 80 + offset
        frame[300:520, x : x + 160] = 235
    return np.ascontiguousarray(frame)


def test_layout() -> bool:
    print("\n[1] layout / cropping")
    clinic = config.load_clinics()[0]
    regions = camera_regions(clinic)
    tiles = split_frame(_frame(-1), clinic)
    print(f"  clinic={clinic.name} grid={clinic.grid} cameras={len(regions)}")
    for tile in tiles:
        print(f"    {tile.camera_name:<16} {tile.size[0]}x{tile.size[1]} px")
    ok = len(tiles) == len(regions) and all(t.image.size > 0 for t in tiles)
    print(PASS if ok else FAIL)
    return ok


def _grid_frame(rows: int, cols: int, size=(591, 1280), dark: bool = False) -> np.ndarray:
    """Textured tiles separated by the black dividers Hik-Connect draws."""
    h, w = size
    rng = np.random.default_rng(7)
    base = 12 if dark else 120
    frame = rng.integers(base - 10, base + 10, (h, w, 3), dtype=np.uint8)
    for i in range(1, cols):
        x = round(w * i / cols)
        frame[:, x - 2 : x + 3] = 0
    for i in range(1, rows):
        y = round(h * i / rows)
        frame[y - 2 : y + 3, :] = 0
    return frame


def test_layout_detection() -> bool:
    print("\n[2] layout auto-detection")
    cases = [
        ("2x2 grid", _grid_frame(2, 2), (2, 2)),
        ("3x3 grid", _grid_frame(3, 3), (3, 3)),
        ("full screen", _frame(-1), (1, 1)),
        # A night view is almost black everywhere; without a contrast check it
        # would look like dividers in every direction.
        ("dark full screen", _grid_frame(1, 1, dark=True), (1, 1)),
    ]
    ok = True
    for label, frame, expected in cases:
        got = detect_grid(frame)
        good = got == expected
        ok = ok and good
        print(f"  {label:<18} -> {got[0]}x{got[1]}  expected {expected[0]}x{expected[1]}"
              f"  {'ok' if good else 'WRONG'}")

    # An unconfigured layout must fall back to positional names, never reuse
    # the configured camera names for the wrong tiles.
    clinic = config.load_clinics()[0]
    names = [name for name, _ in camera_regions(clinic, grid=(1, 1))]
    configured = [c.name for c in clinic.active_cameras()]
    mismatch_ok = tuple(clinic.grid) == (1, 1) or names == [config.LAYOUT_SINGLE_VIEW_NAME]
    print(f"  single-view naming -> {names} (configured: {configured[:2]}...)")
    ok = ok and mismatch_ok
    print(PASS if ok else FAIL)
    return ok


def test_motion() -> bool:
    print("\n[3] motion detection")
    detector = MotionDetector()
    for _ in range(config.MOTION_WARMUP_FRAMES + 4):
        detector.update(_frame(0))
    still = detector.update(_frame(0))
    moved = detector.update(_frame(220))
    print(f"  static frame -> motion={still.motion} area={still.area_ratio}")
    print(f"  moved  frame -> motion={moved.motion} area={moved.area_ratio}")
    ok = (not still.motion) and moved.motion
    print(PASS if ok else FAIL)
    return ok


def test_storage() -> bool:
    print("\n[4] storage (sqlite + screenshots)")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        db = Database(tmp_path / "test.db")
        logger = EventLogger(db=db, screenshot_dir=tmp_path / "shots")
        event_id = logger.log_event(
            clinic_name="Test Clinic",
            camera_name="Reception",
            description="Staff opening clinic.",
            severity="Low",
            frame=_frame(50),
            confidence=0.91,
            clinic_status="Open",
            reason="Reception staff arrived.",
            staff_present=True,
            person_count=1,
            motion_score=0.12,
            detections=[{"label": "person", "confidence": 0.91, "box": [1, 2, 3, 4]}],
            source="selftest",
        )
        high_id = logger.log_event(
            clinic_name="Test Clinic",
            camera_name="Waiting Area",
            description="Person on the floor near the entrance.",
            severity="High",
            frame=_frame(90),
            immediate_attention=True,
            person_count=2,
            source="selftest",
        )
        events = db.get_events(limit=10)
        highs = db.get_events(severity="High")
        counts = db.counts_by_severity()
        shot = events[0]["screenshot_path"]
        shot_exists = (tmp_path / "shots" / shot).is_file() if shot else False
        print(f"  inserted ids: {event_id}, {high_id}")
        print(f"  rows={len(events)} high={len(highs)} counts={counts}")
        print(f"  newest first: {[e['severity'] for e in events]}")
        print(f"  screenshot written: {shot_exists} ({shot})")
        ok = (
            len(events) == 2
            and len(highs) == 1
            and counts["Total"] == 2
            and events[0]["ts_epoch"] >= events[1]["ts_epoch"]
            and shot_exists
            and events[0]["detections"] is not None
        )
        db.close()  # release the sqlite handle before the temp dir is removed
    print(PASS if ok else FAIL)
    return ok


def test_gemini_parsing() -> bool:
    print("\n[5] gemini response handling (offline)")
    analyzer = GeminiAnalyzer(api_key="", enabled=False)
    samples = [
        '{"description":"Staff opening clinic.","clinic_status":"Open",'
        '"severity":"Low","reason":"Reception staff arrived."}',
        '```json\n{"description":"Patient fell.","severity":"critical",'
        '"immediate_attention":"true","clinic_status":"open"}\n```',
        'Here is the result:\n{"description":"Empty room","severity":"nonsense"}',
    ]
    ok = True
    for raw in samples:
        data = parse_response(raw)
        if data is None:
            ok = False
            print(f"  could not parse: {raw[:40]}...")
            continue
        analysis = analyzer._to_analysis(data, 0)
        print(f"  -> severity={analysis.severity:<6} status={analysis.clinic_status:<7} "
              f"attention={analysis.immediate_attention}  {analysis.description[:38]!r}")
    # The escalation rule: immediate_attention forces High.
    escalated = analyzer._to_analysis(parse_response(samples[1]), 0)
    ok = ok and escalated.severity == "High" and escalated.clinic_status == "Open"
    print(f"  skipped when disabled: {not analyzer.should_analyze('c', 'cam')}")
    print(PASS if ok else FAIL)
    return ok


def test_yolo() -> bool:
    print("\n[6] YOLOv8n (optional)")
    try:
        from detection.yolo_detector import YoloDetector
        detector = YoloDetector()
        detector.load()
    except Exception as exc:
        print(f"  skipped: {exc}")
        return True
    detections = detector.detect(_frame(120))
    print(f"  model loaded, {len(detections)} detections on a synthetic frame, "
          f"avg {detector.avg_inference_ms} ms")
    print(PASS)
    return True


def main() -> int:
    config.ensure_directories()
    print("clinic_monitor self-test")
    print(f"  db        : {config.DB_PATH}")
    print(f"  screenshots: {config.SCREENSHOT_DIR}")
    results = [
        test_layout(),
        test_layout_detection(),
        test_motion(),
        test_storage(),
        test_gemini_parsing(),
        test_yolo(),
    ]
    print("\n" + ("ALL CHECKS PASSED" if all(results) else "SOME CHECKS FAILED"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
