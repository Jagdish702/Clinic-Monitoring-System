# AI Clinic Monitoring System - Project Report

**Built:** 7-18 August 2026
**Deployment:** CureBay clinics, Odisha - 13 sites monitored through one Android device
**Repository:** https://github.com/Jagdish702/Clinic-Monitoring-System
**Status:** working system, verified against live clinic feeds

---

## 1. What this is

A Python system that monitors clinics using **the Hik-Connect mobile app screen
as its only video source**. No RTSP, no ONVIF, no ISAPI, no Hikvision SDK - it
reads pixels off an Android screen over ADB and nothing else.

It does four jobs:

| Job | Tool | When |
| --- | --- | --- |
| "What is happening at clinic X?" | `run.bat` | you ask, in plain English |
| Watch every clinic continuously | `patrol.bat` | runs unattended |
| Daily operational report per clinic | `report.bat` | end of day, or automatic |
| See alerts and reports | `dashboard.bat` | any time, in a browser |

---

## 2. Where it stands

| | |
| --- | --- |
| Python | 5,724 lines across 24 modules |
| Commits | 8, all pushed |
| Clinics reachable | 13 (plus test entries) |
| Events recorded | 179 |
| Observations recorded | 198 |
| Days of patrol data | 3 |
| Evidence screenshots | 190 (5 MB) |
| Database | 0.2 MB |

Runs on a phone over USB **or** on a headless Android emulator on the same PC -
the emulator path needs no cable, no handset and no display, which is also what
would make it deployable on a server.

---

## 3. The constraint that shaped everything

Screen capture is an indirect, lossy video source. Three consequences drove most
of the engineering:

1. **Frames are expensive to interpret and cheap to discard**, so the pipeline
   filters hard before spending anything on AI.
2. **The screen is not a stable feed.** The app switches views, other apps steal
   focus, the device sleeps, streams take seconds to connect. The system has to
   notice when it is looking at the wrong thing.
3. **Nothing labels the cameras.** Position in the grid is the only identity, so
   the code reads real view bounds from the app rather than assuming a layout.

---

## 4. The pipeline

```
Hik-Connect app on Android (phone or headless emulator)
        |  adb screencap every 1.5 s
        v
Stage 1  Capture --------> crop the video region, split the grid per camera
        v
Stage 2  Motion gate ------ no motion --> discard (removes most frames)
        v
Stage 3  YOLOv8n (CPU) ---- no person --> discard
        v
Stage 4  Gemini Flash ----> description + severity (throttled, fail-safe)
        v
Stage 5  SQLite event + observation + evidence screenshot
        v
Stage 6  Dashboard, and a daily report per clinic
```

Measured: YOLOv8n takes ~1.2 s on its first call, then **~140 ms per tile** on
CPU. A live run filtered **112 tiles down to 2 events**.

---

## 5. Every file, and what it is for

### Entry points

| File | Purpose |
| --- | --- |
| `run.py` / `run.bat` | Plain-English front door. Parses "check curebay banamalipur for 1 min" locally - no API call to work out intent - and prints what it understood before acting. |
| `ask.py` / `ask.bat` | One clinic, on demand. `--open` drives the phone itself. Describes **every** camera, because "the clinic is empty" is a valid answer. |
| `patrol.py` / `patrol.bat` | Rotates through every clinic continuously. Starts the emulator and the dashboard, refreshes reports as it goes. |
| `report.py` / `report.bat` | Daily operational report per clinic. |
| `dashboard/app.py` / `dashboard.bat` | Flask dashboard: alert feed, filters, and the reports viewer. |
| `main.py` | The original continuous monitor for a single clinic; person-gated alerting with a foreground guard. |

### Stage modules

