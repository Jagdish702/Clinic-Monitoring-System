"""
Turn one phone screenshot into per-camera frames.

Hik-Connect shows several cameras in a grid. We crop the live-video region out
of the screenshot (dropping the app chrome) and then split it into one tile per
camera so every downstream stage works on a single camera at a time.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

import cv2
import numpy as np

import config
from capture.adb_capture import BaseCapture, CaptureError, build_capture

log = logging.getLogger(__name__)


@dataclass
class CameraFrame:
    """A single camera tile cropped from one phone screenshot."""

    clinic_name: str
    camera_name: str
    image: np.ndarray
    captured_at: float                      # epoch seconds
    region_px: Tuple[int, int, int, int]    # (x, y, w, h) in screen pixels

    @property
    def size(self) -> Tuple[int, int]:
        h, w = self.image.shape[:2]
        return w, h


def _region_to_px(
    region: Tuple[float, float, float, float], width: int, height: int
) -> Tuple[int, int, int, int]:
    x, y, w, h = region
    px = int(round(x * width))
    py = int(round(y * height))
    pw = int(round(w * width))
    ph = int(round(h * height))
    px = max(0, min(px, width - 1))
    py = max(0, min(py, height - 1))
    pw = max(1, min(pw, width - px))
    ph = max(1, min(ph, height - py))
    return px, py, pw, ph


def _seam_score(gray: np.ndarray, position: int, axis: int) -> float:
    """
    How much a row/column at ``position`` looks like a Hik-Connect divider.

    Returns the fraction of the seam that is both near-black and clearly
    darker than the picture either side of it. Requiring the contrast as well
    as the darkness stops a night-time (nearly black) full-screen view from
    being mistaken for a grid.
    """
    half = config.LAYOUT_SEAM_HALF_WIDTH
    limit = gray.shape[1 - axis]  # length along the axis we slice
    if position - 3 * half < 0 or position + 3 * half >= limit:
        return 0.0

    def band(start: int, stop: int) -> np.ndarray:
        return gray[:, start:stop] if axis == 0 else gray[start:stop, :]

    seam = band(position - half, position + half + 1)
    before = band(position - 3 * half, position - half)
    after = band(position + half + 1, position + 3 * half + 1)
    if seam.size == 0 or before.size == 0 or after.size == 0:
        return 0.0

    reduce_axis = 1 if axis == 0 else 0
    seam_line = seam.min(axis=reduce_axis).astype(np.int16)
    neighbours = np.minimum(
        before.mean(axis=reduce_axis), after.mean(axis=reduce_axis)
    ).astype(np.int16)

    dark = seam_line <= config.LAYOUT_SEAM_MAX_LEVEL
    contrast = (neighbours - seam_line) >= config.LAYOUT_SEAM_MIN_CONTRAST
    return float(np.count_nonzero(dark & contrast) / seam_line.size)


def detect_grid(
    frame: np.ndarray, candidates: Optional[Tuple[Tuple[int, int], ...]] = None
) -> Tuple[int, int]:
    """
    Work out the camera grid actually on screen by looking for the black
    divider lines Hik-Connect draws between tiles. Falls back to (1, 1).
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]

    for rows, cols in candidates or config.LAYOUT_CANDIDATES:
        if rows == 1 and cols == 1:
            return 1, 1
        seams = [
            _seam_score(gray, round(width * i / cols), axis=0)
            for i in range(1, cols)
        ] + [
            _seam_score(gray, round(height * i / rows), axis=1)
            for i in range(1, rows)
        ]
        if seams and min(seams) >= config.LAYOUT_SEAM_MIN_COVERAGE:
            return rows, cols
    return 1, 1


def has_pinned_regions(clinic: "config.ClinicConfig") -> bool:
    """True when the operator pinned regions by hand - never second-guess it."""
    return any(cam.region is not None for cam in clinic.active_cameras())


def _fallback_names(slots: int) -> List[str]:
    """
    Names for a layout the config did not describe.

    Which physical camera occupies which tile cannot be read off the pixels,
    so the tiles get honest positional names rather than a wrong one.
    """
    if slots == 1:
        return [config.LAYOUT_SINGLE_VIEW_NAME]
    return [f"View {i + 1}" for i in range(slots)]


def camera_regions(
    clinic: "config.ClinicConfig",
    grid: Optional[Tuple[int, int]] = None,
) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    """
    Resolve every tile on screen to a (name, fractional region) pair.

    Cameras with an explicit ``region`` use it verbatim. Otherwise tiles are
    laid out left-to-right, top-to-bottom over the clinic's ``video_region``.
    When ``grid`` differs from the configured one - i.e. the app is showing a
    layout we have no names for - positional names are used instead.
    """
    rows, cols = grid or clinic.grid
    rows = max(1, rows)
    cols = max(1, cols)
    vx, vy, vw, vh = clinic.video_region
    cell_w = vw / cols
    cell_h = vh / rows

    def cell(slot: int) -> Tuple[float, float, float, float]:
        r, c = divmod(slot, cols)
        return (vx + c * cell_w, vy + r * cell_h, cell_w, cell_h)

    if tuple(grid or clinic.grid) != tuple(clinic.grid):
        return list(zip(_fallback_names(rows * cols), map(cell, range(rows * cols))))

    resolved: List[Tuple[str, Tuple[float, float, float, float]]] = []
    slot = 0
    for cam in clinic.active_cameras():
        if cam.region is not None:
            resolved.append((cam.name, tuple(cam.region)))
            continue
        if slot >= rows * cols:
            log.warning(
                "clinic %s: camera %s has no grid slot left (grid is %dx%d)",
                clinic.name,
                cam.name,
                rows,
                cols,
            )
            continue
        resolved.append((cam.name, cell(slot)))
        slot += 1
    return resolved


