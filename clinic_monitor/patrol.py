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
import notify  # noqa: E402
from analysis import scoring  # noqa: E402
from analysis.camera_health import CameraHealth, HealthStatus, assess_sequence  # noqa: E402
from ask import CameraObservation, watch  # noqa: E402
from ai.gemini_analyzer import GeminiAnalyzer, resolve_phone_claim  # noqa: E402
from capture.adb_capture import CaptureError  # noqa: E402
import report as reporting  # noqa: E402
from control import emulator  # noqa: E402
from control.emulator import EmulatorError  # noqa: E402
from control.navigator import (  # noqa: E402
    DeviceNotFoundError,
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
        "staff_present": False,
        "person_present": False,
        "source": "patrol",
    }
    return row


def _plain_description(
    obs: CameraObservation, health: Optional[CameraHealth]
) -> str:
    """
    Describe a camera without spending an API call.

    Idle and faulty cameras are not sent to Gemini - there is nothing to
    describe and the answer would cost money to say so. They still belong in
    the dashboard though: a clinic where nothing moved should show four quiet
    cameras, not disappear from the feed entirely.
    """
    if health is not None and health.status.is_problem:
        return f"Camera fault: {health.status.label}. {health.status.action}."
    if obs.motion_frames:
        return "Movement seen, but no people identified."
    return "No activity: no movement and no people seen during this check."


def _log_camera(
    event_logger: Optional[EventLogger],
    clinic_name: str,
    camera: str,
    obs: CameraObservation,
    health: Optional[CameraHealth],
    when: datetime,
    analysis=None,
) -> None:
    """Write one dashboard event for a camera, described or not."""
    if event_logger is None:
        return
    frame = obs.best_frame
    if frame is not None and obs.best_detections:
        frame = draw_detections(frame, obs.best_detections)

    if analysis is not None:
        # YOLO's raw count is what put this frame in front of Gemini, but a
        # poster or screen showing a person scores just as high on YOLO as a
        # real one - Gemini looks at the same frame with actual scene
        # understanding, so when it says nobody is really there, that
        # overrides the count instead of leaving a "People: N" next to a
        # description that just said otherwise.
        person_count = (
            obs.max_persons
            if (analysis.staff_present or analysis.person_present)
            else 0
        )
        event_logger.log_event(
            clinic_name=clinic_name, camera_name=camera,
            description=analysis.description, severity=analysis.severity,
            frame=frame,
            confidence=max((d.confidence for d in obs.best_detections), default=None),
            clinic_status=analysis.clinic_status, reason=analysis.reason,
            staff_present=analysis.staff_present,
            person_present=analysis.person_present,
            unusual_activity=analysis.unusual_activity,
            immediate_attention=analysis.immediate_attention,
            person_count=person_count, detections=obs.best_detections,
            source="patrol", when=when, category=analysis.category,
        )
        return

    event_logger.log_event(
        clinic_name=clinic_name, camera_name=camera,
        description=_plain_description(obs, health),
        severity="Low",
        frame=frame,
        # Deliberately blank, not "Unclear". These cameras never went to
        # Gemini, so open/closed was never judged - and "Unclear" would claim
        # we looked and could not decide. The dashboard omits the pill when
        # this is empty, which is the truthful display.
        clinic_status=None,
        reason=(
            "Reported from motion and object detection; no AI description was "
            "needed for this camera."
        ),
        person_count=obs.max_persons,
        motion_score=round(obs.motion_frames / max(obs.frames, 1), 3),
        detections=obs.best_detections,
        source="patrol",
        when=when,
    )


def _save(event_logger: Optional[EventLogger], row: dict) -> None:
    if event_logger is None:
        return
    try:
        event_logger.db.insert_observation(row)
    except Exception as exc:                      # never let logging stop a patrol
        log.error("could not record observation: %s", exc)


def _previous_health_status(
    event_logger: Optional[EventLogger], clinic_name: str, camera: str
) -> Optional[str]:
    """
    This camera's health_status on its last visit, or None if there isn't one.

    A single bad-looking visit is often nothing - a hand wiping the lens, a
    moth on the dome, a stray reflection - and reporting every one of those
    as a "camera fault" is noise nobody can act on. Requiring the same visit
    to look unhealthy twice in a row (any problem status, not necessarily
    the identical one) before treating it as a real, actionable fault is
    cheap: `observations` already has one row per visit per camera, so this
    is a lookup against data already being written, not new state to keep.
    """
    if event_logger is None:
        return None
    row = event_logger.db.conn.execute(
        "SELECT health_status FROM observations WHERE clinic_name = ? "
        "AND camera_name = ? ORDER BY ts_epoch DESC LIMIT 1",
        (clinic_name, camera),
    ).fetchone()
    return row["health_status"] if row else None