| File | Purpose |
| --- | --- |
| `capture/adb_capture.py` | `adb exec-out screencap` with a shell fallback; device discovery; optional scrcpy backend. |
| `capture/frame_reader.py` | Paced reader. Crops the video region, splits the grid, detects the on-screen layout, survives outages. |
| `motion/motion_detector.py` | MOG2 background subtraction **plus** frame differencing, per camera, with warm-up and a noise gate. |
| `detection/yolo_detector.py` | YOLOv8n on CPU. Lazy load, thread-locked inference, COCO label mapping, evidence boxes. |
| `ai/gemini_analyzer.py` | Prompt, JSON parsing, severity normalisation, per-camera cooldown, hourly quota, circuit breaker, thinking-mode negotiation. |
| `analysis/camera_health.py` | Camera faults from the picture itself: no signal, frozen, too dark, dirty lens, obstructed. |
| `analysis/camera_role.py` | Infers which camera looks indoors and which outdoors, from stored descriptions. |
| `control/navigator.py` | Drives the app: launch, find a clinic by name, open its live view, measure tile positions. |
| `control/emulator.py` | Finds the SDK, boots an AVD headless, waits until it is genuinely usable, tunes its memory. |
| `storage/database.py` | SQLite schema and queries: `events` (alerts) and `observations` (every camera, every visit). |
| `storage/logger.py` | Writes the evidence JPEG and the event row; retention purge. |
| `dashboard/render.py` | Small Markdown-to-HTML converter for the report viewer, escaping first. |

### Support

| File | Purpose |
| --- | --- |
| `config.py` | Every tunable in one place, all overridable by environment variable. |
| `clinics.json` | Per-clinic crop regions. Not needed when using `--open` or `--emulator`. |
| `tools/selftest.py` | Six offline checks - no phone, no API key needed. |
| `tools/preview_layout.py` | Calibration: draws configured regions on a live screenshot. |
| `tools/seed_demo.py` | Fills the dashboard with sample alerts before hardware is connected. |

---

## 6. Capabilities

### Continuous patrol

One phone shows one clinic at a time, so covering all of them means visiting in
turn: `Clinic 1 -> analyse -> Clinic 2 -> ... -> back to 1`. Configurable
seconds per clinic. Offline clinics are skipped and retried next round. One bad
clinic never stops the patrol. Ctrl+C finishes the current clinic rather than
abandoning it mid-capture.

Measured pace: **~1m45s per clinic** at a 20-second watch window - navigation
dominates. A full 13-clinic lap at the default 60s is roughly **30 minutes**.

### Camera fault detection

Runs on frames the pipeline already has, costs nothing, needs no API call:

| Verdict | What it looks like | Action |
| --- | --- | --- |
| `no signal` | black and featureless | check power and NVR connection |
| `frozen` | frames pixel-identical - a live sensor always has noise | restart the channel |
| `too dark` | a real scene, almost no light | check IR and lighting |
| `needs cleaning` | normal brightness, whole picture soft | clean the lens |
| `obstructed` | lit, but most of the view carries no detail | something in front of the lens |

Thresholds calibrated against real tiles here and validated on 88 saved frames
with no false positives.

### Daily report

Four sections per clinic: operating hours, occupancy, checkup area, camera
health. Written to `reports/<CLINIC>/<date>.md`, viewable in the dashboard, and
rebuilt automatically as the patrol visits each clinic.

### Dashboard

Alert feed newest-first with severity and clinic filters and evidence
screenshots, plus an **All clinics** panel that lists every clinic, renders its
daily report, and rebuilds all reports for a chosen day in one click.

### Emulator

`patrol.bat` boots a headless Android emulator, starts the dashboard, and
patrols - no cable, no handset, nothing to plug in. The app package is detected
rather than assumed, so the same command works on the phone (white-labelled
`com.connect.enduser`) and the emulator (`com.hikvision.hikconnect`).

---

## 7. Decisions worth knowing

**The funnel is a cost control, not an optimisation.** Motion and YOLO exist to
stop Gemini being called. Four brakes: a motion gate (0.4% changed pixels), a
person gate (0.45 confidence), a per-camera cooldown (30 s), and an hourly
ceiling (150 calls). The patrol adds a fifth: idle and faulty cameras are
described locally instead of being sent to the API.

