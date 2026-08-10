"""Stage 1 - screen capture from the Hik-Connect mobile app."""

from .adb_capture import (
    AdbScreenCapture,
    CaptureError,
    ScrcpyWindowCapture,
    build_capture,
    list_devices,
)
from .frame_reader import (
    CameraFrame,
    FrameReader,
    camera_regions,
    detect_grid,
    split_frame,
)

__all__ = [
    "AdbScreenCapture",
    "CaptureError",
    "ScrcpyWindowCapture",
    "build_capture",
    "list_devices",
    "CameraFrame",
    "FrameReader",
    "camera_regions",
    "detect_grid",
    "split_frame",
]
