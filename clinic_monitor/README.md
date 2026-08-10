# AI Clinic Monitoring System

Monitors clinics using **the Hik-Connect mobile app screen as the only video
source**. No RTSP, no ONVIF, no ISAPI, no Hikvision SDK — frames are captured
from an Android device over ADB, filtered through two cheap local stages, and
only the interesting ones reach Gemini.

```
Hik-Connect app on Android
        │  adb screencap every 1–2 s
        ▼
Stage 1  Screen capture ──► crop the live video region, split the grid per camera
        ▼
Stage 2  OpenCV motion gate ──── no motion ──► skip (kills >90% of frames)
        ▼
Stage 3  YOLOv8n on CPU ──────── no person ──► skip
        ▼
Stage 4  Gemini 2.0 Flash ─────► description + severity (throttled, fail-safe)
        ▼
Stage 5  SQLite event log + screenshot
        ▼
Stage 6  Dashboard (newest first, filter by severity/clinic)
```

## 1. Install

A virtualenv with every dependency (OpenCV, NumPy, Flask, google-genai,
CPU-only torch, ultralytics) already exists at `..\.venv`, and `yolov8n.pt`
has been downloaded into this folder. Use that interpreter:

```bash
..\.venv\Scripts\python.exe main.py --list-devices
```

To rebuild the environment elsewhere:

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then create your `.env`:

```bash
copy .env.example .env
```

and put your key from https://aistudio.google.com/apikey into `GEMINI_API_KEY`.

### A note on the model id

The default is **`gemini-flash-lite-latest`**, not a pinned `gemini-2.0-flash`.
Newly issued API keys get `429 RESOURCE_EXHAUSTED … limit: 0` on the pinned
2.0 ids and `404 … no longer available to new users` on the 2.5 ids; only the
rolling aliases work. Probed against this project's key:

| Model id | Result |
| --- | --- |
| `gemini-flash-lite-latest` | works — default (cheapest per call) |
| `gemini-flash-latest` | works — stronger judgement on poor-quality tiles |
| `gemini-2.0-flash`, `-flash-lite` | 429 `limit: 0` |
| `gemini-2.5-flash`, `-flash-lite` | 404 closed to new users |
| `gemini-3-flash`, `-flash-lite` | 404 not found |

Override with `CM_GEMINI_MODEL`. Note `models.list()` returns
`501 UNIMPLEMENTED` for this key, so ids can only be found by trying them.

Flash models reason before answering, which eats the output-token budget and
truncates the JSON. The analyzer turns that off, negotiating the setting on the
first call (`thinking_budget=0` → `thinking_level="low"` → off) because each
model generation accepts a different one. That needs `google-genai >= 2.x`.

## 2. Prepare the phone

1. Enable **Developer options → USB debugging** on the Android device.
2. Connect it and accept the RSA prompt.
3. Open Hik-Connect, sign in, and leave it on the **multi-camera live view**.
4. Turn the screen timeout up (or enable "stay awake while charging") —
   `screencap` returns a black frame on a sleeping screen.

Check the connection:

```bash
python main.py --list-devices
```

If `adb` is not on PATH, set `CM_ADB_PATH` in `.env`
(on this machine: `C:\Android\Sdk\platform-tools\adb.exe`).

## 3. Calibrate the layout

Regions are fractions of the screen (`x, y, width, height`, each 0–1), so they
survive resolution changes. Preview the current guess:

```bash
python tools/preview_layout.py --tiles
```

It writes `screenshots/_layout/layout_preview.jpg` with the video region (blue)
and each camera tile (green) drawn on a live screenshot. Adjust
`video_region` / `grid` in `clinics.json` until the green boxes sit on the
camera tiles, then re-run.

```bash
copy clinics.example.json clinics.json
```

`clinics.json` (when present) overrides the defaults in `config.py`. One entry
per clinic; each entry maps to one Android device via `adb_serial`. Cameras get
grid slots automatically, or you can pin an explicit `region` per camera.

### Layout auto-detection

Hik-Connect flips between a full-screen camera and a grid as you use it. A
fixed grid would keep slicing quadrants out of a single camera and file them
under the wrong camera names, so the reader measures the black divider lines on
every frame and crops to whatever is actually on screen (`LAYOUT_AUTODETECT`).