**`ask` deliberately does not person-gate.** Monitoring only pays for a
description when someone is present; a question must be answerable for an empty
room.

**Navigation reads the accessibility tree, not pixels.** Device names are
matched as text, so a reordered list still works. Tile crops come from real
`play_window_layout` bounds, so nothing is hand-tuned per clinic. Every step
verifies, so a failed tap raises instead of silently analysing the wrong clinic.

**Gemini model choice was forced by the API.** `gemini-2.0-flash` returns
`429 ... limit: 0` on this key and the 2.5 ids return `404 no longer available
to new users`. Only rolling aliases work; the default is
`gemini-flash-lite-latest`. `models.list()` returns `501 UNIMPLEMENTED`, so ids
can only be found by trying them.

**Open/closed comes from the indoor camera only.** See §9.

---

## 8. Problems found and fixed

Every item was a real failure observed against the live system.

| # | Problem | Why it mattered |
| --- | --- | --- |
| 1 | Truncated JSON, 8-52 s latencies | Thinking tokens ate the output budget |
| 2 | `gemini-2.0-flash` 429 on every call | Stage 4 was completely dead |
| 3 | Layout detector scanned the whole screen | Cropped app chrome on a portrait live view |
| 4 | Fixed 2x2 grid while the app showed one camera | One camera sliced into quadrants, filed under four wrong names |
| 5 | `--keep-open` navigated to X but labelled events Y | Every event filed under the wrong clinic |
| 6 | **Stale `ui.xml` parsed after a failed dump** | Screen checks answered for a *different screen*; "found" clinics named `Playback` |
| 7 | Any text accepted as a device name | Could tap into something unrelated |
| 8 | Offline check bled into the next card | Wrong clinic blamed for being down |
| 9 | `shell()` dropped stderr | Failures reported as `''` instead of "Killed" |
| 10 | Tapped while the list was still gliding | Row moved between measuring and tapping |
| 11 | Visit counting treated each camera row as a check | Report said "6 checks" and "only one check" in one document |
| 12 | "Closed 19:26, 146 min late" | Claimed a closure nobody observed |
| 13 | Frozen feed reported as "view blocked" | Sends someone to look for an obstruction instead of restarting the channel |
| 14 | Health judged while streams were still connecting | A working camera reported "no signal" |
| 15 | **Crops used the UI tree's height, not the screenshot's** | On-screen nav buttons make them differ (2072 vs 2340); every crop slid 13% down |
| 16 | Quiet clinics vanished from the dashboard | Idle cameras produced no event at all |
| 17 | Idle cameras claimed `clinic_status: Unclear` | Implied we looked and could not decide, when we never asked |
| 18 | Emulator "ready" at `sys.boot_completed` | Android sets it before it can be driven; `am start` did nothing |
| 19 | PowerShell text round-trips re-encoded files | Dashboard header showed mojibake |

Items 6 and 15 were the most dangerous: both produced **confident, wrong output
rather than errors**. The pattern applied throughout is *prefer a visible
failure to a plausible wrong answer*.

### Two failures that were not code

* **YOLO false positive.** A printed poster of a masked patient scored
  `person 0.53`. Gemini overrode it: *"posters on a wall, no people present."*
  The clearest justification for stage 4's cost.
* **uiautomator SIGKILLed by the phone**, around midnight, with the app closed
  and 1.8 GB free. A reboot restored it. Suspected Samsung Device Care.
  Navigation now reports this specifically and notes that capture is unaffected.

---

## 9. What the system found about the clinics

These are findings about the deployment, not the software.

**Cameras 03 and 04 are dead almost everywhere.** Of 98 recorded checks on those
channels, **96 were faulty** - overwhelmingly "no signal". The pattern is
identical at every clinic, which points to NVR configuration or unconnected
channels rather than eleven separately broken cameras. Roughly half of the
nominal camera estate is recording nothing.

**Camera numbering is not consistent.** Most clinics have Camera 01 outdoors and
Camera 02 indoors, but **BANAMALIPUR and DELANGA are reversed**, and GOP appears
to have both cameras indoors. Any rule like "Camera 01 is the entrance" would be
wrong at those sites.

