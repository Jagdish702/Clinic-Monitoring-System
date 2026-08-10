"""
Drive the Hik-Connect app over ADB: launch it, find a clinic by name, open its
live view, and read the exact on-screen position of every camera tile.

Everything here works off the app's **accessibility tree**
(``uiautomator dump``) rather than pixels:

* text matching survives the device list being reordered or renamed
* tile crops come from real view bounds, so ``clinics.json`` never needs
  hand-tuning per clinic
* every step verifies it worked, so a failed tap raises instead of silently
  leaving us on the wrong screen

Still no RTSP / ONVIF / camera API - this only touches the phone UI.
"""

from __future__ import annotations

import difflib
import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import config

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DUMP_PATH = "/sdcard/clinic_monitor_ui.xml"
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*\s(\S+)\}")

Region = Tuple[float, float, float, float]


class NavigationError(RuntimeError):
    """The app could not be driven to the requested state."""


@dataclass
class Node:
    """One node of the on-screen accessibility tree."""

    text: str
    desc: str
    resource_id: str
    cls: str
    clickable: bool
    bounds: Tuple[int, int, int, int]      # (x1, y1, x2, y2)

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def center(self) -> Tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def cy(self) -> int:
        return self.center[1]

    def has_id(self, suffix: str) -> bool:
        return self.resource_id.split("/")[-1] == suffix