* Detected layout **matches** `grid` → your configured camera names are used.
* Detected layout **differs** → tiles get positional names (`Full view`,
  `View 1`…`View n`) and a warning is logged, because which physical camera is
  in a tile cannot be read off the pixels.

So: for named events, leave the app parked on the grid view that `clinics.json`
describes. Pinning explicit `region` values on cameras disables detection for
that clinic and is always honoured as-is.

## 4. Run

```bash
python main.py --with-dashboard
```

Then open http://127.0.0.1:8000.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--clinic "Clinic A"` | monitor a single clinic |
| `--once` | one capture pass, then exit (smoke test) |
| `--no-gemini` | motion + YOLO only, zero API calls |
| `--no-annotate` | store clean screenshots without detection boxes |
| `--verbose` | debug logging |

Dashboard alone:

```bash
python dashboard/app.py
```

Offline checks (no phone, no API key needed):

```bash
python tools/selftest.py
python tools/seed_demo.py     # fills the dashboard with sample alerts
python tools/seed_demo.py --clear
```

The database currently holds 6 demo rows (`source = "demo"`) so the dashboard
has something to show — clear them with the command above once real events
start arriving.

Measured on this machine (CPU only): YOLOv8n is ~1.2 s on the first call while
the graph warms up, then ~140 ms per tile. At a 1.5 s capture interval that
leaves plenty of headroom, because the motion gate means most tiles never reach
YOLO at all.

## Just say what you want

```bash
run.bat open hik connect and check curebay banamalipur for 1 min, tell me what is happening
```

Or double-click `run.bat` and type the instruction when it asks.

[`run.py`](run.py) parses the sentence **locally** - no API call is spent
working out what you meant - and prints what it understood before doing
anything, so a misreading is visible rather than silent:

```
  clinic   : CUREBAY BANAMALIPUR
  watch for: 60s
  question : ...
```

It understands `curebay <place>` (or a quoted `"name"`), durations like
`1 min` / `90 seconds` / `2 minutes` (default 60s), and `list clinics`. The
whole sentence is passed on as the question, so "is anyone waiting?" is
answered directly. A misspelt clinic gets a "did you mean ...?" suggestion.

## Asking about one clinic on demand

The monitoring loop only spends a Gemini call when YOLO sees a person. A
question like "what is happening there?" has to be answerable for an empty
room too, so `ask.py` is the request/response counterpart - it watches for a
fixed window, then describes the most informative frame per camera.

```bash
python ask.py --open "CUREBAY NAYAHAT" --duration 60 --question "what is happening there right now?"
```

On Windows use the launcher instead - it finds the virtualenv itself and works
from cmd.exe, PowerShell or a double-click, so there is no shell syntax to get
wrong:

```bash
ask.bat "CUREBAY NAYAHAT" 60
```

`ask.bat` with no arguments lists the clinics. `dashboard.bat` serves the
dashboard.

`--open` drives the phone itself: launch Hik-Connect → scroll the device list →
match the name → tap live view → confirm the stream loaded → read each camera
tile's exact position from the app → watch → report. No `clinics.json` entry is
needed, and results land in the same database and dashboard (tagged
`source = "ask"`).

```bash
python ask.py --list-clinics          # every device in the app, in list order
python ask.py --clinic "X" --duration 60   # analyse what is already on screen
```

### How navigation works

[`control/navigator.py`](control/navigator.py) drives the app through its
**accessibility tree** (`uiautomator dump`), not pixels:

* device names are matched as text, so a reordered list still works
* tile crops come from real `play_window_layout` view bounds, so nothing is
  hand-tuned per clinic
* every step verifies (`am start` → confirm the activity; tap → confirm the
  live view opened), so a failed tap raises instead of leaving us on the wrong
  screen analysing the wrong clinic
* a device showing **Device Offline** is refused with a clear message rather
  than analysed as four black rectangles

The app package is white-labelled (`com.connect.enduser`, not
`com.hikvision.hikconnect`) - override with `CM_HIK_PACKAGE`. Resource ids that
would break on an app redesign are named in `config.py` under "Phone control".

### Keeping the monitor honest

`main.py` analyses whatever is on screen, so if the phone wanders off the live
view it will log your home screen as clinic activity. It now checks the
foreground every 20s and warns; pass `--keep-open` to have it navigate back:

```bash
python main.py --keep-open "CUREBAY NAYAHAT" --with-dashboard
```

