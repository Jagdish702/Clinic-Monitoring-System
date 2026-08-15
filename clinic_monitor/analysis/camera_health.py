"""
Camera health from the picture itself.

A monitoring system that cannot tell "nothing is happening" from "this camera
stopped working" is worse than useless: a dead camera reports calm forever.
These checks run on frames the pipeline already has, cost nothing, and need no
API call.

What is detectable, and what each looks like:

* **NO_SIGNAL**  - dark and almost featureless. Hik-Connect draws the Hikvision
  logo on a black background when a channel has no stream.
* **FROZEN**     - consecutive frames are pixel-identical. A live camera always
  shows sensor noise, so a perfectly still image means the feed stalled.
* **TOO_DARK**   - lit scene expected, almost no light and no usable detail.
* **NEEDS_CLEANING** - the picture is soft everywhere: a dirty, smeared or
  fogged lens destroys high-frequency detail while brightness stays normal.
* **OBSTRUCTED** - a large part of the view is flat and featureless, as when
  something is placed in front of the lens.

Deliberately *not* inferred: whether a camera is "broken" in hardware terms.
Only what the picture shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence

import cv2
import numpy as np

# --------------------------------------------------------------------------- #
# Thresholds, calibrated against real tiles from these clinics.
#
#   working cameras     brightness  89-134   detail 569-5400  edges .055-.17  flat .00-.30
#   offline logo screen brightness     5.2   detail    ~1200  edges     .018  flat      .85
#
# Note that sharpness does NOT separate them: the Hikvision logo on a black
# screen is crisp, so an offline channel scores *higher* detail than a real
# night-time scene. Brightness combined with flatness is what actually splits
# the two, which is why the no-signal test uses those and ignores detail.
# --------------------------------------------------------------------------- #
DARK_BRIGHTNESS = 25.0           # nothing meaningful is visible below this
NO_SIGNAL_FLAT_RATIO = 0.60      # ...and the frame carries almost no detail
FROZEN_DIFF = 0.35               # mean absolute difference between frames
# A dirty lens keeps normal brightness but loses high-frequency detail. The
# softest real camera here scores 569, so 250 leaves a wide margin against
# false alarms while still catching a genuinely smeared view.
BLUR_VARIANCE = 250.0
BLUR_EDGE_RATIO = 0.040
BLUR_MIN_BRIGHTNESS = 60.0       # never judge sharpness on a night-mode frame
OBSTRUCTION_FLAT_RATIO = 0.65    # lit, but most of the view carries no detail
OBSTRUCTION_EDGE_RATIO = 0.030
MIN_FRAMES_FOR_FROZEN = 4


class HealthStatus(str, Enum):
    OK = "ok"
    NO_SIGNAL = "no_signal"
    FROZEN = "frozen"
    TOO_DARK = "too_dark"
    NEEDS_CLEANING = "needs_cleaning"
    OBSTRUCTED = "obstructed"

    @property
    def is_problem(self) -> bool:
        return self is not HealthStatus.OK

    @property
    def label(self) -> str:
        return {
            HealthStatus.OK: "OK",
            HealthStatus.NO_SIGNAL: "no signal / channel offline",
            HealthStatus.FROZEN: "feed frozen",
            HealthStatus.TOO_DARK: "too dark to see anything",
            HealthStatus.NEEDS_CLEANING: "lens dirty or out of focus",
            HealthStatus.OBSTRUCTED: "view blocked",
        }[self]

    @property
    def action(self) -> str:
        return {
            HealthStatus.OK: "",
            HealthStatus.NO_SIGNAL: "check the camera power and NVR connection",
            HealthStatus.FROZEN: "restart the channel; the stream has stalled",
            HealthStatus.TOO_DARK: "check IR / night mode and area lighting",
            HealthStatus.NEEDS_CLEANING: "clean the lens and dome cover",
            HealthStatus.OBSTRUCTED: "something is in front of the camera",
        }[self]


@dataclass
class CameraHealth:
    """Measured picture quality for one camera."""

    status: HealthStatus
    brightness: float                  # mean grey level, 0-255
    detail: float                      # Laplacian variance: sharpness
    contrast: float                    # standard deviation of grey levels
    edge_ratio: float                  # fraction of pixels on an edge
    flat_ratio: float                  # fraction with no local variation
    motion_between_frames: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether anything seen on this camera can be trusted."""
        return self.status in (HealthStatus.OK, HealthStatus.NEEDS_CLEANING)

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "brightness": round(self.brightness, 1),
            "detail": round(self.detail, 1),
            "contrast": round(self.contrast, 1),
            "edge_ratio": round(self.edge_ratio, 4),
            "flat_ratio": round(self.flat_ratio, 3),
            "motion_between_frames": (
                round(self.motion_between_frames, 3)
                if self.motion_between_frames is not None
                else None
            ),
            "notes": self.notes,
        }


