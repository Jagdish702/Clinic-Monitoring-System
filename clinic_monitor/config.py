"""
Central configuration for the AI Clinic Monitoring System.

Everything tunable lives here. Clinic / camera layout can additionally be
supplied from ``clinics.json`` (see ``clinics.example.json``) so the code does
not need to be edited per deployment.

Coordinates for video regions are expressed as fractions of the phone screen
(0.0 - 1.0) so they survive changes in device resolution:

    region = (x, y, w, h)

Use ``python tools/preview_layout.py`` to calibrate them visually.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent

# Optional .env support - never a hard requirement.
try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass


Region = Tuple[float, float, float, float]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
SCREENSHOT_DIR = Path(os.getenv("CM_SCREENSHOT_DIR", str(BASE_DIR / "screenshots")))
DB_PATH = Path(os.getenv("CM_DB_PATH", str(BASE_DIR / "storage" / "events.db")))
LOG_DIR = Path(os.getenv("CM_LOG_DIR", str(BASE_DIR / "logs")))

# --------------------------------------------------------------------------- #
# Stage 1 - screen capture
# --------------------------------------------------------------------------- #
ADB_PATH = os.getenv("CM_ADB_PATH", "adb")
CAPTURE_INTERVAL_SEC = float(os.getenv("CM_CAPTURE_INTERVAL", "1.5"))
CAPTURE_TIMEOUT_SEC = float(os.getenv("CM_CAPTURE_TIMEOUT", "20"))
# Screenshots from modern phones are huge; downscale before any processing.
CAPTURE_MAX_WIDTH = int(os.getenv("CM_CAPTURE_MAX_WIDTH", "1280"))
CAPTURE_RETRY_DELAY_SEC = float(os.getenv("CM_CAPTURE_RETRY_DELAY", "5"))
CAPTURE_MAX_CONSECUTIVE_FAILURES = 10

# Hik-Connect switches between full-screen and grid views while you use it, so
# the reader measures the black divider lines on each frame instead of assuming
# the configured grid. Candidates are tried largest-first.
LAYOUT_AUTODETECT = _env_bool("CM_LAYOUT_AUTODETECT", True)
LAYOUT_CANDIDATES = ((3, 3), (2, 2), (1, 2), (2, 1), (1, 1))
LAYOUT_SEAM_HALF_WIDTH = 2      # pixels sampled either side of a seam centre
LAYOUT_SEAM_MAX_LEVEL = 40      # a divider is near-black
LAYOUT_SEAM_MIN_CONTRAST = 18   # ...and clearly darker than its neighbours
LAYOUT_SEAM_MIN_COVERAGE = 0.85 # fraction of the seam that must satisfy both
# Name used when the app is on a single-camera view: which camera it is showing
# cannot be known from pixels alone.
LAYOUT_SINGLE_VIEW_NAME = "Full view"

# --------------------------------------------------------------------------- #
# Android emulator - an alternative to a physically connected phone
# --------------------------------------------------------------------------- #
# With an emulator the whole system needs no cable and no handset, which is
# also what makes it deployable on a server.
ANDROID_SDK = os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT", "")
# Empty means "use the only AVD installed", which is the common case.
EMULATOR_AVD = os.getenv("CM_EMULATOR_AVD", "")
EMULATOR_BOOT_TIMEOUT_SEC = float(os.getenv("CM_EMULATOR_BOOT_TIMEOUT", "240"))
# Breathing room after boot before the app is launched. Starting a video app
# into peak boot load is what makes it hang long enough to trigger an ANR.
EMULATOR_SETTLE_SEC = float(os.getenv("CM_EMULATOR_SETTLE", "15"))
# Headless: the emulator window is never looked at - every frame is read over
# ADB - so drawing and compositing it is pure overhead. This is the single
# biggest thing that reduces emulator lag, and it is what makes the setup
# viable on a server with no display at all.
EMULATOR_HEADLESS = _env_bool("CM_EMULATOR_HEADLESS", True)
# -no-snapshot-save keeps a crash from corrupting the saved state; the rest
# just makes boot quicker and networking unthrottled.
EMULATOR_ARGS = (
    "-no-snapshot-save",
    "-no-boot-anim",
    "-netdelay", "none",
    "-netspeed", "full",
)
# Memory the AVD gets. Android 12 decoding several video streams struggles on
# the 2048 MB default; these are applied to the AVD's config.ini by
# `python -m control.emulator --tune`.
EMULATOR_RAM_MB = int(os.getenv("CM_EMULATOR_RAM_MB", "4096"))
EMULATOR_HEAP_MB = int(os.getenv("CM_EMULATOR_HEAP_MB", "512"))

# --------------------------------------------------------------------------- #
# Phone control - opening the app and navigating to a clinic
# --------------------------------------------------------------------------- #
# This build of Hik-Connect is white-labelled: the package is NOT
# com.hikvision.hikconnect. Verify with:
#   adb shell cmd package resolve-activity --brief <package>
HIK_PACKAGE = os.getenv("CM_HIK_PACKAGE", "com.connect.enduser")
# Builds of the same app seen in the wild. The navigator checks which one is
# actually installed rather than assuming, because the white-labelled build on
# the clinic phone and the Play Store build on an emulator differ.
HIK_PACKAGE_CANDIDATES = (
    "com.connect.enduser",        # CureBay white-label
    "com.hikvision.hikconnect",   # Play Store Hik-Connect
    "com.mcu.iVMS",               # iVMS-4500
)
HIK_LAUNCH_ACTIVITY = os.getenv(
    "CM_HIK_ACTIVITY", "com.hikvision.hikconnect.login.LoadingActivity"
)
# Substrings matched against the focused window to tell which screen we are on.
HIK_LIST_ACTIVITY_HINT = "MainTabActivity"
HIK_VIDEO_ACTIVITY_HINT = "VideoActivity"
# Resource ids of the controls we tap / measure. These are the parts most
# likely to break if the app is redesigned, so they are named here.
NAV_PLAY_BUTTON_ID = "multi_channel_fold_iv"   # the play icon on a device row
NAV_TILE_ID = "play_window_layout"             # one live camera tile
NAV_THUMBNAIL_ID = "channel_item_layout"       # fallback: a camera thumbnail
NAV_MORE_BUTTON_ID = "device_more_iv"          # the ... on every device row
NAV_OFFLINE_TEXT = "device offline"

NAV_MAX_SCROLLS = int(os.getenv("CM_NAV_MAX_SCROLLS", "25"))
NAV_SCROLL_SETTLE_SEC = 1.2
NAV_LAUNCH_TIMEOUT_SEC = 30.0
NAV_VIEW_TIMEOUT_SEC = 25.0
# Streams need a moment to connect after the live view opens.
NAV_STREAM_SETTLE_SEC = float(os.getenv("CM_NAV_STREAM_SETTLE", "8"))

# --------------------------------------------------------------------------- #
# Stage 2 - motion detection
# --------------------------------------------------------------------------- #
MOTION_WORK_WIDTH = 480          # frames are resized to this before diffing
MOTION_MIN_AREA_RATIO = float(os.getenv("CM_MOTION_MIN_AREA", "0.004"))  # 0.4%
MOTION_PIXEL_THRESHOLD = 25      # foreground mask binarisation threshold
MOTION_HISTORY = 200             # MOG2 history length (frames)
MOTION_VAR_THRESHOLD = 24        # MOG2 sensitivity (lower = more sensitive)
MOTION_WARMUP_FRAMES = 8         # frames ignored while the model settles
MOTION_BLUR_KERNEL = 5           # odd number; suppresses sensor noise
MOTION_DILATE_ITERATIONS = 2
MOTION_MIN_BOX_AREA_RATIO = 0.0008  # discard specks smaller than this

# --------------------------------------------------------------------------- #
# Stage 3 - YOLOv8n object detection (CPU only)
# --------------------------------------------------------------------------- #
YOLO_MODEL = os.getenv("CM_YOLO_MODEL", "yolov8n.pt")
YOLO_DEVICE = "cpu"
YOLO_CONF = float(os.getenv("CM_YOLO_CONF", "0.30"))
YOLO_PERSON_CONF = float(os.getenv("CM_YOLO_PERSON_CONF", "0.45"))
YOLO_IOU = 0.45
YOLO_IMGSZ = int(os.getenv("CM_YOLO_IMGSZ", "640"))
YOLO_MAX_DET = 50
TORCH_THREADS = int(os.getenv("CM_TORCH_THREADS", "2"))

# --------------------------------------------------------------------------- #
# Stage 4 - Gemini vision
# --------------------------------------------------------------------------- #
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Rolling aliases only: pinned ids such as "gemini-2.0-flash" return 429
# (limit: 0) on new API keys, and the 2.5 ids 404 for new users.
# Lite is the cheapest per call and the kindest to a rate-limited key, which
# matters here because stage 4 fires on every person detection.
GEMINI_MODEL = os.getenv("CM_GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_ENABLED = _env_bool("CM_GEMINI_ENABLED", True)
GEMINI_TIMEOUT_SEC = float(os.getenv("CM_GEMINI_TIMEOUT", "30"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("CM_GEMINI_MAX_OUTPUT_TOKENS", "1024"))
# Flash models think before answering. Scene description does not need it, and
# thinking tokens come out of the output budget - leave this on.
GEMINI_DISABLE_THINKING = _env_bool("CM_GEMINI_DISABLE_THINKING", True)
GEMINI_MAX_RETRIES = int(os.getenv("CM_GEMINI_RETRIES", "2"))
GEMINI_RETRY_BACKOFF_SEC = 2.0
# Cost control: at most one call per camera per cooldown window, and a global
# hourly ceiling shared by every clinic.
GEMINI_COOLDOWN_SEC = float(os.getenv("CM_GEMINI_COOLDOWN", "30"))
GEMINI_MAX_CALLS_PER_HOUR = int(os.getenv("CM_GEMINI_MAX_CALLS_PER_HOUR", "150"))
# Circuit breaker - stop hammering a broken API.
GEMINI_FAILURE_THRESHOLD = 3
GEMINI_FAILURE_BACKOFF_SEC = 60.0
# Images are downscaled before upload; 768px is plenty for scene understanding.
GEMINI_MAX_WIDTH = 768
GEMINI_JPEG_QUALITY = 80

# --------------------------------------------------------------------------- #
# Stage 5 - storage
# --------------------------------------------------------------------------- #
SCREENSHOT_JPEG_QUALITY = 85
SCREENSHOT_RETENTION_DAYS = int(os.getenv("CM_SCREENSHOT_RETENTION_DAYS", "14"))
# Save a screenshot for every logged event (disable to save disk space).
SAVE_SCREENSHOTS = _env_bool("CM_SAVE_SCREENSHOTS", True)

# --------------------------------------------------------------------------- #
# Daily reporting
# --------------------------------------------------------------------------- #
# The schedule a clinic is expected to keep. Observed opening and closing are
# compared against these; they are not used for anything else.
EXPECTED_OPEN = os.getenv("CM_EXPECTED_OPEN", "07:30")
EXPECTED_CLOSE = os.getenv("CM_EXPECTED_CLOSE", "19:30")
EXPECTED_LUNCH_START = os.getenv("CM_EXPECTED_LUNCH_START", "13:30")
EXPECTED_LUNCH_END = os.getenv("CM_EXPECTED_LUNCH_END", "14:30")
SCHEDULE_TOLERANCE_MINUTES = int(os.getenv("CM_SCHEDULE_TOLERANCE", "30"))
# People visible at once before the checkup area counts as unusually crowded.
CROWDING_PERSONS = int(os.getenv("CM_CROWDING_PERSONS", "6"))
# Quiet stretch during opening hours that is worth reporting.
INACTIVITY_MINUTES = int(os.getenv("CM_INACTIVITY_MINUTES", "90"))
# Midday quiet stretch long enough to read as a lunch break.
LUNCH_MIN_MINUTES = int(os.getenv("CM_LUNCH_MIN_MINUTES", "30"))

# --------------------------------------------------------------------------- #
# Stage 6 - dashboard
# --------------------------------------------------------------------------- #
DASHBOARD_HOST = os.getenv("CM_DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("CM_DASHBOARD_PORT", "8000"))
DASHBOARD_REFRESH_SEC = int(os.getenv("CM_DASHBOARD_REFRESH", "5"))
DASHBOARD_PAGE_SIZE = 100

# --------------------------------------------------------------------------- #
# Clinic / camera layout
# --------------------------------------------------------------------------- #


@dataclass
class CameraConfig:
    """One camera tile inside the Hik-Connect grid."""

    name: str
    # Explicit fractional region of the *screen*. When None the region is
    # derived from the parent clinic's video_region + grid.
    region: Optional[Region] = None
    enabled: bool = True


@dataclass
class ClinicConfig:
    """One Android device mirroring one Hik-Connect account/clinic."""

    name: str
    adb_serial: Optional[str] = None      # None -> the only connected device
    backend: str = "adb"                  # "adb" | "scrcpy"
    # Portion of the phone screen occupied by the live video grid. The default
    # skips the Hik-Connect top bar and bottom navigation bar.
    video_region: Region = (0.0, 0.12, 1.0, 0.56)
    grid: Tuple[int, int] = (2, 2)        # (rows, cols)
    cameras: List[CameraConfig] = field(default_factory=list)
    # Only used by the scrcpy backend: absolute screen pixels of the mirror
    # window, as {"left":..,"top":..,"width":..,"height":..}.
    screen_region: Optional[dict] = None
    enabled: bool = True

    def active_cameras(self) -> List[CameraConfig]:
        return [c for c in self.cameras if c.enabled]


# Default single-clinic, 2x2 grid setup. Override via clinics.json.
CLINICS: List[ClinicConfig] = [
    ClinicConfig(
        name="Clinic A",
        adb_serial=None,
        backend="adb",
        video_region=(0.0, 0.12, 1.0, 0.56),
        grid=(2, 2),
        cameras=[
            CameraConfig("Reception"),
            CameraConfig("Waiting Area"),
            CameraConfig("Consultation"),
            CameraConfig("Entrance"),
        ],
    )
]

CLINICS_FILE = BASE_DIR / "clinics.json"


def _clinic_from_dict(raw: dict) -> ClinicConfig:
    cameras = [
        CameraConfig(
            name=c["name"],
            region=tuple(c["region"]) if c.get("region") else None,
            enabled=bool(c.get("enabled", True)),
        )
        for c in raw.get("cameras", [])
    ]
    return ClinicConfig(
        name=raw["name"],
        adb_serial=raw.get("adb_serial"),
        backend=raw.get("backend", "adb"),
        video_region=tuple(raw.get("video_region", (0.0, 0.12, 1.0, 0.56))),
        grid=tuple(raw.get("grid", (2, 2))),
        cameras=cameras,
        screen_region=raw.get("screen_region"),
        enabled=bool(raw.get("enabled", True)),
    )


def load_clinics() -> List[ClinicConfig]:
    """Return the clinic list, preferring ``clinics.json`` when present."""
    if CLINICS_FILE.exists():
        with CLINICS_FILE.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        clinics = [_clinic_from_dict(item) for item in raw.get("clinics", [])]
        if clinics:
            return [c for c in clinics if c.enabled]
    return [c for c in CLINICS if c.enabled]


def ensure_directories() -> None:
    for path in (SCREENSHOT_DIR, DB_PATH.parent, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