Reopening also resets the motion background model - a new scene would otherwise
register as one huge motion event.

## Project layout

```
clinic_monitor/
├── capture/
│   ├── adb_capture.py     ADB / scrcpy backends, device discovery
│   └── frame_reader.py    paced reader, video-region crop, grid split
├── motion/
│   └── motion_detector.py MOG2 + frame differencing, per-camera state
├── detection/
│   └── yolo_detector.py   YOLOv8n CPU wrapper, class mapping, box drawing
├── ai/
│   └── gemini_analyzer.py prompt, JSON parsing, cooldown/quota/circuit breaker
├── control/
│   └── navigator.py       launch the app, find a clinic, open its live view
├── storage/
│   ├── database.py        SQLite schema + queries
│   └── logger.py          screenshot + event writer
├── dashboard/
│   ├── app.py             Flask routes and JSON API
│   └── templates/
├── tools/                 preview_layout.py, selftest.py, seed_demo.py
├── screenshots/           evidence images (clinic/date/HHMMSS_camera.jpg)
├── config.py              every tunable, plus clinic/camera layout
├── ask.py                 on-demand "what is happening at X?"
└── main.py                continuous monitoring
```

## How API usage is kept low

Four independent brakes, all in `config.py`:

1. **Motion gate** — a frame with less than `MOTION_MIN_AREA_RATIO` (0.4% of
   the tile) of changed pixels never reaches YOLO.
2. **Person gate** — no person above `YOLO_PERSON_CONF` (0.45) means no Gemini
   call, whatever else is in frame.
3. **Per-camera cooldown** — at most one Gemini call per camera per
   `GEMINI_COOLDOWN_SEC` (30 s).
4. **Hourly ceiling** — `GEMINI_MAX_CALLS_PER_HOUR` (150) shared across every
   clinic, enforced with a sliding window.

On a typical quiet clinic that lands around a few dozen calls per hour instead
of ~2400 frames.

## Failure behaviour

* **Gemini errors** — retried with backoff; after 3 consecutive failures
  stage 4 pauses for 60 s. The pipeline keeps running and writes a YOLO-only
  event (`source = "yolo"`) so the audit trail has no holes.
* **No API key** — stage 4 disables itself at startup with a warning.
* **Capture failures** — logged, retried after `CAPTURE_RETRY_DELAY_SEC`; the
  motion background model is reset so a resumed feed does not fire a false
  alert. After 10 consecutive failures that clinic's thread stops.
* **Per-tile exceptions** — caught and logged; one bad camera never takes down
  the clinic loop.

## Event schema

`storage/events.db`, table `events`:

`id, timestamp, ts_epoch, clinic_name, camera_name, description, severity,
confidence, screenshot_path, clinic_status, reason, staff_present,
patient_present, unusual_activity, immediate_attention, person_count,
motion_score, detections (JSON), source, acknowledged`

Screenshot paths are stored relative to `screenshots/` so the database stays
portable.

## Known limits

* **Class coverage** — stock YOLOv8n is COCO-trained: `person`, `chair`,
  `couch`, `bed`, `tv`/`laptop` (mapped to `monitor`), `dining table`. COCO has
  **no** wheelchair, door or medical-equipment class; those need custom
  weights, which you can drop in via `CM_YOLO_MODEL` — extra classes are passed
  through automatically.
* **Image quality** — a phone screenshot of a CCTV grid is lossy, and each tile
  is only a few hundred pixels wide. Fewer, larger tiles detect better than a
  3×3 grid.
* **YOLO false positives** — on this feed a printed poster of a masked patient
  scored `person 0.53`. Gemini caught it ("posters on a wall, no people
  present"), which is the main reason stage 4 is worth its cost; raise
  `CM_YOLO_PERSON_CONF` if you see a lot of it.
* **Tile identity** — a camera's name comes from its position in the grid, not
  from the on-screen OSD text. Reordering cameras in the app silently reorders
  the names.
* **One device per clinic** — each clinic runs in its own thread against its
  own `adb_serial`.
* **scrcpy backend** — implemented but optional; it needs `pip install mss`
  and a fixed `screen_region`. ADB `screencap` is the default and needs nothing
  extra.

## Deliberately not implemented yet

RTSP, ONVIF, Hikvision API, automatic model switching, video playback, timeline
search, push notifications, multi-machine deployment.
