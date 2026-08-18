"""
Continuous patrol: rotate through every clinic, one after another, forever.

    Clinic 1 -> analyse -> Clinic 2 -> analyse -> ... -> Clinic N -> back to 1

One phone can only show one clinic at a time, so "continuous monitoring across
all clinics" means visiting them in turn rather than watching them at once.
Each visit navigates the app, watches for a configurable window, describes what
changed, and files events into the same database and dashboard as everything
else (tagged ``source = "patrol"``).

    patrol.bat 60                     one minute per clinic, forever
    patrol.bat 120                    two minutes per clinic
    patrol.py --duration 60 --rounds 2
    patrol.py --duration 60 --clinics BANAMALIPUR,NAYAHAT
    patrol.py --duration 60 --all-cameras     describe idle cameras too

Stop it with Ctrl+C; it finishes the current clinic and prints a summary.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from analysis.camera_health import CameraHealth, HealthStatus, assess_sequence  # noqa: E402
from ask import CameraObservation, watch  # noqa: E402
from ai.gemini_analyzer import GeminiAnalyzer  # noqa: E402
from capture.adb_capture import CaptureError  # noqa: E402
import report as reporting  # noqa: E402
from control import emulator  # noqa: E402
from control.emulator import EmulatorError  # noqa: E402
from control.navigator import (  # noqa: E402
    NavigationError,
    PhoneNavigator,
    build_clinic,
)
from detection.yolo_detector import YoloDetector, draw_detections  # noqa: E402
from storage.database import Database  # noqa: E402
from storage.logger import EventLogger  # noqa: E402

log = logging.getLogger("patrol")

SEVERITY_MARK = {"High": "!!", "Medium": " !", "Low": "  "}


class PatrolStats:
    """Running totals for the whole patrol, printed on exit."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.rounds = 0
        self.visits = 0
        self.skipped: Dict[str, str] = {}      # clinic -> last reason
        self.events = 0
        self.gemini_calls = 0
        self.by_severity = {"High": 0, "Medium": 0, "Low": 0}
        self.highlights: List[str] = []
        self.faults: Dict[str, HealthStatus] = {}

    def summary(self) -> str:
        mins = (time.monotonic() - self.started) / 60
        return (
            f"{mins:.1f} min | rounds={self.rounds} visits={self.visits} "
            f"events={self.events} "
            f"(High {self.by_severity['High']}, Medium {self.by_severity['Medium']}, "
            f"Low {self.by_severity['Low']}) gemini={self.gemini_calls} "
            f"skipped={len(self.skipped)}"
        )


def _observation_row(
    clinic_name: str,
    camera: str,
    obs: CameraObservation,
    health: Optional[CameraHealth],
) -> dict:
    """One row per camera per visit - the quiet ones matter for the report."""
    when = datetime.fromtimestamp(obs.best_at) if obs.best_at else datetime.now()
    row = {
        "timestamp": when.astimezone().isoformat(timespec="seconds"),
        "ts_epoch": when.timestamp(),
        "day": when.strftime("%Y-%m-%d"),
        "clinic_name": clinic_name,
        "camera_name": camera,
        "frames": obs.frames,
        "motion_frames": obs.motion_frames,
        "max_persons": obs.max_persons,
        "health_status": health.status.value if health else None,
        "brightness": health.brightness if health else None,
        "detail": health.detail if health else None,
        "edge_ratio": health.edge_ratio if health else None,
        "flat_ratio": health.flat_ratio if health else None,
        "frame_change": health.motion_between_frames if health else None,
        "clinic_status": None,
        "severity": None,
        "unusual": False,
        "description": "",
        "source": "patrol",
    }
    return row


def _save(event_logger: Optional[EventLogger], row: dict) -> None:
    if event_logger is None:
        return
    try:
        event_logger.db.insert_observation(row)
    except Exception as exc:                      # never let logging stop a patrol
        log.error("could not record observation: %s", exc)


