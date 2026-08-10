"""
AI Clinic Monitoring System - pipeline entry point.

    Hik-Connect app -> ADB screen capture -> motion gate -> YOLOv8n -> Gemini
    -> SQLite -> dashboard

Usage
-----
    python main.py                     # monitor every enabled clinic
    python main.py --clinic "Clinic A" # monitor one clinic
    python main.py --once              # single pass, useful for smoke tests
    python main.py --no-gemini         # motion + YOLO only (no API calls)
    python main.py --with-dashboard    # also serve the dashboard
    python main.py --list-devices      # show adb devices and exit
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

# Allow both `python main.py` and `python clinic_monitor/main.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from ai.gemini_analyzer import GeminiAnalyzer  # noqa: E402
from capture.adb_capture import CaptureError, list_devices  # noqa: E402
from capture.frame_reader import (  # noqa: E402
    CameraFrame,
    FrameReader,
    has_pinned_regions,
)
from control.navigator import (  # noqa: E402
    NavigationError,
    PhoneNavigator,
    build_clinic,
)
from detection.yolo_detector import Detection, YoloDetector, draw_detections  # noqa: E402
from motion.motion_detector import MotionRegistry  # noqa: E402
from storage.database import Database  # noqa: E402
from storage.logger import EventLogger  # noqa: E402

log = logging.getLogger("clinic_monitor")

STATS_INTERVAL_SEC = 60.0
# How often to verify Hik-Connect is still in the foreground (adb round trip).
FOREGROUND_CHECK_SEC = 20.0


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@dataclass
class PipelineStats:
    """Per-clinic counters - the cheapest way to see the funnel working."""

    frames_captured: int = 0
    tiles_seen: int = 0
    motion_frames: int = 0
    yolo_runs: int = 0
    person_frames: int = 0
    gemini_events: int = 0
    fallback_events: int = 0
    capture_failures: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def summary(self) -> str:
        uptime = time.monotonic() - self.started_at
        return (
            f"uptime={uptime / 60:.1f}m frames={self.frames_captured} "
            f"tiles={self.tiles_seen} motion={self.motion_frames} "
            f"yolo={self.yolo_runs} person={self.person_frames} "
            f"events(gemini/fallback)={self.gemini_events}/{self.fallback_events} "
            f"capture_fail={self.capture_failures}"
        )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
class ClinicPipeline:
    """Runs the full 4-stage funnel for one clinic (one Android device)."""

    def __init__(
        self,
        clinic: "config.ClinicConfig",
        detector: YoloDetector,
        analyzer: GeminiAnalyzer,
        event_logger: EventLogger,
        stop_event: threading.Event,
        annotate: bool = True,
        keep_open: Optional[str] = None,
    ) -> None:
        # When set, the pipeline verifies Hik-Connect is still showing this
        # device's live view and re-opens it if the phone wandered off.
        self.keep_open = keep_open
        self._navigator: Optional[PhoneNavigator] = None
        self._last_foreground_check = 0.0
        self.clinic = clinic
        self.detector = detector
        self.analyzer = analyzer
        self.event_logger = event_logger
        self.stop_event = stop_event
        self.annotate = annotate
        self.stats = PipelineStats()
        self.motion = MotionRegistry()
        self.reader: Optional[FrameReader] = None
        # Per-camera timestamp of the last event written, used to avoid
        # flooding the log with near-identical alerts.
        self._last_event: Dict[str, float] = {}

    # -- helpers ---------------------------------------------------------- #
    def _event_due(self, camera: str) -> bool:
        last = self._last_event.get(camera, 0.0)
        return (time.monotonic() - last) >= config.GEMINI_COOLDOWN_SEC

    def _mark_event(self, camera: str) -> None:
        self._last_event[camera] = time.monotonic()

    def check_foreground(self) -> bool:
        """
        Make sure we are still looking at a Hik-Connect live view.

        Without this the pipeline happily analyses whatever is on screen -
        a home screen, a messaging app - and logs it as clinic activity.
        Checked on an interval because each check costs an adb round trip.
        """
        now = time.monotonic()
        if now - self._last_foreground_check < FOREGROUND_CHECK_SEC:
            return True
        self._last_foreground_check = now

        if self._navigator is None:
            self._navigator = PhoneNavigator(serial=self.clinic.adb_serial)
        try:
            if self._navigator.is_live_view():
                return True
            focus = self._navigator.current_focus() or "unknown"
        except NavigationError as exc:
            log.debug("[%s] foreground check failed: %s", self.clinic.name, exc)
            return True  # never let a diagnostic stop the monitoring loop

        if not self.keep_open:
            log.warning(
                "[%s] Hik-Connect is not showing a live view (focus=%s) - "
                "frames are being analysed anyway; pass --keep-open to recover",
                self.clinic.name,
                focus,
            )
            return False

        log.warning(
            "[%s] live view lost (focus=%s) - reopening %r",
            self.clinic.name,
            focus,
            self.keep_open,
        )
        try:
            self._navigator.launch()
            regions = self._navigator.open_live_view(self.keep_open)
        except NavigationError as exc:
            log.error("[%s] could not reopen the live view: %s", self.clinic.name, exc)
            return False

        # The app may have reopened in a different layout (it remembers the
        # last view). Re-measure rather than keep cropping the old positions.
        if regions and len(regions) != len(self.clinic.active_cameras()):
            log.warning(
                "[%s] layout changed on reopen: %d tiles now, was %d - remeasuring",
                self.clinic.name,
                len(regions),
                len(self.clinic.active_cameras()),
            )
            self.apply_clinic(
                build_clinic(self.keep_open, regions, self.clinic.adb_serial)
            )
        # The scene just changed completely; a stale background model would
        # read the new feed as one huge motion event.
        self.motion.reset_all()
        return True

    def apply_clinic(self, clinic: "config.ClinicConfig") -> None:
        """Swap in a freshly measured layout without restarting the pipeline."""
        self.clinic = clinic
        if self.reader is not None:
            self.reader.clinic = clinic
            self.reader._pinned = has_pinned_regions(clinic)
            self.reader.current_grid = None
        self.motion.reset_all()

    # -- stages ----------------------------------------------------------- #
    def process_tile(self, tile: CameraFrame) -> None:
        """Run one camera tile through stages 2-5."""
        self.stats.tiles_seen += 1

        # Stage 2 - motion gate.
        motion = self.motion.update(self.clinic.name, tile.camera_name, tile.image)
        if not motion.motion:
            return
        self.stats.motion_frames += 1

        # Stage 3 - YOLOv8n.
        detections: List[Detection] = self.detector.detect(tile.image)
        self.stats.yolo_runs += 1
        persons = YoloDetector.persons(detections)
        if not persons:
            return
        self.stats.person_frames += 1

        top_conf = max(p.confidence for p in persons)
        when = datetime.fromtimestamp(tile.captured_at)
        evidence = (
            draw_detections(tile.image, detections) if self.annotate else tile.image
        )

        # Stage 4 - Gemini (throttled internally; returns None when skipped).
        analysis = self.analyzer.analyze(
            tile.image,
            clinic_name=tile.clinic_name,
            camera_name=tile.camera_name,
            detections=detections,
            timestamp=when.strftime("%Y-%m-%d %H:%M:%S"),
        )

        # Stage 5 - persist.
        if analysis is not None:
            self.event_logger.log_event(
                clinic_name=tile.clinic_name,
                camera_name=tile.camera_name,
                description=analysis.description,
                severity=analysis.severity,
                frame=evidence,
                confidence=top_conf,
                clinic_status=analysis.clinic_status,
                reason=analysis.reason,
                staff_present=analysis.staff_present,
                patient_present=analysis.patient_present,
                unusual_activity=analysis.unusual_activity,
                immediate_attention=analysis.immediate_attention,
                person_count=len(persons),
                motion_score=motion.score,
                detections=detections,
                source="gemini",
                when=when,
            )
            self.stats.gemini_events += 1
            self._mark_event(tile.camera_name)
            return

        # Gemini skipped, disabled or failing - keep a YOLO-only record so the
        # system still produces a usable audit trail.
        if not self._event_due(tile.camera_name):
            return
        noun = "person" if len(persons) == 1 else "people"
        self.event_logger.log_event(
            clinic_name=tile.clinic_name,
            camera_name=tile.camera_name,
            description=f"Motion with {len(persons)} {noun} detected (AI analysis unavailable).",
            severity="Low",
            frame=evidence,
            confidence=top_conf,
            clinic_status="Unclear",
            reason="Logged from YOLO detection; Gemini was skipped or unavailable.",
            person_count=len(persons),
            motion_score=motion.score,
            detections=detections,
            source="yolo",
            when=when,
        )
        self.stats.fallback_events += 1
        self._mark_event(tile.camera_name)

    # -- loop ------------------------------------------------------------- #
    def run_once(self) -> int:
        """One capture + processing pass. Returns the number of tiles handled."""
        assert self.reader is not None
        self.check_foreground()
        tiles = self.reader.read()
        if not tiles:
            self.stats.capture_failures += 1
            self.motion.reset_all()  # background model is stale after an outage
            return 0
        self.stats.frames_captured += 1
        for tile in tiles:
            if self.stop_event.is_set():
                break
            try:
                self.process_tile(tile)
            except Exception as exc:  # one bad tile must not kill the clinic
                log.exception("[%s] error processing %s: %s", self.clinic.name, tile.camera_name, exc)
        return len(tiles)

    def run(self) -> None:
        try:
            self.reader = FrameReader(self.clinic)
        except CaptureError as exc:
            log.error("[%s] cannot start capture: %s", self.clinic.name, exc)
            return

        cameras = ", ".join(c.name for c in self.clinic.active_cameras())
        log.info(
            "[%s] monitoring %s (device=%s, backend=%s, every %.1fs)",
            self.clinic.name,
            cameras or "no cameras configured",
            self.clinic.adb_serial or "auto",
            self.clinic.backend,
            config.CAPTURE_INTERVAL_SEC,
        )

        last_stats = time.monotonic()
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                self.run_once()

                if self.reader.consecutive_failures >= config.CAPTURE_MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "[%s] stopping: %d consecutive capture failures",
                        self.clinic.name,
                        self.reader.consecutive_failures,
                    )
                    break

                if time.monotonic() - last_stats >= STATS_INTERVAL_SEC:
                    log.info("[%s] %s", self.clinic.name, self.stats.summary())
                    last_stats = time.monotonic()

                delay = config.CAPTURE_INTERVAL_SEC - (time.monotonic() - started)
                if self.reader.consecutive_failures:
                    delay = max(delay, config.CAPTURE_RETRY_DELAY_SEC)
                self.stop_event.wait(max(0.0, delay))
        finally:
            self.reader.close()
            log.info("[%s] stopped - %s", self.clinic.name, self.stats.summary())


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = False) -> None:
    config.ensure_directories()
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        config.LOG_DIR / "clinic_monitor.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    for noisy in ("werkzeug", "urllib3", "google_genai", "httpx", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # google-genai logs "AFC is enabled..." straight to the root logger on
    # every request; drop it rather than turning the root logger down.
    class _DropSdkChatter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return not (
                record.name == "root" and str(record.msg).startswith("AFC is enabled")
            )

    for handler in root.handlers:
        handler.addFilter(_DropSdkChatter())


def select_clinics(name: Optional[str]) -> List["config.ClinicConfig"]:
    clinics = config.load_clinics()
    if not name:
        return clinics
    matches = [c for c in clinics if c.name.lower() == name.lower()]
    if not matches:
        available = ", ".join(c.name for c in clinics) or "none"
        raise SystemExit(f"unknown clinic {name!r}. Configured clinics: {available}")
    return matches


def clinic_from_device(
    device_name: str, serial: Optional[str] = None
) -> "config.ClinicConfig":
    """
    Navigate to a Hik-Connect device and describe it from the app itself.

    The clinic name and the crop regions then both come from what is actually
    on screen. Taking the name from clinics.json while navigating somewhere
    else would label every event with the wrong clinic - the events would look
    perfectly normal and be quietly false.
    """
    navigator = PhoneNavigator(serial=serial)
    navigator.launch()
    regions = navigator.open_live_view(device_name)
    clinic = build_clinic(device_name, regions, adb_serial=serial)
    log.info(
        "measured %r from the app: %dx%d grid, %d cameras",
        clinic.name,
        clinic.grid[0],
        clinic.grid[1],
        len(clinic.cameras),
    )
    return clinic


def print_devices() -> int:
    try:
        devices = list_devices()
    except CaptureError as exc:
        print(f"error: {exc}")
        return 1
    if not devices:
        print("no adb devices found - is USB debugging on and the cable connected?")
        return 1
    print(f"{'SERIAL':<28} STATE")
    for dev in devices:
        print(f"{dev.serial:<28} {dev.state}")
    return 0


def start_dashboard_thread() -> None:
    from dashboard.app import create_app

    app = create_app()

    def _serve() -> None:
        app.run(
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    thread = threading.Thread(target=_serve, name="dashboard", daemon=True)
    thread.start()
    log.info(
        "dashboard running at http://%s:%d",
        config.DASHBOARD_HOST,
        config.DASHBOARD_PORT,
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Clinic Monitoring System")
    parser.add_argument("--clinic", help="monitor only this clinic (by name)")
    parser.add_argument("--once", action="store_true", help="single pass then exit")
    parser.add_argument("--no-gemini", action="store_true", help="skip stage 4 entirely")
    parser.add_argument(
        "--no-annotate",
        action="store_true",
        help="store clean screenshots without detection boxes",
    )
    parser.add_argument(
        "--with-dashboard", action="store_true", help="serve the dashboard in-process"
    )
    parser.add_argument(
        "--keep-open",
        metavar="NAME",
        help="Hik-Connect device to open and keep on screen. The clinic name "
        "and camera regions are measured from the app, so clinics.json is not "
        "used and cannot disagree with what is on screen.",
    )
    parser.add_argument("--serial", help="adb serial, when several devices are attached")
    parser.add_argument("--list-devices", action="store_true", help="list adb devices and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    if args.list_devices:
        return print_devices()

    if args.keep_open and args.clinic and args.keep_open.lower() != args.clinic.lower():
        log.error(
            "--clinic %r and --keep-open %r disagree. They must name the same "
            "clinic, or events would be filed under the wrong one.",
            args.clinic,
            args.keep_open,
        )
        return 1

    if args.keep_open:
        # The device on screen defines both the label and the crop regions, so
        # the two can never drift apart.
        try:
            clinics = [clinic_from_device(args.keep_open, args.serial)]
        except NavigationError as exc:
            log.error("could not open %r: %s", args.keep_open, exc)
            return 1
    else:
        clinics = select_clinics(args.clinic)

    if not clinics:
        log.error("no enabled clinics configured - edit config.py or clinics.json")
        return 1

    database = Database()
    event_logger = EventLogger(db=database)
    event_logger.purge_old_data()

    detector = YoloDetector()
    try:
        detector.load()  # fail fast rather than on the first motion event
    except Exception as exc:
        log.error("could not load the YOLO model: %s", exc)
        return 1

    # None -> fall back to CM_GEMINI_ENABLED from the environment.
    analyzer = GeminiAnalyzer(enabled=False if args.no_gemini else None)
    if analyzer.enabled:
        log.info(
            "Gemini enabled (model=%s, cooldown=%.0fs, max %d calls/hour)",
            analyzer.model,
            analyzer.cooldown_sec,
            config.GEMINI_MAX_CALLS_PER_HOUR,
        )
    else:
        log.warning("Gemini disabled - events will be logged from YOLO only")

    if args.with_dashboard:
        start_dashboard_thread()

    stop_event = threading.Event()

    def _handle_signal(signum, _frame) -> None:
        log.info("signal %s received - shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):  # not available on some platforms
        pass

    pipelines = [
        ClinicPipeline(
            clinic=clinic,
            detector=detector,
            analyzer=analyzer,
            event_logger=event_logger,
            stop_event=stop_event,
            annotate=not args.no_annotate,
            keep_open=args.keep_open,
        )
        for clinic in clinics
    ]

    if args.once:
        for pipeline in pipelines:
            try:
                pipeline.reader = FrameReader(pipeline.clinic)
            except CaptureError as exc:
                log.error("[%s] %s", pipeline.clinic.name, exc)
                continue
            # The motion model needs a couple of frames before it reports
            # anything, so a single pass primes it and then processes.
            for _ in range(config.MOTION_WARMUP_FRAMES + 1):
                pipeline.run_once()
                time.sleep(0.2)
            pipeline.reader.close()
            log.info("[%s] %s", pipeline.clinic.name, pipeline.stats.summary())
    else:
        threads = [
            threading.Thread(target=p.run, name=f"clinic-{p.clinic.name}", daemon=True)
            for p in pipelines
        ]
        for thread in threads:
            thread.start()
        try:
            while any(t.is_alive() for t in threads):
                for thread in threads:
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            stop_event.set()
        finally:
            stop_event.set()
            for thread in threads:
                thread.join(timeout=10)

    log.info("YOLO: %d inferences, avg %.1f ms", detector.total_inferences, detector.avg_inference_ms)
    log.info("Gemini: %s", analyzer.stats())
    log.info("events written: %d", event_logger.events_written)
    database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
