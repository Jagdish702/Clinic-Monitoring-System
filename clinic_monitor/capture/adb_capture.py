"""
Screen capture backends.

The only supported video source is the Hik-Connect app rendered on an Android
device. Two backends are provided:

* ``AdbScreenCapture``  - ``adb exec-out screencap -p`` (default, no extra deps)
* ``ScrcpyWindowCapture`` - grabs a region of the desktop where a scrcpy mirror
  window is visible (requires the optional ``mss`` package)

No RTSP / ONVIF / ISAPI / camera SDK is used anywhere.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np

import config

log = logging.getLogger(__name__)

# Hide the console window that subprocess would otherwise flash on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CaptureError(RuntimeError):
    """Raised when a frame could not be captured."""


@dataclass
class DeviceInfo:
    serial: str
    state: str


def _run(args: List[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def list_devices(adb_path: Optional[str] = None) -> List[DeviceInfo]:
    """Return every device currently known to the adb server."""
    adb = adb_path or config.ADB_PATH
    try:
        proc = _run([adb, "devices"], timeout=config.CAPTURE_TIMEOUT_SEC)
    except FileNotFoundError as exc:
        raise CaptureError(
            f"adb executable not found at {adb!r}. Set CM_ADB_PATH in .env."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptureError("adb devices timed out") from exc

    devices: List[DeviceInfo] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        devices.append(DeviceInfo(serial.strip(), state.strip()))
    return devices


class BaseCapture:
    """Common interface for every capture backend."""

    name = "base"

    def grab(self) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class AdbScreenCapture(BaseCapture):
    """
    Capture the phone screen with ``adb screencap``.

    ``exec-out`` is tried first because it streams raw PNG bytes. Some older
    ROMs do not implement it, so we fall back to ``adb shell`` and undo the
    CRLF translation the shell applies to the binary stream.
    """

    name = "adb"

    def __init__(
        self,
        serial: Optional[str] = None,
        adb_path: Optional[str] = None,
        timeout: Optional[float] = None,
        max_width: Optional[int] = None,
    ) -> None:
        self.serial = serial
        self.adb_path = adb_path or config.ADB_PATH
        self.timeout = timeout or config.CAPTURE_TIMEOUT_SEC
        self.max_width = max_width or config.CAPTURE_MAX_WIDTH
        self._use_exec_out = True
        self._base = [self.adb_path]
        if serial:
            self._base += ["-s", serial]

    # -- helpers ---------------------------------------------------------- #
    def _screencap_bytes(self) -> bytes:
        if self._use_exec_out:
            proc = _run(self._base + ["exec-out", "screencap", "-p"], self.timeout)
            if proc.returncode == 0 and proc.stdout[:8] == b"\x89PNG\r\n\x1a\n":
                return proc.stdout
            log.debug(
                "exec-out screencap unusable on %s (rc=%s), falling back to shell",
                self.serial or "default",
                proc.returncode,
            )
            self._use_exec_out = False

        proc = _run(self._base + ["shell", "screencap", "-p"], self.timeout)
        if proc.returncode != 0:
            raise CaptureError(
                f"adb screencap failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        # The shell turns every \n into \r\n on the way out.
        return proc.stdout.replace(b"\r\n", b"\n")

    # -- public API ------------------------------------------------------- #
    def grab(self) -> np.ndarray:
        try:
            payload = self._screencap_bytes()
        except FileNotFoundError as exc:
            raise CaptureError(
                f"adb executable not found at {self.adb_path!r}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CaptureError("adb screencap timed out") from exc

        if not payload:
            raise CaptureError("adb screencap returned no data (device asleep?)")

        frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise CaptureError("could not decode the screencap PNG")

        return _downscale(frame, self.max_width)

    def wake_and_check(self) -> bool:
        """Best-effort check that the device is reachable and awake."""
        devices = list_devices(self.adb_path)
        if not devices:
            return False
        if self.serial:
            return any(d.serial == self.serial and d.state == "device" for d in devices)
        return any(d.state == "device" for d in devices)


class ScrcpyWindowCapture(BaseCapture):
    """
    Capture a fixed desktop region where a scrcpy mirror window is visible.

    Requires ``pip install mss``. Position the scrcpy window once, note its
    pixel bounds, and set ``screen_region`` on the clinic config.
    """

    name = "scrcpy"

    def __init__(self, screen_region: dict, max_width: Optional[int] = None) -> None:
        try:
            import mss  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise CaptureError(
                "the scrcpy backend needs the optional 'mss' package "
                "(pip install mss)"
            ) from exc
        import mss as _mss

        self._sct = _mss.mss()
        self.region = screen_region
        self.max_width = max_width or config.CAPTURE_MAX_WIDTH

    def grab(self) -> np.ndarray:
        shot = self._sct.grab(self.region)
        frame = np.array(shot)[:, :, :3]  # BGRA -> BGR
        return _downscale(np.ascontiguousarray(frame), self.max_width)

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


def _downscale(frame: np.ndarray, max_width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if max_width and w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(
            frame, (max_width, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA
        )
    return frame


def build_capture(clinic: "config.ClinicConfig") -> BaseCapture:
    """Instantiate the backend requested by a clinic configuration."""
    if clinic.backend == "scrcpy":
        if not clinic.screen_region:
            raise CaptureError(
                f"clinic {clinic.name!r} uses the scrcpy backend but has no "
                "screen_region configured"
            )
        return ScrcpyWindowCapture(clinic.screen_region)
    return AdbScreenCapture(serial=clinic.adb_serial)


def wait_for_device(
    serial: Optional[str] = None, attempts: int = 3, delay: float = 2.0
) -> bool:
    """Poll adb until the device shows up in the ``device`` state."""
    for attempt in range(attempts):
        try:
            devices = list_devices()
        except CaptureError as exc:
            log.error("%s", exc)
            return False
        for dev in devices:
            if dev.state != "device":
                continue
            if serial is None or dev.serial == serial:
                return True
        if attempt < attempts - 1:
            time.sleep(delay)
    return False