def visit(
    navigator: PhoneNavigator,
    name: str,
    duration: float,
    interval: float,
    detector: YoloDetector,
    analyzer: GeminiAnalyzer,
    event_logger: Optional[EventLogger],
    stats: PatrolStats,
    all_cameras: bool,
    question: str,
) -> None:
    """Navigate to one clinic, watch it, describe and log what was seen."""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{stamp}] {name}")

    regions = navigator.open_live_view(name)          # raises on offline/failure
    clinic = build_clinic(name, regions, adb_serial=navigator.serial)
    observations = watch(clinic, duration, interval, detector=detector)
    stats.visits += 1

    if not observations:
        print("    no frames captured")
        return

    for camera, obs in observations.items():
        active = obs.max_persons > 0 or obs.motion_frames > 0

        health = assess_sequence(obs.samples) if obs.samples else None
        record = _observation_row(clinic.name, camera, obs, health)

        # A camera showing no stream will never show activity, so describing it
        # is money spent to be told the screen is black. Report the fault.
        if health is not None and health.status is not HealthStatus.OK:
            print(f"    {camera:<12} CAMERA FAULT: {health.status.label}")
            stats.faults[f"{clinic.name}/{camera}"] = health.status
            if not health.usable:
                _save(event_logger, record)
                continue

        # Describing a camera that saw nothing costs an API call to be told
        # nothing happened. Idle cameras are reported from the cheap stages
        # instead, unless --all-cameras is given.
        if not active and not all_cameras:
            print(f"    {camera:<12} idle (no motion, no people)")
            _save(event_logger, record)
            continue

        analysis = None
        if obs.best_frame is not None:
            analysis = analyzer.analyze(
                obs.best_frame,
                clinic_name=clinic.name,
                camera_name=camera,
                detections=obs.best_detections,
                timestamp=datetime.fromtimestamp(obs.best_at).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                extra_question=question or None,
            )

        if analysis is None:
            print(f"    {camera:<12} {obs.activity} - no description "
                  f"(quota or error)")
            _save(event_logger, record)
            continue

        stats.gemini_calls += 1
        stats.by_severity[analysis.severity] = (
            stats.by_severity.get(analysis.severity, 0) + 1
        )
        mark = SEVERITY_MARK.get(analysis.severity, "  ")
        print(f" {mark} {camera:<12} [{analysis.severity}] {analysis.description}")

        if analysis.severity in ("High", "Medium"):
            stats.highlights.append(
                f"[{stamp}] {analysis.severity} - {clinic.name}/{camera}: "
                f"{analysis.description}"
            )

        record.update(
            clinic_status=analysis.clinic_status,
            severity=analysis.severity,
            unusual=analysis.unusual_activity or analysis.immediate_attention,
            description=analysis.description,
        )
        _save(event_logger, record)

        if event_logger is not None:
            event_logger.log_event(
                clinic_name=clinic.name,
                camera_name=camera,
                description=analysis.description,
                severity=analysis.severity,
                frame=draw_detections(obs.best_frame, obs.best_detections),
                confidence=max(
                    (d.confidence for d in obs.best_detections), default=None
                ),
                clinic_status=analysis.clinic_status,
                reason=analysis.reason,
                staff_present=analysis.staff_present,
                patient_present=analysis.patient_present,
                unusual_activity=analysis.unusual_activity,
                immediate_attention=analysis.immediate_attention,
                person_count=obs.max_persons,
                detections=obs.best_detections,
                source="patrol",
                when=datetime.fromtimestamp(obs.best_at),
            )
            stats.events += 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="rotate through every clinic continuously"
    )
    parser.add_argument(
        "--duration", type=float, default=60.0,
        help="seconds to watch each clinic (default 60)",
    )
    parser.add_argument(
        "--rounds", type=int, default=0,
        help="how many full passes; 0 (default) means keep going until Ctrl+C",
    )
    parser.add_argument(
        "--clinics", default="",
        help="comma-separated substrings to restrict the rotation, "
        "e.g. BANAMALIPUR,NAYAHAT",
    )
    parser.add_argument(
        "--all-cameras", action="store_true",
        help="describe every camera, including idle ones (costs more API calls)",
    )
    parser.add_argument(
        "--pause", type=float, default=0.0,
        help="seconds to wait between clinics (default 0)",
    )
    parser.add_argument(
        "--rest", type=float, default=0.0,
        help="seconds to wait between full rounds (default 0)",
    )
    parser.add_argument(
        "--interval", type=float, default=config.CAPTURE_INTERVAL_SEC,
        help="capture interval in seconds",
    )
    parser.add_argument("--question", default="", help="extra question per camera")
    parser.add_argument("--serial", help="adb serial, if several devices attached")
    parser.add_argument(
        "--emulator", nargs="?", const="", metavar="AVD",
        help="start (or reuse) the Android emulator and patrol through it, "
        "instead of a phone on a cable. Optionally name the AVD.",
    )
    parser.add_argument(
        "--no-reports", action="store_true",
        help="skip rebuilding the daily report after each clinic",
    )
    parser.add_argument(
        "--no-log", action="store_true", help="do not write events to the database"
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(name)-18s %(message)s"
    )
    for noisy in ("httpx", "google_genai", "PIL", "control.navigator"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config.ensure_directories()

    stop = {"now": False}

    def _handle(_signum, _frame):
        # Finish the clinic in progress rather than abandoning it mid-capture.
        print("\n\nstopping after this clinic...")
        stop["now"] = True

    signal.signal(signal.SIGINT, _handle)

    serial = args.serial
    if args.emulator is not None:
        try:
            serial = emulator.ensure_running(args.emulator or None)
        except EmulatorError as exc:
            print(f"could not start the emulator: {exc}")
            return 1
        package = emulator.detect_hik_package(serial)
        if package is None:
            print(f"Hik-Connect is not installed on {serial}. Install it on the "
                  "emulator and sign in, then run this again.")
            return 1
        print(f"emulator ready: {serial} (app: {package})")

    navigator = PhoneNavigator(serial=serial)
    print("opening Hik-Connect and reading the device list...")
    try:
        navigator.launch()
        names = navigator.list_devices()
    except NavigationError as exc:
        print(f"could not read the device list: {exc}")
        return 1

    if args.clinics:
        wanted = [w.strip().lower() for w in args.clinics.split(",") if w.strip()]
        names = [n for n in names if any(w in n.lower() for w in wanted)]
        if not names:
            print(f"no clinics matched {args.clinics!r}")
            return 1

    # Navigation costs roughly half a minute per clinic on top of the watch
    # window, so say up front how long a lap will really take.
    per_clinic = args.duration + 30 + args.pause
    print(f"\n{len(names)} clinics in rotation, {args.duration:.0f}s each")
    print(f"estimated {per_clinic * len(names) / 60:.0f} min per full round "
          f"(watching + navigation)")
    print("Ctrl+C to stop\n" + "=" * 72)

    detector = YoloDetector()
    detector.load()
    analyzer = GeminiAnalyzer(cooldown_sec=0.0)
    event_logger = None if args.no_log else EventLogger(db=Database())
    stats = PatrolStats()

    while not stop["now"]:
        stats.rounds += 1
        print(f"\n{'=' * 72}\nROUND {stats.rounds}  -  {datetime.now():%Y-%m-%d %H:%M:%S}"
              f"\n{'=' * 72}")

        for name in names:
            if stop["now"]:
                break
            try:
                visit(
                    navigator, name, args.duration, args.interval, detector,
                    analyzer, event_logger, stats, args.all_cameras, args.question,
                )
            except NavigationError as exc:
                # Offline clinics come back later, so never drop them from the
                # rotation - just note it and move on. visit() has already
                # printed the clinic header, so only the reason is needed.
                reason = str(exc).split(" - ")[0]
                stats.skipped[name] = reason
                print(f"    skipped: {reason}")
            except CaptureError as exc:
                stats.skipped[name] = f"capture failed: {exc}"
                print(f"    skipped: capture failed: {exc}")
            except Exception as exc:                      # never kill the patrol
                stats.skipped[name] = f"error: {exc}"
                log.exception("unexpected error at %s", name)

            # Rebuild this clinic's report as soon as its visit ends, so the
            # dashboard is current within a lap rather than only after a
            # manual run. It is a cheap read of rows already written.
            if not args.no_reports:
                try:
                    reporting.generate(name, datetime.now().strftime("%Y-%m-%d"),
                                       event_logger.db if event_logger else None)
                except Exception as exc:
                    log.debug("report refresh failed for %s: %s", name, exc)

            if args.pause and not stop["now"]:
                time.sleep(args.pause)

        print(f"\nround {stats.rounds} done - {stats.summary()}")
        if args.rounds and stats.rounds >= args.rounds:
            break
        if args.rest and not stop["now"]:
            print(f"resting {args.rest:.0f}s before the next round...")
            time.sleep(args.rest)

    print(f"\n{'=' * 72}\nPATROL FINISHED\n{'=' * 72}")
    print(stats.summary())
    if stats.skipped:
        print("\nskipped clinics:")
        for name, reason in stats.skipped.items():
            print(f"  {name:<26} {reason}")
    if stats.highlights:
        print(f"\nthings worth a look ({len(stats.highlights)}):")
        for line in stats.highlights[-20:]:
            print(f"  {line}")
    else:
        print("\nnothing above Low severity was seen.")
    print(f"\nfull history: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