class PhoneNavigator:
    """Launch and navigate Hik-Connect on one Android device."""

    def __init__(
        self,
        serial: Optional[str] = None,
        adb_path: Optional[str] = None,
        package: Optional[str] = None,
    ) -> None:
        self.serial = serial
        self.adb_path = adb_path or config.ADB_PATH
        self.package = package or config.HIK_PACKAGE
        self._base = [self.adb_path] + (["-s", serial] if serial else [])

    # -- adb plumbing ----------------------------------------------------- #
    def _run(
        self, args: Sequence[str], timeout: float = 60.0
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                self._base + list(args),
                capture_output=True,
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise NavigationError(f"adb not found at {self.adb_path!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise NavigationError(f"adb {' '.join(args[:2])} timed out") from exc

    def _stdout(self, args: Sequence[str], timeout: float = 60.0) -> bytes:
        return self._run(args, timeout).stdout

    def shell(self, *args: str, timeout: float = 60.0) -> str:
        """
        Run a shell command, returning stdout **and** stderr.

        Android tools report their most important failures on stderr - a
        SIGKILLed uiautomator only ever says "Killed" there - so dropping it
        turns a diagnosable failure into an empty string.
        """
        proc = self._run(["shell", *args], timeout)
        return (
            proc.stdout.decode("utf-8", "replace")
            + proc.stderr.decode("utf-8", "replace")
        )

    def tap(self, x: int, y: int) -> None:
        log.debug("tap(%d, %d)", x, y)
        self.shell("input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def key(self, name: str) -> None:
        self.shell("input", "keyevent", name)

    def back(self) -> None:
        self.key("KEYCODE_BACK")

    # -- state ------------------------------------------------------------ #
    def current_focus(self) -> str:
        match = _FOCUS_RE.search(self.shell("dumpsys", "window"))
        return match.group(1) if match else ""

    def is_app_foreground(self) -> bool:
        return self.package in self.current_focus()

    def is_live_view(self) -> bool:
        focus = self.current_focus()
        return self.package in focus and config.HIK_VIDEO_ACTIVITY_HINT in focus

    # Content-based screen checks, used alongside the focus string. Both are
    # cheap and they fail differently: the focus can lag a transition, while a
    # dump can fail outright. Deciding from what the tree actually contains is
    # the safer default when we are about to tap something.
    @staticmethod
    def shows_live_view(nodes: Sequence[Node]) -> bool:
        return any(n.has_id(config.NAV_TILE_ID) for n in nodes)

    @staticmethod
    def shows_device_list(nodes: Sequence[Node]) -> bool:
        return any(
            n.has_id(config.NAV_PLAY_BUTTON_ID) or n.has_id(config.NAV_MORE_BUTTON_ID)
            for n in nodes
        ) and not PhoneNavigator.shows_live_view(nodes)

    def is_screen_locked(self) -> bool:
        out = self.shell("dumpsys", "window")
        return "mDreamingLockscreen=true" in out

    def wake(self) -> None:
        """Turn the screen on. screencap returns black on a sleeping display."""
        if "mScreenOn=true" not in self.shell("dumpsys", "power"):
            self.key("KEYCODE_WAKEUP")
            time.sleep(1.0)

    # -- the UI tree ------------------------------------------------------ #
    def dump_nodes(self, retries: int = 5) -> List[Node]:
        """
        Snapshot the accessibility tree.

        ``uiautomator dump`` waits for the UI to go idle and gives up with
        "could not get idle state" on screens that animate forever - which is
        every Hik-Connect screen, because both the device list and the live
        view play video continuously.

        The file is deleted first and the command's own output is checked,
        because otherwise a failed dump leaves the *previous* screen's XML on
        disk and we parse that instead: every screen check then silently
        answers for the wrong screen.
        """
        last = ""
        for attempt in range(retries):
            self.shell("rm", "-f", _DUMP_PATH)
            # "2>&1" is evaluated by the *device* shell. Without it, the
            # message mksh prints when uiautomator is SIGKILLed ("Killed")
            # reaches neither stream on the host and the failure looks blank.
            out = self.shell("uiautomator", "dump", _DUMP_PATH, "2>&1")
            last = out.strip()
            if "dumped to" in out.lower():
                raw = self._stdout(["exec-out", "cat", _DUMP_PATH]).decode(
                    "utf-8", "replace"
                )
                if raw.lstrip().startswith("<"):
                    return self._parse(raw)
            log.debug("uiautomator dump attempt %d/%d: %s", attempt + 1, retries, last)
            time.sleep(1.0 + attempt)  # give the animation a chance to settle

        if "killed" in last.lower():
            raise NavigationError(
                "the phone killed the uiautomator process (SIGKILL) on every "
                f"attempt. Navigation is unavailable until that recovers - "
                "reboot the phone, or close background apps and retry. Capture "
                "and analysis are unaffected: they use screencap, so "
                "`main.py` and `ask.py --clinic` still work if you open the "
                "live view by hand."
            )
        raise NavigationError(
            f"could not read the UI tree after {retries} attempts ({last!r}). "
            "The screen may be locked, or the app may never reach an idle state."
        )

    @staticmethod
    def _parse(raw: str) -> List[Node]:
        nodes: List[Node] = []
        for element in ET.fromstring(raw).iter("node"):
            match = _BOUNDS_RE.match(element.get("bounds", ""))
            if not match:
                continue
            nodes.append(
                Node(
                    text=(element.get("text") or "").strip(),
                    desc=(element.get("content-desc") or "").strip(),
                    resource_id=element.get("resource-id") or "",
                    cls=element.get("class", ""),
                    clickable=element.get("clickable") == "true",
                    bounds=tuple(int(v) for v in match.groups()),  # type: ignore[arg-type]
                )
            )
        return nodes

    @staticmethod
    def screen_size(nodes: Sequence[Node]) -> Tuple[int, int]:
        """
        Screen size for the *current rotation*, taken from the tree itself.

        ``wm size`` always reports the portrait dimensions, which would be
        wrong whenever the app is in landscape.
        """
        width = max((n.bounds[2] for n in nodes), default=0)
        height = max((n.bounds[3] for n in nodes), default=0)
        if not width or not height:
            raise NavigationError("could not determine the screen size")
        return width, height

    # -- launching -------------------------------------------------------- #
    def launch(self, timeout: Optional[float] = None) -> None:
        """Start Hik-Connect and wait until one of its screens has focus."""
        self.wake()
        if self.is_screen_locked():
            raise NavigationError(
                "the phone screen is locked - unlock it (a PIN cannot be "
                "entered from here) and retry"
            )
        self._run(
            ["shell", "am", "start", "-n", f"{self.package}/{config.HIK_LAUNCH_ACTIVITY}"]
        )
        deadline = time.monotonic() + (timeout or config.NAV_LAUNCH_TIMEOUT_SEC)
        while time.monotonic() < deadline:
            if self.is_app_foreground():
                log.info("Hik-Connect is in the foreground (%s)", self.current_focus())
                time.sleep(2.0)  # let the list finish drawing
                return
            time.sleep(1.0)
        raise NavigationError(
            f"Hik-Connect did not come to the foreground within {timeout}s "
            f"(focus is {self.current_focus()!r})"
        )

    def ensure_device_list(self) -> None:
        """Get back to the device list from wherever we are."""
        if not self.is_app_foreground():
            self.launch()
        for _ in range(4):
            nodes = self.dump_nodes()
            if self.shows_device_list(nodes):
                return
            log.debug("not on the device list yet - pressing back")
            self.back()
            time.sleep(1.5)
        # Re-launching is more reliable than pressing back forever.
        self.launch()
        if not self.shows_device_list(self.dump_nodes()):
            raise NavigationError(
                "could not get back to the Hik-Connect device list "
                f"(focus is {self.current_focus()!r})"
            )

    # -- finding a clinic -------------------------------------------------- #
    @staticmethod
    def device_rows(nodes: Sequence[Node]) -> List[Node]:
        """
        Device title nodes, in the order they appear on screen.

        A title is only accepted when the row's own â–¶ / ... control sits beside
        it. Matching on text shape alone let unrelated labels through - on the
        live view screen, "Playback" and "Event Messages" were being treated as
        clinics.
        """
        markers = [
            n
            for n in nodes
            if n.has_id(config.NAV_PLAY_BUTTON_ID) or n.has_id(config.NAV_MORE_BUTTON_ID)
        ]
        rows = [
            n
            for n in nodes
            if _looks_like_device(n)
            and any(
                abs(m.cy - n.cy) < 60 and m.center[0] > n.center[0] for m in markers
            )
        ]
        return sorted(rows, key=lambda n: n.bounds[1])

    def scroll_to_top(self, times: int = 14) -> None:
        """
        Scroll the list back to the top.

        The swipe deliberately starts below the status bar: starting near the
        top edge pulls down the notification shade instead of scrolling.
        """
        nodes = self.dump_nodes()
        _, height = self.screen_size(nodes)
        for _ in range(times):
            self.swipe(
                int(0.5 * 1080), int(height * 0.50), int(0.5 * 1080), int(height * 0.88), 250
            )
            time.sleep(0.2)
        time.sleep(1.0)

    def list_devices(self, max_scrolls: Optional[int] = None) -> List[str]:
        """Every device name in the list, in on-screen order."""
        self.ensure_device_list()
        self.scroll_to_top()
        seen: List[str] = []
        dry = 0
        for _ in range(max_scrolls or config.NAV_MAX_SCROLLS):
            added = False
            for row in self.device_rows(self.dump_nodes()):
                if row.text not in seen:
                    seen.append(row.text)   # display order, never sorted
                    added = True
            dry = 0 if added else dry + 1
            if dry >= 3:
                break
            self._scroll_down()
        return seen

    def _scroll_down(self) -> None:
        nodes = self.dump_nodes()
        width, height = self.screen_size(nodes)
        self.swipe(width // 2, int(height * 0.81), width // 2, int(height * 0.38), 400)
        time.sleep(config.NAV_SCROLL_SETTLE_SEC)

    def find_device(self, name: str, max_scrolls: Optional[int] = None) -> Node:
        """Scroll until the named device is on screen; return its title node."""
        needle = name.strip().lower()
        self.ensure_device_list()
        self.scroll_to_top()
        seen: List[str] = []
        for step in range(max_scrolls or config.NAV_MAX_SCROLLS):
            nodes = self.dump_nodes()
            for row in self.device_rows(nodes):
                if row.text not in seen:
                    seen.append(row.text)
                if needle in row.text.lower():
                    log.info("found %r after %d scrolls", row.text, step)
                    return row
            self._scroll_down()
        close = difflib.get_close_matches(name, seen, n=1, cutoff=0.4)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise NavigationError(
            f"no device matching {name!r} after "
            f"{max_scrolls or config.NAV_MAX_SCROLLS} scrolls.{hint} "
            f"Devices seen: {seen}"
        )

    def _card_nodes(self, nodes: Sequence[Node], row: Node) -> List[Node]:
        """
        Nodes belonging to one device card.

        The card ends where the next device title starts. A fixed-height window
        would be wrong: cards sit ~640px apart, so anything much larger reads
        the *next* clinic's "Device Offline" banner and blames this one.
        """
        top = row.bounds[1] - 40
        later = [r.bounds[1] for r in self.device_rows(nodes) if r.bounds[1] > row.bounds[1]]
        bottom = min(later) - 10 if later else row.bounds[1] + 700
        return [n for n in nodes if top <= n.cy <= bottom]

    def is_offline(self, nodes: Sequence[Node], row: Node) -> bool:
        return any(
            config.NAV_OFFLINE_TEXT in n.label.lower()
            for n in self._card_nodes(nodes, row)
        )

    # -- opening the live view --------------------------------------------- #
    def _stable_nodes(self, tries: int = 6, pause: float = 0.6) -> List[Node]:
        """
        Dump repeatedly until the device rows stop moving.

        A list that is still gliding from a flick invalidates coordinates
        between measuring and tapping: the row you aimed at has scrolled away
        and the tap lands on whatever took its place.
        """
        previous: Optional[Tuple] = None
        nodes: List[Node] = []
        for _ in range(tries):
            nodes = self.dump_nodes()
            signature = tuple((r.text, r.bounds[1]) for r in self.device_rows(nodes))
            if signature and signature == previous:
                return nodes
            previous = signature
            time.sleep(pause)
        log.debug("device list never settled; using the last snapshot")
        return nodes

    def _wait_for_live_view(self, timeout: Optional[float] = None) -> bool:
        deadline = time.monotonic() + (timeout or config.NAV_VIEW_TIMEOUT_SEC)
        while time.monotonic() < deadline:
            if self.is_live_view():
                return True
            time.sleep(1.0)
        return False

    def open_live_view(
        self, name: str, settle: Optional[float] = None, attempts: int = 3
    ) -> List[Region]:
        """
        Open a clinic's live view and return each camera tile's screen region
        as (x, y, w, h) fractions. Raises if anything did not work.
        """
        needle = name.strip().lower()
        self.find_device(name)          # scrolls it into view

        for attempt in range(attempts):
            nodes = self._stable_nodes()
            rows = [r for r in self.device_rows(nodes) if needle in r.text.lower()]
            if not rows:
                # It scrolled off while we were looking; go and find it again.
                self.find_device(name)
                continue
            row = rows[0]

            if self.is_offline(nodes, row):
                raise NavigationError(
                    f"{row.text!r} is showing 'Device Offline' - there is no "
                    "live stream to analyse"
                )

            target = self._play_button(nodes, row)
            log.info(
                "opening live view for %r via %s",
                row.text,
                target.resource_id or target.cls,
            )
            self.tap(*target.center)
            if self._wait_for_live_view():
                break
            log.warning(
                "tap did not open the live view (attempt %d/%d) - retrying",
                attempt + 1,
                attempts,
            )
            self.ensure_device_list()
        else:
            raise NavigationError(
                f"tapped play for {name!r} {attempts} times but the live view "
                f"never opened (focus is {self.current_focus()!r})"
            )

        # Streams connect a second or two after the activity appears.
        time.sleep(settle if settle is not None else config.NAV_STREAM_SETTLE_SEC)
        regions = self.tile_regions()
        if not regions:
            raise NavigationError("live view opened but no camera tiles were found")
        log.info("live view ready: %d tiles", len(regions))
        return regions

    def _play_button(self, nodes: Sequence[Node], row: Node) -> Node:
        """The â–¶ control on a device row, or a camera thumbnail as a fallback."""
        same_row = [
            n
            for n in nodes
            if n.clickable
            and abs(n.cy - row.cy) < 60
            and n.center[0] > row.center[0]
        ]
        for node in same_row:
            if node.has_id(config.NAV_PLAY_BUTTON_ID):
                return node

        thumbnails = [
            n
            for n in self._card_nodes(nodes, row)
            if n.clickable and n.has_id(config.NAV_THUMBNAIL_ID)
        ]
        if thumbnails:
            log.warning(
                "no %s on the %r row - falling back to the first camera thumbnail",
                config.NAV_PLAY_BUTTON_ID,
                row.text,
            )
            return min(thumbnails, key=lambda n: n.center)
        raise NavigationError(
            f"could not find a way to open {row.text!r} - the app layout may "
            "have changed (expected a control with id "
            f"{config.NAV_PLAY_BUTTON_ID!r})"
        )

    # -- reading the layout ------------------------------------------------ #
    def tile_regions(self) -> List[Region]:
        """
        Fractional screen region of every live camera tile, reading order.

        This is what removes hand-tuned crops: the bounds come from the app's
        own view hierarchy.
        """
        nodes = self.dump_nodes()
        width, height = self.screen_size(nodes)
        boxes = sorted(
            {n.bounds for n in nodes if n.has_id(config.NAV_TILE_ID)},
            key=lambda b: (b[1], b[0]),          # top-to-bottom, left-to-right
        )
        return [
            (x1 / width, y1 / height, (x2 - x1) / width, (y2 - y1) / height)
            for x1, y1, x2, y2 in boxes
        ]


def _looks_like_device(node: Node) -> bool:
    """
    Shape test for a device title, used together with the row-control check.

    Card captions ("Camera 01", "Expand (4 in Total)") and screen furniture
    must not be mistaken for clinic names.
    """
    text = node.text.strip()
    if len(text) < 3 or len(text) > 60:
        return False
    lowered = text.lower()
    if lowered.startswith(
        ("camera", "expand", "recent", "device offline", "playback", "event ")
    ):
        return False
    return node.bounds[0] < 400 and 0 < node.bounds[3] - node.bounds[1] < 120


def infer_grid(regions: Sequence[Region]) -> Tuple[int, int]:
    """Rows/cols implied by a set of tile regions."""
    if not regions:
        return (1, 1)
    rows = len({round(r[1], 3) for r in regions})
    cols = len({round(r[0], 3) for r in regions})
    return max(1, rows), max(1, cols)


def build_clinic(
    name: str,
    regions: Sequence[Region],
    adb_serial: Optional[str] = None,
) -> "config.ClinicConfig":
    """
    Turn measured tile regions into a ready-to-use ClinicConfig.

    Tiles are named Camera 01..N in reading order, matching the OSD labels
    Hikvision burns into each stream.
    """
    cameras = [
        config.CameraConfig(name=f"Camera {i + 1:02d}", region=tuple(region))
        for i, region in enumerate(regions)
    ]
    x0 = min(r[0] for r in regions)
    y0 = min(r[1] for r in regions)
    x1 = max(r[0] + r[2] for r in regions)
    y1 = max(r[1] + r[3] for r in regions)
    return config.ClinicConfig(
        name=name,
        adb_serial=adb_serial,
        backend="adb",
        video_region=(x0, y0, x1 - x0, y1 - y0),
        grid=infer_grid(regions),
        cameras=cameras,
    )
