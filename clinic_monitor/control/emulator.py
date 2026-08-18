"""
Start and manage the Android emulator that stands in for a physical phone.

The rest of the system does not care whether the screen it captures belongs to
a handset on a USB cable or to an emulator on the same machine - both speak
ADB. Using an emulator removes the cable, the handset and the person who has
to plug it in, which is also what makes the system deployable on a server.

Nothing here assumes an emulator is *not* already running: starting a second
copy of the same AVD fails, so an existing one is always reused.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional

import config

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


class EmulatorError(RuntimeError):
    """The emulator could not be found or started."""


# --------------------------------------------------------------------------- #
# Finding the tooling
# --------------------------------------------------------------------------- #
def _candidate_sdks() -> List[Path]:
    paths = []
    if config.ANDROID_SDK:
        paths.append(Path(config.ANDROID_SDK))
    home = Path.home()
    paths += [
        Path("C:/Android/Sdk"),
        home / "AppData/Local/Android/Sdk",
        home / "Android/Sdk",
        home / "Library/Android/sdk",
    ]
    return paths


def emulator_binary() -> Path:
    """Locate emulator.exe / emulator, or explain where to look."""
    name = "emulator.exe" if _NO_WINDOW else "emulator"
    for sdk in _candidate_sdks():
        candidate = sdk / "emulator" / name
        if candidate.is_file():
            return candidate
    raise EmulatorError(
        "could not find the Android emulator. Set ANDROID_HOME to your SDK "
        "folder (the one containing an 'emulator' directory), or install it "
        "from Android Studio > SDK Manager > SDK Tools > Android Emulator."
    )


def list_avds() -> List[str]:
    """Every virtual device configured on this machine."""
    proc = subprocess.run(
        [str(emulator_binary()), "-list-avds"],
        capture_output=True, timeout=60, creationflags=_NO_WINDOW,
    )
    return [
        line.strip()
        for line in proc.stdout.decode("utf-8", "replace").splitlines()
        if line.strip() and not line.startswith("INFO")
    ]


def pick_avd(preferred: Optional[str] = None) -> str:
    """Choose which AVD to boot: the requested one, or the only one there is."""
    avds = list_avds()
    if not avds:
        raise EmulatorError(
            "no Android virtual devices exist. Create one in Android Studio > "
            "Device Manager (a Pixel with Google Play and API 31+ works well), "
            "then install Hik-Connect on it and sign in."
        )
    wanted = preferred or config.EMULATOR_AVD
    if wanted:
        for avd in avds:
            if avd.lower() == wanted.lower():
                return avd
        raise EmulatorError(
            f"no virtual device named {wanted!r}. Available: {', '.join(avds)}"
        )
    if len(avds) > 1:
        log.info("several AVDs available (%s) - using %s", ", ".join(avds), avds[0])
    return avds[0]


# --------------------------------------------------------------------------- #
# ADB helpers (kept local so this module does not depend on the navigator)
# --------------------------------------------------------------------------- #
def _adb(*args: str, timeout: float = 60.0) -> str:
    proc = subprocess.run(
        [config.ADB_PATH, *args],
        capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
    )
    return (
        proc.stdout.decode("utf-8", "replace")
        + proc.stderr.decode("utf-8", "replace")
    )


def running_serial() -> Optional[str]:
    """Serial of an already-running emulator, if any."""
    for line in _adb("devices").splitlines()[1:]:
        if "\t" not in line:
            continue
        serial, state = (part.strip() for part in line.split("\t", 1))
        if serial.startswith("emulator-") and state == "device":
            return serial
    return None


def is_booted(serial: str) -> bool:
    return "1" in _adb("-s", serial, "shell", "getprop", "sys.boot_completed")


# --------------------------------------------------------------------------- #
# Starting it
# --------------------------------------------------------------------------- #
def start(avd: Optional[str] = None, timeout: Optional[float] = None) -> str:
    """Boot an AVD and return its adb serial once it is ready for use."""
    name = pick_avd(avd)
    binary = emulator_binary()
    log.info("starting emulator %r (this takes a minute on a cold boot)", name)

    subprocess.Popen(
        [str(binary), "-avd", name, *config.EMULATOR_ARGS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW | _DETACHED,   # outlives this process
        cwd=str(binary.parent),                 # the binary expects its own dir
    )

    deadline = time.monotonic() + (timeout or config.EMULATOR_BOOT_TIMEOUT_SEC)
    serial = None
    while time.monotonic() < deadline:
        serial = serial or running_serial()
        if serial and is_booted(serial):
            log.info("emulator %s is up", serial)
            time.sleep(3.0)         # let the launcher settle before we tap
            wake(serial)
            return serial
        time.sleep(3.0)

    raise EmulatorError(
        f"emulator {name!r} did not finish booting within "
        f"{timeout or config.EMULATOR_BOOT_TIMEOUT_SEC:.0f}s. Try starting it "
        "once by hand from Android Studio to see what it reports."
    )


def wake(serial: str) -> None:
    """Wake the screen and dismiss the swipe-to-unlock screen if present."""
    _adb("-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    time.sleep(0.5)
    if "mDreamingLockscreen=true" in _adb("-s", serial, "shell", "dumpsys", "window"):
        # Emulators default to swipe-only security, which this clears.
        _adb("-s", serial, "shell", "input", "swipe", "540", "1600", "540", "600", "250")
        time.sleep(1.0)


def ensure_running(avd: Optional[str] = None) -> str:
    """
    Return a usable emulator serial, starting one only if needed.

    Reusing a running emulator matters: booting a second copy of the same AVD
    fails outright, and a cold boot costs a minute that a patrol should not pay
    on every round.
    """
    serial = running_serial()
    if serial and is_booted(serial):
        log.info("reusing the emulator already running (%s)", serial)
        wake(serial)
        return serial
    return start(avd)


def detect_hik_package(serial: str) -> Optional[str]:
    """
    Which build of Hik-Connect is installed on this device.

    The clinic phone runs a white-labelled package and an emulator usually has
    the Play Store one, so the package is discovered rather than assumed.
    """
    installed = _adb("-s", serial, "shell", "pm", "list", "packages")
    names = set(re.findall(r"package:(\S+)", installed))
    for candidate in config.HIK_PACKAGE_CANDIDATES:
        if candidate in names:
            return candidate
    return None
