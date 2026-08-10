"""
Layout calibration helper.

Grabs one screenshot from the device and draws the configured video region and
camera grid on top of it, so the fractional regions in ``config.py`` /
``clinics.json`` can be tuned until each box sits exactly on one camera tile.

    python tools/preview_layout.py
    python tools/preview_layout.py --clinic "Clinic A" --tiles
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from capture.adb_capture import CaptureError, build_capture  # noqa: E402
from capture.frame_reader import (  # noqa: E402
    camera_regions,
    detect_grid,
    has_pinned_regions,
    split_frame,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="preview the Hik-Connect grid layout")
    parser.add_argument("--clinic", help="clinic name (defaults to the first one)")
    parser.add_argument("--tiles", action="store_true", help="also save each cropped tile")
    parser.add_argument("--show", action="store_true", help="open a preview window")
    args = parser.parse_args()

    clinics = config.load_clinics()
    if not clinics:
        print("no clinics configured")
        return 1
    clinic = clinics[0]
    if args.clinic:
        matches = [c for c in clinics if c.name.lower() == args.clinic.lower()]
        if not matches:
            print(f"unknown clinic {args.clinic!r}")
            return 1
        clinic = matches[0]

    try:
        capture = build_capture(clinic)
        frame = capture.grab()
    except CaptureError as exc:
        print(f"capture failed: {exc}")
        return 1

    height, width = frame.shape[:2]
    canvas = frame.copy()

    grid = tuple(clinic.grid)
    if config.LAYOUT_AUTODETECT and not has_pinned_regions(clinic):
        grid = detect_grid(frame)
        state = "matches config" if grid == tuple(clinic.grid) else "DIFFERS from config"
        print(f"layout : detected {grid[0]}x{grid[1]} ({state} {clinic.grid[0]}x{clinic.grid[1]})")

    # The overall video region, in blue.
    vx, vy, vw, vh = clinic.video_region
    cv2.rectangle(
        canvas,
        (int(vx * width), int(vy * height)),
        (int((vx + vw) * width), int((vy + vh) * height)),
        (255, 160, 0),
        2,
    )

    # Every camera tile, in green.
    for name, region in camera_regions(clinic, grid=grid):
        x, y, w, h = region
        p1 = (int(x * width), int(y * height))
        p2 = (int((x + w) * width), int((y + h) * height))
        cv2.rectangle(canvas, p1, p2, (0, 220, 90), 2)
        cv2.putText(
            canvas, name, (p1[0] + 6, p1[1] + 22),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 90), 2, cv2.LINE_AA,
        )

    out_dir = Path(config.SCREENSHOT_DIR) / "_layout"
    out_dir.mkdir(parents=True, exist_ok=True)
    preview_path = out_dir / "layout_preview.jpg"
    cv2.imwrite(str(preview_path), canvas)
    print(f"screen: {width}x{height}")
    print(f"saved  : {preview_path}")

    if args.tiles:
        for tile in split_frame(frame, clinic, grid=grid):
            path = out_dir / f"tile_{tile.camera_name.replace(' ', '_')}.jpg"
            cv2.imwrite(str(path), tile.image)
            print(f"tile   : {path}  ({tile.size[0]}x{tile.size[1]})")

    if args.show:
        cv2.imshow("layout preview (press any key)", canvas)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    capture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
