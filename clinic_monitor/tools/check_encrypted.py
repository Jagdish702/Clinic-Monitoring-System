"""
List every clinic whose Hik-Connect device shows an "Encrypted" camera -
one that needs its own password typed in (by a human, over scrcpy - see
DEPLOY_GCP.md) before patrol can see anything from it at all.

    python tools/check_encrypted.py               print to the console
    python tools/check_encrypted.py --email        also email the list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import notify  # noqa: E402
from control.navigator import PhoneNavigator  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", action="store_true",
                         help="also send the result via notify.send_email()")
    args = parser.parse_args()

    nav = PhoneNavigator()
    nav.launch()
    encrypted = nav.encrypted_devices()

    label = f"{config.STATE_NAME or '?'}/{config.CLUSTER_NAME or '?'}"
    if encrypted:
        print(f"{label}: {len(encrypted)} clinic(s) showing Encrypted cameras:")
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