**An outdoor camera cannot judge whether a clinic is open.** At the same clinic
and the same minute:

```
ASTARANGA   18:00   Camera 01 = Closed   Camera 02 = Open
NAYAHAT     19:00   Camera 01 = Closed   Camera 02 = Open
BAMANAL     18:00   Camera 01 = Closed   Camera 02 = Open
BANAMALIPUR 18:00   Camera 01 = Open     Camera 02 = Closed  (cameras reversed)
```

In all four the outdoor camera said Closed and the indoor one said Open - and
where the cameras are physically reversed, the verdicts flip with them. An empty
street in the evening looks shut whether or not the clinic is working. Operating
hours are now taken from the indoor camera alone, and the dashboard no longer
shows a status pill on outdoor cameras.

**Devices flap.** NIMAPADA was offline on every check across several days.
BANAMALIPUR and KUNDHEI were online in the morning and offline the same evening.

---

## 10. Failure behaviour

| Failure | Response |
| --- | --- |
| Gemini error | Retried with backoff; 3 consecutive failures pause stage 4 for 60 s |
| Gemini quota (429) | No retries - honours the API's own `retryDelay` |
| Gemini unavailable | Pipeline continues; the camera is described locally instead |
| No API key | Stage 4 disables itself at startup with a warning |
| Capture failure | Retried; motion model reset so a resumed feed does not fire a false alert |
| Phone leaves the live view | Detected within 20 s; `--keep-open` navigates back |
| Device offline | Skipped with a clear message, retried next round |
| Per-clinic exception | Caught; one bad clinic never stops a patrol |

---

## 11. Known limits

* **No alerting.** A High-severity event at 3 a.m. is a card on a dashboard
  nobody is watching. This remains the largest practical gap.
* **One device, one clinic at a time.** A full lap is ~30 minutes, so anything
  urgent could wait that long to be noticed.
* **No unique visitor counting.** Counting entries without double-counting needs
  continuous video and frame-to-frame tracking. The report gives a confirmed
  floor, an over-counting upper bound and hourly peaks, each labelled - and
  refuses to produce a single invented total.
* **Coverage gaps are never reported as findings.** If the patrol started at
  16:00, opening time reads "cannot tell", not "opened late".
* **Class coverage.** Stock YOLOv8n is COCO-trained: no wheelchair, door or
  medical-equipment class. Custom weights can be supplied via `CM_YOLO_MODEL`.
* **Tile identity comes from position**, not from the on-screen OSD text.
* **App updates** can rename the resource ids navigation depends on. It will
  fail loudly, not silently.
* **Free-tier rate limits.** Sustained monitoring hits 429s and degrades to
  local descriptions, recovering on its own.

---

## 12. Deliberately not implemented

RTSP, ONVIF, Hikvision API integration, automatic model switching, video
playback, timeline search, push notifications, multi-machine deployment.

The two most valuable next builds, in order:

1. **Alerting** - notify someone on High severity. This turns a tool you have to
   remember to check into a system that tells you things.
2. **Cloud deployment** - the headless emulator makes the whole system portable
   to a server. Capture must stay wherever the device is, so the natural split
   is agent-local, storage/dashboard/reports in the cloud.

---

## 13. Running it

```bash
run.bat check curebay banamalipur for 1 min, tell me what is happening
patrol.bat 60                 emulator + dashboard + 60s per clinic
report.bat                    today's reports for every clinic patrolled
dashboard.bat                 http://127.0.0.1:8000
```

Prerequisites: an Android emulator with Hik-Connect installed and signed in
(or a phone connected by USB with debugging enabled and the screen unlocked),
and `GEMINI_API_KEY` in `.env`.

Offline verification, no phone or API key required:

```bash
..\.venv\Scripts\python.exe tools\selftest.py
```

Data lives in `storage/events.db`, evidence in `screenshots/`, reports in
`reports/`, logs in `logs/`. Nothing leaves the machine except the frames sent
to Gemini.