def split_frame(
    frame: np.ndarray,
    clinic: "config.ClinicConfig",
    captured_at: Optional[float] = None,
    margin: float = 0.02,
    grid: Optional[Tuple[int, int]] = None,
) -> List[CameraFrame]:
    """
    Crop ``frame`` into one :class:`CameraFrame` per camera.

    ``margin`` trims a small border off each tile so the grid separator lines
    and camera-name overlays do not register as motion.
    """
    captured_at = captured_at if captured_at is not None else time.time()
    height, width = frame.shape[:2]
    tiles: List[CameraFrame] = []

    for name, region in camera_regions(clinic, grid=grid):
        x, y, w, h = _region_to_px(region, width, height)
        if margin > 0:
            mx = int(w * margin)
            my = int(h * margin)
            if w - 2 * mx > 16 and h - 2 * my > 16:
                x, y, w, h = x + mx, y + my, w - 2 * mx, h - 2 * my
        crop = frame[y : y + h, x : x + w]
        if crop.size == 0:
            log.warning("empty crop for %s/%s - check the region config", clinic.name, name)
            continue
        tiles.append(
            CameraFrame(
                clinic_name=clinic.name,
                camera_name=name,
                image=np.ascontiguousarray(crop),
                captured_at=captured_at,
                region_px=(x, y, w, h),
            )
        )
    return tiles


class FrameReader:
    """
    Paced reader that yields per-camera frames from one device.

    It owns the capture backend, enforces the capture interval, and survives
    transient adb failures (device asleep, USB hiccup, app backgrounded).
    """

    def __init__(
        self,
        clinic: "config.ClinicConfig",
        capture: Optional[BaseCapture] = None,
        interval: Optional[float] = None,
    ) -> None:
        self.clinic = clinic
        self.capture = capture or build_capture(clinic)
        self.interval = interval or config.CAPTURE_INTERVAL_SEC
        self.consecutive_failures = 0
        self.frames_read = 0
        self._last_grab = 0.0
        # Layout actually on screen; None until the first frame is measured.
        self.current_grid: Optional[Tuple[int, int]] = None
        self._pinned = has_pinned_regions(clinic)

    def read_screen(self) -> Optional[np.ndarray]:
        """Grab one full screenshot, or None if the capture failed."""
        try:
            frame = self.capture.grab()
        except CaptureError as exc:
            self.consecutive_failures += 1
            log.warning(
                "[%s] capture failed (%d in a row): %s",
                self.clinic.name,
                self.consecutive_failures,
                exc,
            )
            return None
        self.consecutive_failures = 0
        self.frames_read += 1
        return frame

    def resolve_grid(self, frame: np.ndarray) -> Tuple[int, int]:
        """
        Measure the layout on screen and report changes.

        Hik-Connect flips between full-screen and grid views as it is used, so
        a fixed grid would silently start cropping quadrants out of a single
        camera. Hand-pinned regions are always honoured as-is.
        """
        if self._pinned or not config.LAYOUT_AUTODETECT:
            return tuple(self.clinic.grid)

        # Detect inside the video region only. On a portrait live view the grid
        # occupies a band under the title bar, and scanning the whole screen
        # would look for dividers in the app chrome and find none.
        height, width = frame.shape[:2]
        x, y, w, h = _region_to_px(self.clinic.video_region, width, height)
        grid = detect_grid(frame[y : y + h, x : x + w])
        if grid != self.current_grid:
            if self.current_grid is not None:
                log.info(
                    "[%s] layout changed %dx%d -> %dx%d",
                    self.clinic.name,
                    *self.current_grid,
                    *grid,
                )
            if tuple(grid) != tuple(self.clinic.grid):
                log.warning(
                    "[%s] app is showing a %dx%d layout, config describes %dx%d - "
                    "tiles will be logged with positional names",
                    self.clinic.name,
                    *grid,
                    *self.clinic.grid,
                )
            self.current_grid = grid
        return grid

    def read(self) -> List[CameraFrame]:
        """Grab one screenshot and split it into camera tiles."""
        frame = self.read_screen()
        if frame is None:
            return []
        return split_frame(frame, self.clinic, grid=self.resolve_grid(frame))

    def stream(self, stop_event: Optional[threading.Event] = None) -> Iterator[List[CameraFrame]]:
        """Yield camera tiles forever at the configured interval."""
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            started = time.monotonic()
            tiles = self.read()
            if tiles:
                yield tiles
            elif self.consecutive_failures >= config.CAPTURE_MAX_CONSECUTIVE_FAILURES:
                log.error(
                    "[%s] giving up after %d consecutive capture failures",
                    self.clinic.name,
                    self.consecutive_failures,
                )
                return
            elif self.consecutive_failures:
                stop_event.wait(config.CAPTURE_RETRY_DELAY_SEC)
                continue

            elapsed = time.monotonic() - started
            stop_event.wait(max(0.0, self.interval - elapsed))

    def close(self) -> None:
        self.capture.close()