def _all_frozen(observations: Dict[str, CameraObservation]) -> bool:
    """True when every camera watched in this visit read as a stalled stream."""
    verdicts = [
        assess_sequence(obs.samples).status
        for obs in observations.values()
        if obs.samples
    ]
    return bool(verdicts) and all(v is HealthStatus.FROZEN for v in verdicts)


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
) -> bool:
    """
    Navigate to one clinic, watch it, describe and log what was seen.

    Returns True when every camera still read as frozen after a second look -
    the caller counts those across clinics to tell a stalled screen on our side
    from cameras that are genuinely down.
    """
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{stamp}] {name}")

    regions = navigator.open_live_view(name)          # raises on offline/failure
    if event_logger is not None:
        event_logger.db.record_clinic_status(name, "online", datetime.now())
    clinic = build_clinic(name, regions, adb_serial=navigator.serial)
    observations = watch(clinic, duration, interval, detector=detector)
    stats.visits += 1

    if not observations:
        print("    no frames captured")
        return False

    # Every camera of a clinic freezing at the same instant is not a clinic
    # fault. Ten sites in ten villages froze and recovered in lockstep for
    # ninety minutes at a time here, which only happens when the screen we are
    # photographing stops being redrawn - the emulator stalling, not the NVRs.
    # So look once more before blaming anyone: re-open the live view and watch
    # again briefly. A real dead feed is still dead the second time.
    frozen_visit = False
    if _all_frozen(observations):
        print("    every camera frozen - looking again before calling it a fault")
        try:
            regions = navigator.open_live_view(name)
            clinic = build_clinic(name, regions, adb_serial=navigator.serial)
            second = watch(clinic, min(duration, 20.0), interval, detector=detector)
        except (NavigationError, CaptureError) as exc:
            log.warning("re-check of %s failed: %s", name, exc)
            second = {}
        if second and not _all_frozen(second):
            print("    second look is live - the first was our own stall")
            observations = second
        else:
            frozen_visit = True
            print("    still frozen on the second look")

    # Every camera of one visit is filed under a single timestamp so the
    # dashboard groups them together. Using each camera's own best-frame time
    # scattered a clinic's cameras across the feed seconds apart.
    visit_at = datetime.now()

    for camera, obs in sorted(observations.items()):
        active = obs.max_persons > 0 or obs.motion_frames > 0

        health = assess_sequence(obs.samples) if obs.samples else None
        record = _observation_row(clinic.name, camera, obs, health)

        # A camera showing no stream will never show activity, so describing it
        # is money spent to be told the screen is black. Report the fault -
        # but only once it has shown a problem on two visits running, so a
        # one-off glitch is recorded quietly instead of raised as a fault.
        if health is not None and health.status is not HealthStatus.OK:
            previous_status = _previous_health_status(event_logger, clinic.name, camera)
            persistent = previous_status not in (None, HealthStatus.OK.value)
            if persistent:
                print(f"    {camera:<12} CAMERA FAULT: {health.status.label}")
                stats.faults[f"{clinic.name}/{camera}"] = health.status
            else:
                print(f"    {camera:<12} {health.status.label} (first look - "
                      f"not yet reported as a fault)")
            if not health.usable:
                _save(event_logger, record)
                if persistent:
                    _log_camera(event_logger, clinic.name, camera, obs, health, visit_at)
                    stats.events += 1
                continue

        # Describing a camera that saw nothing costs an API call to be told
        # nothing happened. Idle cameras are reported from the cheap stages
        # instead, unless --all-cameras is given - but they still appear in the
        # dashboard, so a quiet clinic shows as quiet rather than missing.
        if not active and not all_cameras:
            print(f"    {camera:<12} idle (no motion, no people)")
            _save(event_logger, record)
            _log_camera(event_logger, clinic.name, camera, obs, health, visit_at)
            stats.events += 1
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
            _log_camera(event_logger, clinic.name, camera, obs, health, visit_at)
            stats.events += 1
            continue

        # An unconfirmed "staff on phone" call (posture alone, no device
        # actually seen) is downgraded here, once, so every downstream use
        # of severity/category - stats, the console line, the observation
        # row, the logged event - sees the same corrected verdict.
        analysis.category, analysis.severity = resolve_phone_claim(analysis)

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
            staff_present=analysis.staff_present,
            person_present=analysis.person_present,
        )
        _save(event_logger, record)

        _log_camera(
            event_logger, clinic.name, camera, obs, health, visit_at, analysis
        )
        stats.events += 1

    return frozen_visit


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

    if not names:
        # An empty list here is not "nothing to patrol" - every deployment
        # has clinics. Seen in practice: the emulator was still settling
        # when patrol started, list_devices() came back empty, and the round
        # loop below spun at full speed forever - visits=0 events=0 every
        # time, no sleep between rounds - filling a VM's disk with ~300
        # million log lines in about a day while patrolling nothing. Exit
        # instead and let systemd's Restart=always try again in 10s, once
        # (hopefully) the app has actually finished loading.
        print("device list came back empty - refusing to start a patrol loop "
              "with nothing to visit. Check Hik-Connect is signed in and the "
              "list actually loads, then retry.")
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

    stuck = 0                 # clinics that read all-frozen back to back
    while not stop["now"]:
        stats.rounds += 1
        print(f"\n{'=' * 72}\nROUND {stats.rounds}  -  {datetime.now():%Y-%m-%d %H:%M:%S}"
              f"\n{'=' * 72}")

        for name in names:
            if stop["now"]:
                break
            try:
                if visit(
                    navigator, name, args.duration, args.interval, detector,
                    analyzer, event_logger, stats, args.all_cameras, args.question,
                ):
                    stuck += 1
                else:
                    stuck = 0
                if event_logger is not None:
                    try:
                        today = scoring.clinic_scores(
                            event_logger.db, window="1D",
                            state=config.STATE_NAME or None,
                            cluster=config.CLUSTER_NAME or None,
                        )
                        overall = (today.get(name) or {}).get("overall")
                        if overall is not None:
                            notify.notify_low_score(
                                name, config.STATE_NAME, config.CLUSTER_NAME, overall
                            )
                    except Exception as exc:
                        log.debug("low-score check failed for %s: %s", name, exc)
            except DeviceNotFoundError as exc:
                # Our own navigation failed to locate the device while
                # scrolling - not a signal about the clinic's device at all,
                # so it must never be written as clinic_status="offline".
                # Doing so once made a clinic that had never gone down show
                # up as "offline" on the dashboard because a stray "Sharing"
                # badge threw off a scroll count.
                reason = str(exc).split(" - ")[0]
                stats.skipped[name] = f"navigation: {reason}"
                print(f"    skipped (navigation, not recorded as offline): {reason}")
            except NavigationError as exc:
                # Offline clinics come back later, so never drop them from the
                # rotation - just note it and move on. visit() has already
                # printed the clinic header, so only the reason is needed.
                reason = str(exc).split(" - ")[0]
                stats.skipped[name] = reason
                print(f"    skipped: {reason}")
                # Record it: without a row here a skipped clinic leaves no
                # trace at all, and afterwards nobody can say when a site went
                # down or how long it stayed down.
                if event_logger is not None:
                    try:
                        event_logger.db.record_clinic_status(
                            name, "offline", datetime.now(), reason
                        )
                    except Exception as db_exc:
                        log.error("could not record offline status: %s", db_exc)
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

            # Separate clinics freezing one after another is not a coincidence:
            # it is our own screen that has stopped being redrawn. Left alone
            # this used to persist for an hour and a half, filing every clinic
            # in the rotation as a camera fault. Restart the app, and if that
            # does not clear it, the emulator underneath.
            if stuck >= config.STALL_RESTART_APP:
                print(f"\n{stuck} clinics frozen in a row - this is our screen, "
                      f"not theirs. Restarting the app.")
                try:
                    if stuck >= config.STALL_RESTART_EMULATOR and args.emulator is not None:
                        print("    the app restart did not help - restarting the "
                              "emulator")
                        # Scoped to this patrol's own AVD - with several
                        # emulators possibly running side by side (one per
                        # account), an unscoped stop() would risk killing a
                        # different instance's emulator instead of this one.
                        emulator.stop(avd=args.emulator or None)
                        navigator.use_serial(
                            emulator.ensure_running(args.emulator or None)
                        )
                        navigator.launch()
                    else:
                        navigator.restart_app()
                    stuck = 0
                except (EmulatorError, NavigationError) as exc:
                    log.error("could not recover from the stall: %s", exc)

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
