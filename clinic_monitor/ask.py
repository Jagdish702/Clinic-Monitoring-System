"""
On-demand query: watch a clinic for a while and report what is happening.

This is the request/response counterpart to ``main.py``. The monitoring loop is
deliberately person-gated - it only spends a Gemini call when YOLO sees someone.
A question like "what is happening there?" has to be answered even when the
room is empty, so this tool watches for a fixed window, tracks activity with the
cheap stages, then describes the most interesting frame per camera.

    # drive the phone: open the app, find the clinic, open its feed, watch
    python ask.py --open "CUREBAY NIMAPADA" --duration 60

    # analyse whatever is already on screen, using clinics.json
    python ask.py --clinic "CUREBAY CHHAITANA" --duration 60 \
        --question "is anyone waiting?"

    python ask.py --list-clinics
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from ai.gemini_analyzer import GeminiAnalyzer  # noqa: E402
from capture.adb_capture import CaptureError  # noqa: E402
from capture.frame_reader import FrameReader  # noqa: E402
from control.navigator import NavigationError, PhoneNavigator, build_clinic  # noqa: E402
from detection.yolo_detector import Detection, YoloDetector, draw_detections  # noqa: E402
from motion.motion_detector import MotionRegistry  # noqa: E402
from storage.database import Database  # noqa: E402
from storage.logger import EventLogger  # noqa: E402

log = logging.getLogger("ask")


@dataclass
class CameraObservation:
    """What the cheap stages saw on one camera over the watch window."""

    camera_name: str
    frames: int = 0
    motion_frames: int = 0
    max_persons: int = 0
    labels: Dict[str, int] = field(default_factory=dict)
    best_frame: Optional[np.ndarray] = None
    best_detections: List[Detection] = field(default_factory=list)
    best_score: float = -1.0
    best_at: float = 0.0

    def consider(
        self,
        frame: np.ndarray,
        detections: List[Detection],
        persons: int,
        motion_score: float,
        when: float,
    ) -> None:
        """Keep the most informative frame: people first, then most motion."""
        score = persons * 10.0 + motion_score
        if score > self.best_score or self.best_frame is None:
            self.best_score = score
            self.best_frame = frame.copy()
            self.best_detections = detections
            self.best_at = when

    @property
    def activity(self) -> str:
        if self.max_persons:
            noun = "person" if self.max_persons == 1 else "people"
            return f"up to {self.max_persons} {noun}"
        if self.motion_frames:
            return "movement, no people"
        return "no activity"


def watch(
    clinic: "config.ClinicConfig",
    duration: float,
    interval: float,
) -> Dict[str, CameraObservation]:
    """Run stages 1-3 over a fixed window and collect per-camera observations."""
    reader = FrameReader(clinic, interval=interval)
    detector = YoloDetector()
    detector.load()
    motion = MotionRegistry()
    observations: Dict[str, CameraObservation] = {}

    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        started = time.monotonic()
        for tile in reader.read():
            obs = observations.setdefault(
                tile.camera_name, CameraObservation(tile.camera_name)
            )
            obs.frames += 1

            result = motion.update(clinic.name, tile.camera_name, tile.image)
            if result.warming_up:
                continue
            if result.motion:
                obs.motion_frames += 1

            # Unlike the monitoring loop, run YOLO on the first few frames even
            # without motion - a still, occupied room is a valid answer.
            if not result.motion and obs.frames > 3:
                obs.consider(tile.image, [], 0, 0.0, tile.captured_at)
                continue

            detections = detector.detect(tile.image)
            persons = YoloDetector.persons(detections)
            obs.max_persons = max(obs.max_persons, len(persons))
            for det in detections:
                obs.labels[det.label] = obs.labels.get(det.label, 0) + 1
            obs.consider(
                tile.image, detections, len(persons), result.score, tile.captured_at
            )

        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval - elapsed))

    reader.close()
    return observations


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ask what is happening at a clinic")
    parser.add_argument(
        "--open",
        dest="open_device",
        metavar="NAME",
        help="drive the phone: launch Hik-Connect, find this device and open its "
        "live view before watching (no clinics.json entry needed)",
    )
    parser.add_argument(
        "--list-clinics",
        action="store_true",
        help="list every device in the Hik-Connect app and exit",
    )
    parser.add_argument("--serial", help="adb serial, when several devices are attached")
    parser.add_argument("--clinic", help="clinic name (defaults to the first configured)")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds to watch")
    parser.add_argument(
        "--interval", type=float, default=config.CAPTURE_INTERVAL_SEC, help="capture interval"
    )
    parser.add_argument("--question", default="", help="extra question for the model")
    parser.add_argument("--no-log", action="store_true", help="do not write events to the database")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)-20s %(message)s")
    for noisy in ("httpx", "google_genai", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config.ensure_directories()

    # --list-clinics and --open drive the phone; everything else reads
    # clinics.json and analyses whatever is already on screen.
    if args.list_clinics:
        navigator = PhoneNavigator(serial=args.serial)
        try:
            navigator.launch()
            names = navigator.list_devices()
        except NavigationError as exc:
            print(f"could not read the device list: {exc}")
            return 1
        print(f"{len(names)} devices in Hik-Connect (in list order):")
        for index, name in enumerate(names, 1):
            print(f"  {index:>2}. {name}")
        return 0

    if args.open_device:
        navigator = PhoneNavigator(serial=args.serial)
        try:
            print(f"opening Hik-Connect and navigating to {args.open_device!r}...")
            navigator.launch()
            regions = navigator.open_live_view(args.open_device)
        except NavigationError as exc:
            print(f"navigation failed: {exc}")
            return 1
        clinic = build_clinic(args.open_device, regions, adb_serial=args.serial)
        print(
            f"live view open: {clinic.grid[0]}x{clinic.grid[1]} grid, "
            f"{len(clinic.cameras)} cameras (regions read from the app)"
        )
    else:
        clinics = config.load_clinics()
        clinic = clinics[0]
        if args.clinic:
            matches = [c for c in clinics if c.name.lower() == args.clinic.lower()]
            if not matches:
                print(
                    f"unknown clinic {args.clinic!r}; configured: "
                    f"{[c.name for c in clinics]}. Use --open to navigate there instead."
                )
                return 1
            clinic = matches[0]

    print(f"watching {clinic.name} for {args.duration:.0f}s "
          f"({len(clinic.active_cameras())} cameras, every {args.interval:.1f}s)...")
    try:
        observations = watch(clinic, args.duration, args.interval)
    except CaptureError as exc:
        print(f"capture failed: {exc}")
        return 1

    if not observations:
        print("no frames captured - is the device awake and Hik-Connect in the foreground?")
        return 1

    # One description per camera, so the cooldown that protects the monitoring
    # loop must not apply here.
    analyzer = GeminiAnalyzer(cooldown_sec=0.0)
    event_logger = None if args.no_log else EventLogger(db=Database())

    print(f"\n{'=' * 72}\n{clinic.name} - {datetime.now():%Y-%m-%d %H:%M:%S}\n{'=' * 72}")
    for name, obs in observations.items():
        objects = ", ".join(f"{k} x{v}" for k, v in sorted(obs.labels.items())) or "-"
        print(f"\n{name}")
        print(f"  frames={obs.frames}  motion={obs.motion_frames}  {obs.activity}")
        print(f"  objects seen: {objects}")

        if obs.best_frame is None:
            continue

        analysis = analyzer.analyze(
            obs.best_frame,
            clinic_name=clinic.name,
            camera_name=name,
            detections=obs.best_detections,
            timestamp=datetime.fromtimestamp(obs.best_at).strftime("%Y-%m-%d %H:%M:%S"),
            extra_question=args.question or None,
        )
        if analysis is None:
            print("  gemini: unavailable (quota or error) - see the counts above")
            continue

        print(f"  severity: {analysis.severity}   clinic: {analysis.clinic_status}")
        print(f"  -> {analysis.description}")
        if analysis.reason:
            print(f"     ({analysis.reason})")
        if analysis.answer:
            print(f"  Q: {args.question}\n  A: {analysis.answer}")

        if event_logger is not None:
            event_logger.log_event(
                clinic_name=clinic.name,
                camera_name=name,
                description=analysis.description,
                severity=analysis.severity,
                frame=draw_detections(obs.best_frame, obs.best_detections),
                confidence=max((d.confidence for d in obs.best_detections), default=None),
                clinic_status=analysis.clinic_status,
                reason=analysis.reason,
                staff_present=analysis.staff_present,
                patient_present=analysis.patient_present,
                unusual_activity=analysis.unusual_activity,
                immediate_attention=analysis.immediate_attention,
                person_count=obs.max_persons,
                detections=obs.best_detections,
                source="ask",
                when=datetime.fromtimestamp(obs.best_at),
            )

    print(f"\n{'=' * 72}")
    print(f"gemini: {analyzer.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