def _metrics(frame: np.ndarray) -> dict:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    edges = cv2.Canny(gray, 60, 160)

    # Local standard deviation: flat areas are where the picture carries no
    # information at all, which is what an obstruction or a logo screen looks
    # like even when the mean brightness is unremarkable.
    blurred = cv2.blur(gray.astype(np.float32), (17, 17))
    sq = cv2.blur((gray.astype(np.float32)) ** 2, (17, 17))
    local_std = np.sqrt(np.maximum(sq - blurred ** 2, 0))

    return {
        "brightness": float(gray.mean()),
        "detail": float(laplacian.var()),
        "contrast": float(gray.std()),
        "edge_ratio": float(np.count_nonzero(edges) / edges.size),
        "flat_ratio": float(np.count_nonzero(local_std < 3.0) / local_std.size),
    }


def assess_frame(frame: np.ndarray) -> CameraHealth:
    """Classify a single frame. Frozen feeds need `assess_sequence` instead."""
    m = _metrics(frame)
    notes: List[str] = []

    if m["brightness"] < DARK_BRIGHTNESS and m["flat_ratio"] > NO_SIGNAL_FLAT_RATIO:
        status = HealthStatus.NO_SIGNAL
        notes.append(
            "black and featureless - the channel is showing no stream "
            "(offline, unplugged, or not recording)"
        )
    elif m["brightness"] < DARK_BRIGHTNESS:
        status = HealthStatus.TOO_DARK
        notes.append("a scene is present but almost no light is reaching the sensor")
    elif (
        m["flat_ratio"] > OBSTRUCTION_FLAT_RATIO
        and m["edge_ratio"] < OBSTRUCTION_EDGE_RATIO
    ):
        status = HealthStatus.OBSTRUCTED
        notes.append(
            f"lit, but {m['flat_ratio'] * 100:.0f}% of the view carries no "
            "detail at all"
        )
    elif (
        m["brightness"] >= BLUR_MIN_BRIGHTNESS
        and m["detail"] < BLUR_VARIANCE
        and m["edge_ratio"] < BLUR_EDGE_RATIO
    ):
        status = HealthStatus.NEEDS_CLEANING
        notes.append(
            f"sharpness {m['detail']:.0f} (normal is 500+) with few edges - "
            "the whole picture is soft"
        )
    else:
        status = HealthStatus.OK

    return CameraHealth(status=status, notes=notes, **m)


def assess_sequence(frames: Sequence[np.ndarray]) -> CameraHealth:
    """
    Classify a camera from several frames taken over a visit.

    Sequences add one thing a single frame cannot show: a stalled feed. Real
    sensors always produce a little noise, so pixel-identical frames mean the
    picture stopped updating.
    """
    if not frames:
        raise ValueError("no frames to assess")

    health = assess_frame(frames[-1])
    if len(frames) < MIN_FRAMES_FOR_FROZEN:
        return health

    diffs = []
    for previous, current in zip(frames, frames[1:]):
        if previous.shape != current.shape:
            continue
        diffs.append(float(cv2.absdiff(previous, current).mean()))
    if not diffs:
        return health

    health.motion_between_frames = float(np.mean(diffs))

    # A no-signal screen is also perfectly still; report the more specific
    # cause rather than calling a blank channel "frozen".
    if health.motion_between_frames < FROZEN_DIFF and health.status is HealthStatus.OK:
        health.status = HealthStatus.FROZEN
        health.notes.append(
            f"frames identical over the visit (mean change "
            f"{health.motion_between_frames:.2f}) - the stream is not updating"
        )
    return health
