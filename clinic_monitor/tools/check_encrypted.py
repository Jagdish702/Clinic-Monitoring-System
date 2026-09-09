"""
List every clinic whose Hik-Connect device shows an "Encrypted" camera -
one that needs its own password typed in (by a human, over scrcpy - see
DEPLOY_GCP.md) before patrol can see anything from it at all.

The "Encrypted" badge is baked into the video thumbnail's own pixels, not
a real accessibility-tree text node (confirmed by dumping the tree for a
known-encrypted clinic and finding nothing) - a screenshot and Gemini
vision is the only way to actually read it, the same way every other
visual judgment in this project already works.

    python tools/check_encrypted.py               print to the console
    python tools/check_encrypted.py --email        also email the list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import notify  # noqa: E402
from ai.gemini_analyzer import GeminiAnalyzer, parse_response  # noqa: E402
from capture.adb_capture import AdbScreenCapture  # noqa: E402
from control.navigator import PhoneNavigator  # noqa: E402

PROMPT = """This is a screenshot of a device list in a CCTV monitoring app
(Hik-Connect). Each card shows a clinic/device name followed by one or more
small camera thumbnails.

Some thumbnails are darkened/blurred with the word "Encrypted" and an
eye-slash icon overlaid on them - that specific camera needs its own
password before it can be viewed. Look only for that "Encrypted" overlay
text; do not guess from a dark or low-quality thumbnail alone.

List every clinic/device name (copy it exactly as written, including any
serial number in parentheses) that has at least one camera thumbnail
showing "Encrypted". If none do, return an empty list.

Return JSON only, no markdown, exactly this key:
{"encrypted_clinics": ["<name>", ...]}
"""


def find_encrypted(nav: PhoneNavigator, analyzer: GeminiAnalyzer) -> List[str]:
    """
    Scroll the device list like list_devices() does, asking Gemini about
    each newly-revealed screenful rather than trying to read the badge from
    the accessibility tree (it isn't there to read).
    """
    capture = AdbScreenCapture(serial=nav.serial, adb_path=nav.adb_path)
    nav.ensure_device_list()
    nav.scroll_to_top()

    seen_names: Set[str] = set()
    encrypted: Set[str] = set()
    dry = 0
    for _ in range(config.NAV_MAX_SCROLLS):
        nodes = nav.dump_nodes()
        rows = nav.device_rows(nodes)
        added = any(row.text not in seen_names for row in rows)
        seen_names.update(row.text for row in rows)

        frame = capture.grab()
        image_bytes = analyzer.encode_frame(frame)
        if image_bytes:
            try:
                text = analyzer._generate(image_bytes, PROMPT)
                data = parse_response(text) or {}
                for name in data.get("encrypted_clinics") or []:
                    encrypted.add(str(name).strip())
            except Exception as exc:
                print(f"  (Gemini check failed on this screen: {exc})")

        dry = 0 if added else dry + 1
        if dry >= 3:
            break
        nav._scroll_down()

    return sorted(encrypted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", action="store_true",
                         help="also send the result via notify.send_email()")
    args = parser.parse_args()

    nav = PhoneNavigator()
    nav.launch()
    analyzer = GeminiAnalyzer()
    encrypted = find_encrypted(nav, analyzer)

    label = f"{config.STATE_NAME or '?'}/{config.CLUSTER_NAME or '?'}"
    if encrypted:
        print(f"{label}: {len(encrypted)} clinic(s) showing an Encrypted camera:")
        for name in encrypted:
            print(f"  - {name}")
    else:
        print(f"{label}: no clinics currently show an Encrypted camera.")

    if args.email:
        body = (
            f"Cluster: {label}\n\n"
            + (
                "\n".join(f"- {name}" for name in encrypted)
                if encrypted else "No clinics currently show an Encrypted camera."
            )
        )
        sent = notify.send_email(f"[ENCRYPTED CAMERAS] {label}", body)
        print("email sent" if sent else "email NOT sent - check CM_EMAIL_* config")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
