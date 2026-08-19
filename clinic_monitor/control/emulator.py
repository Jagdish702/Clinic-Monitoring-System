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
import os
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
    # Ask the OS directly. This used to key off CREATE_NO_WINDOW being present,
    # which happens to be Windows-only - correct by accident, and confusing to
    # anyone reading it on the Linux host this now also runs on.
    name = "emulator.exe" if os.name == "nt" else "emulator"
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


def is_ready(serial: str) -> bool:
    """
    Whether the device can actually be driven, not merely booted.

    ``sys.boot_completed`` flips well before Android is usable: the window
    manager reports no focused window and the package manager is not yet
    answering, so an ``am start`` issued at that moment does nothing and the
    launch times out on a device that looked ready.
    """
    if not is_booted(serial):
        return False
    if "mCurrentFocus" not in _adb("-s", serial, "shell", "dumpsys", "window"):
        return False
    return "package:" in _adb("-s", serial, "shell", "pm", "list", "packages")


def wait_until_ready(serial: str, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_ready(serial):
            return True
        time.sleep(3.0)
    return False


# --------------------------------------------------------------------------- #
# Starting it
# --------------------------------------------------------------------------- #
def start(avd: Optional[str] = None, timeout: Optional[float] = None) -> str:
    """Boot an AVD and return its adb serial once it is ready for use."""
    name = pick_avd(avd)
    binary = emulator_binary()
    args = list(config.EMULATOR_ARGS)
    if config.EMULATOR_HEADLESS:
        # Nothing ever looks at the window; frames come over ADB.
        args.append("-no-window")
    log.info(
        "starting emulator %r%s (this takes a minute on a cold boot)",
        name,
        " headless" if config.EMULATOR_HEADLESS else "",
    )

    subprocess.Popen(
        [str(binary), "-avd", name, *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW | _DETACHED,   # outlives this process
        cwd=str(binary.parent),                 # the binary expects its own dir
    )

    deadline = time.monotonic() + (timeout or config.EMULATOR_BOOT_TIMEOUT_SEC)
    serial = None
    while time.monotonic() < deadline:
        serial = serial or running_serial()
        if serial and is_ready(serial):
            log.info("emulator %s is up and ready", serial)
            # A just-booted emulator is still finishing background work, and
            # launching a video app into that load is what makes it hang long
            # enough for Android to raise an ANR dialog. A short pause here is
            # far cheaper than recovering from one.
            time.sleep(config.EMULATOR_SETTLE_SEC)
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
    if serial:
        if not is_ready(serial):
            log.info("an emulator is present but still starting - waiting")
            if not wait_until_ready(serial):
                raise EmulatorError(
                    f"emulator {serial} booted but never became usable "
                    "(no focused window or package manager)"
                )
        log.info("reusing the emulator already running (%s)", serial)
        wake(serial)
        return serial
    return start(avd)


def avd_config_path(avd: str) -> Path:
    return Path.home() / ".android" / "avd" / f"{avd}.avd" / "config.ini"


def tune_avd(avd: Optional[str] = None, apply: bool = False) -> dict:
    """
    Report (and optionally apply) memory settings that reduce emulator lag.

    The stock AVD gets 2048 MB and a 256 MB app heap, which is thin for
    Android 12 decoding several camera streams at once - the app stalls and
    frames arrive late. Raising both is the change that matters most after
    running headless.

    The emulator must be stopped first: it rewrites config.ini on exit and
    would discard edits made while running.
    """
    name = pick_avd(avd)
    path = avd_config_path(name)
    if not path.is_file():
        raise EmulatorError(f"no config.ini for AVD {name!r} at {path}")

    settings = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            settings[key.strip()] = value.strip()

    wanted = {
        "hw.ramSize": str(config.EMULATOR_RAM_MB),
        "vm.heapSize": str(config.EMULATOR_HEAP_MB),
    }
    changes = {k: (settings.get(k), v) for k, v in wanted.items() if settings.get(k) != v}

    if apply and changes:
        if running_serial():
            raise EmulatorError(
                "stop the emulator before tuning it - it rewrites config.ini "
                "when it exits and would undo these changes"
            )
        backup = path.with_suffix(".ini.backup")
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.partition("=")[0].strip()
            lines.append(f"{key}={wanted[key]}" if key in wanted else line)
        for key, value in wanted.items():
            if key not in settings:
                lines.append(f"{key}={value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("tuned %s (backup at %s)", path.name, backup.name)

    return {"avd": name, "path": str(path), "changes": changes,
            "applied": bool(apply and changes)}


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


def _cli(argv=None) -> int:
    """`python -m control.emulator [--tune|--apply|--start|--stop]`"""
    import argparse

    parser = argparse.ArgumentParser(description="manage the Android emulator")
    parser.add_argument("--avd", help="which virtual device")
    parser.add_argument("--tune", action="store_true",
                        help="show memory settings that would reduce lag")
    parser.add_argument("--apply", action="store_true",
                        help="apply those settings (emulator must be stopped)")
    parser.add_argument("--start", action="store_true", help="boot it")
    parser.add_argument("--stop", action="store_true", help="shut it down")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.stop:
        serial = running_serial()
        if not serial:
            print("no emulator running")
            return 0
        _adb("-s", serial, "emu", "kill")
        print(f"stopping {serial}")
        return 0

    if args.tune or args.apply:
        result = tune_avd(args.avd, apply=args.apply)
        print(f"AVD: {result['avd']}\n{result['path']}")
        if not result["changes"]:
            print("already tuned - nothing to change")
        for key, (old, new) in result["changes"].items():
            verb = "changed" if result["applied"] else "would change"
            print(f"  {verb} {key}: {old} -> {new}")
        if result["changes"] and not result["applied"]:
            print("\nrun again with --apply (stop the emulator first)")
        return 0

    if args.start:
        print(start(args.avd))
        return 0

    print(f"AVDs: {', '.join(list_avds())}")
    print(f"running: {running_serial() or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
