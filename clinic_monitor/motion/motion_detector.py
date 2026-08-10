"""
Stage 2 - motion detection.

This is the cheapest stage and it does the heaviest filtering: on a quiet
clinic it discards well over 90% of frames so YOLO and Gemini never see them.

Two signals are combined:

* MOG2 background subtraction - adapts to slow lighting changes.
* Frame differencing against the previous frame - catches fast movement that
  MOG2 has already learnt into the background.

A frame counts as "motion" when the changed area exceeds
``MOTION_MIN_AREA_RATIO`` of the tile.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

import config

log = logging.getLogger(__name__)


@dataclass
class MotionResult:
    """Outcome of one motion check."""

    motion: bool
    area_ratio: float                                  # changed area / tile area
    boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)
    warming_up: bool = False

    @property
    def score(self) -> float:
        """0-1 style score, mostly useful for logging and the dashboard."""
        return round(min(1.0, self.area_ratio * 10.0), 4)

    def __bool__(self) -> bool:  # `if motion_result:`
        return self.motion


class MotionDetector:
    """Per-camera motion detector. One instance per camera, not shared."""

    def __init__(
        self,
        min_area_ratio: Optional[float] = None,
        work_width: Optional[int] = None,
        history: Optional[int] = None,
        var_threshold: Optional[float] = None,
        warmup_frames: Optional[int] = None,
        pixel_threshold: Optional[int] = None,
    ) -> None:
        self.min_area_ratio = (
            min_area_ratio if min_area_ratio is not None else config.MOTION_MIN_AREA_RATIO
        )
        self.work_width = work_width or config.MOTION_WORK_WIDTH
        self.pixel_threshold = (
            pixel_threshold if pixel_threshold is not None else config.MOTION_PIXEL_THRESHOLD
        )
        self.warmup_frames = (
            warmup_frames if warmup_frames is not None else config.MOTION_WARMUP_FRAMES
        )
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=history or config.MOTION_HISTORY,
            varThreshold=var_threshold or config.MOTION_VAR_THRESHOLD,
            detectShadows=False,
        )
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self._prev_gray: Optional[np.ndarray] = None
        self._frames_seen = 0

    # -- internals -------------------------------------------------------- #
    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        if w > self.work_width:
            scale = self.work_width / float(w)
            frame = cv2.resize(
                frame,
                (self.work_width, max(1, int(round(h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        k = config.MOTION_BLUR_KERNEL | 1  # force odd
        return cv2.GaussianBlur(gray, (k, k), 0)

    # -- public API ------------------------------------------------------- #
    def update(self, frame: np.ndarray) -> MotionResult:
        """Feed one frame and report whether meaningful motion occurred."""
        gray = self._preprocess(frame)
        self._frames_seen += 1

        fg = self._subtractor.apply(gray)
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)

        if self._prev_gray is not None and self._prev_gray.shape == gray.shape:
            diff = cv2.absdiff(self._prev_gray, gray)
            _, diff = cv2.threshold(diff, self.pixel_threshold, 255, cv2.THRESH_BINARY)
            mask = cv2.bitwise_or(fg, diff)
        else:
            mask = fg
        self._prev_gray = gray

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        mask = cv2.dilate(mask, self._kernel, iterations=config.MOTION_DILATE_ITERATIONS)

        if self._frames_seen <= self.warmup_frames:
            # The background model is still settling; everything looks like
            # motion, so report none.
            return MotionResult(motion=False, area_ratio=0.0, warming_up=True)

        total_area = float(mask.shape[0] * mask.shape[1])
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        changed = 0.0
        boxes: List[Tuple[int, int, int, int]] = []
        scale_x = frame.shape[1] / float(mask.shape[1])
        scale_y = frame.shape[0] / float(mask.shape[0])
        min_box_area = config.MOTION_MIN_BOX_AREA_RATIO * total_area

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_box_area:
                continue  # ignore tiny pixel changes (compression noise)
            changed += area
            x, y, w, h = cv2.boundingRect(contour)
            boxes.append(
                (
                    int(x * scale_x),
                    int(y * scale_y),
                    int(w * scale_x),
                    int(h * scale_y),
                )
            )

        area_ratio = changed / total_area if total_area else 0.0
        return MotionResult(
            motion=area_ratio >= self.min_area_ratio,
            area_ratio=round(area_ratio, 5),
            boxes=boxes,
        )

    def reset(self) -> None:
        """Forget the learnt background (e.g. after a capture outage)."""
        self._prev_gray = None
        self._frames_seen = 0
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=config.MOTION_HISTORY,
            varThreshold=config.MOTION_VAR_THRESHOLD,
            detectShadows=False,
        )


class MotionRegistry:
    """Lazily creates and keeps one :class:`MotionDetector` per camera."""

    def __init__(self, **detector_kwargs) -> None:
        self._detectors: Dict[Tuple[str, str], MotionDetector] = {}
        self._kwargs = detector_kwargs

    def get(self, clinic_name: str, camera_name: str) -> MotionDetector:
        key = (clinic_name, camera_name)
        if key not in self._detectors:
            self._detectors[key] = MotionDetector(**self._kwargs)
        return self._detectors[key]

    def update(self, clinic_name: str, camera_name: str, frame: np.ndarray) -> MotionResult:
        return self.get(clinic_name, camera_name).update(frame)

    def reset_all(self) -> None:
        for detector in self._detectors.values():
            detector.reset()
