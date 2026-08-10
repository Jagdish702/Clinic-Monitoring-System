# AI Clinic Monitoring System — Project Report

**Built:** 7–8 August 2026
**Deployment:** CureBay clinics, monitored through one Samsung SM-S911B (`RZCX60GS1CK`)
**Status:** working prototype, verified against live clinic feeds

---

## 1. What this is

A Python system that monitors clinics using **the Hik-Connect mobile app screen
as its only video source**. No RTSP, no ONVIF, no ISAPI, no Hikvision SDK — the
system reads pixels off an Android phone over ADB and nothing else.

It answers two different questions:

| Question | Tool | Trigger |
| --- | --- | --- |
| "Alert me when something happens" | `main.py` | continuous, person-gated |
| "What is happening at clinic X right now?" | `run.py` / `ask.py` | on demand |

Both write to the same database and appear in the same dashboard.

---

## 2. Why the constraint shaped the design

Screen capture is a lossy, indirect video source. Three consequences drove
most of the engineering:

1. **Frames are expensive to interpret and cheap to discard.** A phone
   screenshot of a 2×2 CCTV grid gives four tiles a few hundred pixels wide.
   Sending all of them to a vision model continuously would be wasteful and
   slow, so the pipeline filters hard before spending anything.
2. **The screen is not a stable video feed.** The app switches between
   full-screen and grid views, other apps steal focus, and the phone sleeps.
   The system has to notice when it is looking at the wrong thing.
3. **Nothing tells us which camera is which.** Position in the grid is the
   only identity signal, so the code reads real view bounds from the app
   rather than assuming a layout.

---

## 3. The pipeline

```
Hik-Connect app on Android
        │  adb screencap every 1.5 s
        ▼
Stage 1  Capture ─────► crop the video region, split the grid per camera
        ▼
Stage 2  Motion gate ──── no motion ──► discard (removes most frames)
        ▼
Stage 3  YOLOv8n (CPU) ── no person ──► discard
        ▼
Stage 4  Gemini Flash ──► description + severity (throttled, fail-safe)
        ▼
Stage 5  SQLite event + evidence screenshot
        ▼
Stage 6  Dashboard (newest first, filter by severity / clinic)
```

Measured on this machine: motion checks are negligible; YOLOv8n takes ~1.2 s on
its first call while the graph warms up, then **~140 ms per tile** on CPU. At a
1.5 s capture interval there is ample headroom, because most tiles never reach
YOLO at all.

---

## 4. Every file, and what it is for

### Entry points

| File | Purpose |
| --- | --- |
| `run.py` | **Plain-English front door.** Parses a sentence ("check curebay banamalipur for 1 min") locally — no API call — prints what it understood, then runs the query. |
| `ask.py` | On-demand query. Watches a clinic for a fixed window and describes what it saw. `--open` drives the phone itself. |
| `main.py` | Continuous monitoring. One thread per clinic, person-gated alerts, funnel statistics, graceful shutdown. |
| `dashboard/app.py` | Flask dashboard and JSON API. |
| `run.bat`, `ask.bat`, `dashboard.bat` | Windows launchers. They locate the virtualenv themselves and work from cmd.exe, PowerShell or a double-click. |

### Stage modules

| File | Purpose |
| --- | --- |
| `capture/adb_capture.py` | `adb exec-out screencap` with a shell fallback for ROMs that lack it; device discovery; optional scrcpy/`mss` backend. |
| `capture/frame_reader.py` | Paced reader. Crops the live-video region, splits the grid into one tile per camera, detects the on-screen layout, survives capture outages. |
| `motion/motion_detector.py` | MOG2 background subtraction **plus** frame differencing, per camera. Warm-up frames, contour-area gate that ignores compression noise. |
| `detection/yolo_detector.py` | YOLOv8n on CPU. Lazy load, thread-locked inference, COCO→canonical label mapping, evidence box drawing. |
| `ai/gemini_analyzer.py` | Prompt, JSON parsing, severity normalisation, per-camera cooldown, hourly quota, circuit breaker, thinking-mode negotiation. |
| `storage/database.py` | SQLite schema and queries. WAL mode so the dashboard can read while the monitor writes; thread-local connections. |
| `storage/logger.py` | Writes the evidence JPEG and the event row; retention purge. |
| `control/navigator.py` | Drives the phone: launch the app, find a clinic by name, open its live view, measure each camera tile's exact position. |

### Support

| File | Purpose |
| --- | --- |
| `config.py` | Every tunable in one place, all overridable by environment variable. Clinic layout, thresholds, quotas, resource ids. |
| `clinics.json` | Per-clinic crop regions. Only needed when *not* using `--open`. |
| `tools/selftest.py` | Six offline checks — layout, layout detection, motion, storage, Gemini parsing, YOLO. No phone or API key needed. |
| `tools/preview_layout.py` | Calibration: draws the configured regions on a live screenshot. |
| `tools/seed_demo.py` | Fills the dashboard with sample alerts before hardware is connected. |

---

## 5. Decisions worth knowing

### The three-tier funnel is a cost control, not just an optimisation

Motion and YOLO exist to stop Gemini from being called. Four independent
brakes, all in `config.py`:

1. **Motion gate** — under 0.4 % changed pixels never reaches YOLO.
2. **Person gate** — no person above 0.45 confidence means no Gemini call.
3. **Per-camera cooldown** — at most one call per camera per 30 s.
4. **Hourly ceiling** — 150 calls/hour shared across every clinic.

A live run recorded the funnel working: **112 tiles → 17 motion → 17 YOLO →
7 person → 2 events.**

### `ask.py` deliberately does *not* person-gate

Monitoring only pays for a description when someone is present. But "what is
happening?" must be answerable for an empty room — "the clinic is empty and
closed" is a valid, useful answer. So the query path watches, tracks activity
with the cheap stages, then describes the most informative frame per camera
regardless of whether a person was found.

### Navigation reads the app's accessibility tree, not pixels

`control/navigator.py` uses `uiautomator dump`:

* device names are matched as **text**, so a reordered list still works
* tile crops come from real `play_window_layout` view bounds, so `clinics.json`
  needs no per-clinic tuning
* every step verifies (`am start` → confirm the activity; tap → confirm the
  live view opened), so a failed tap raises instead of silently analysing the
  wrong clinic
* a device showing **Device Offline** is refused with a clear message rather
  than analysed as four black rectangles

Pixel matching would break on any reorder; a vision model would cost an API
call per scroll step and be less reliable than exact text.

### Gemini model choice was forced by the API, not preference

The spec asked for `gemini-2.0-flash`. That model returns
`429 RESOURCE_EXHAUSTED … limit: 0` on this project's key, and the 2.5 ids
return `404 … no longer available to new users`. Probe results:

| Model id | Result |
| --- | --- |
| `gemini-flash-lite-latest` | works — **current default** |
| `gemini-flash-latest` | works |
| `gemini-2.0-flash`, `-flash-lite` | 429, quota limit 0 |
| `gemini-2.5-flash`, `-flash-lite` | 404, closed to new users |
| `gemini-3-flash`, `-flash-lite` | 404, not found |

`models.list()` returns `501 UNIMPLEMENTED` for this key, so ids can only be
found by trying them. Override with `CM_GEMINI_MODEL`.

Flash models also reason before answering, which consumed the output-token
budget and truncated the JSON mid-sentence. Thinking is now disabled, with the
setting **negotiated on the first call** (`thinking_budget=0` →
`thinking_level="low"` → off) because each model generation accepts a different
one.

---

## 6. Problems found and fixed

Every item here was a real failure observed against the live system, not a
hypothetical.

| # | Problem | Why it mattered |
| --- | --- | --- |
| 1 | Truncated JSON from Gemini, 8–52 s latencies | Thinking tokens ate the output budget; responses were unparsable |
| 2 | `gemini-2.0-flash` returned 429 on every call | Stage 4 was completely dead |
| 3 | Layout detector scanned the whole screen | On a portrait live view the grid is a band under the title bar; it would crop app chrome |
| 4 | Fixed 2×2 grid while the app showed one camera | One camera was sliced into quadrants and filed under four wrong camera names |
| 5 | `--keep-open` navigated to X but labelled events Y | Every event filed under the wrong clinic, looking perfectly normal |
| 6 | **Stale `ui.xml` parsed after a failed dump** | Screen checks answered for a *different screen*; the navigator "found" clinics named `Playback` and `Event Messages` |
| 7 | Any text accepted as a device name | Could tap into something unrelated |
| 8 | Offline check scanned 700 px while cards sit ~640 px apart | The *next* clinic's offline banner blamed on this one |
| 9 | `shell()` dropped stderr | Failures reported as `''` instead of "Killed" |
| 10 | Tapped while the list was still gliding | Row moved between measuring and tapping; live view never opened |

Item 6 was the most dangerous: it produced confident, wrong answers rather than
errors. The fix — unique filename, delete before dumping, verify the command
reported success, fail loudly — is the pattern applied throughout: **prefer a
visible failure to a plausible wrong answer.**

### Two failures that were *not* code

* **YOLO false positive.** A printed poster of a masked patient scored
  `person 0.53`. Gemini overrode it: *"posters on a wall, no people present."*
  This is the clearest justification for stage 4's cost.
* **uiautomator SIGKILLed by the phone.** Around midnight, every dump was
  killed — with the app closed, after a force-stop, with 1.8 GB free and no
  leftover processes. A reboot restored it. Suspected Samsung Device Care
  auto-optimisation. Navigation now reports this specifically and notes that
  capture and analysis are unaffected.

---

## 7. Verified against live feeds

* **13 clinics** enumerated from the app, in true list order.
* **CUREBAY CHHAITANA** — first end-to-end run; person walking outside past
  resting cattle at night, correctly described.
* **CUREBAY NAYAHAT** — first fully automatic run (app launched from the home
  screen); empty and closed at 23:45.
* **CUREBAY BANAMALIPUR** — staff member at the desk with a patient facing
  them, 11:39; entrance with parked motorcycles.
* **CUREBAY NIMAPADA** — persistently **offline** on every check across two
  days. This is an operational finding: that clinic currently has no monitorable
  feed at all.

Evidence crops were opened and inspected to confirm the system was reading the
intended pixels, not just producing plausible text.

---

## 8. Failure behaviour

| Failure | Response |
| --- | --- |
| Gemini error | Retried with backoff; 3 consecutive failures pause stage 4 for 60 s |
| Gemini quota (429) | No retries — honours the API's own `retryDelay`, one concise log line |
| Gemini unavailable | Pipeline continues; YOLO-only event written (`source = "yolo"`) so the audit trail has no holes |
| No API key | Stage 4 disables itself at startup with a warning |
| Capture failure | Retried; motion background model reset so a resumed feed does not fire a false alert |
| Phone leaves the live view | Detected within 20 s; `--keep-open` navigates back and resets the motion model |
| Device offline | Refused with a clear message instead of analysing black frames |
| Per-tile exception | Caught and logged; one bad camera never stops the clinic loop |

---

## 9. Known limits

* **Class coverage.** Stock YOLOv8n is COCO-trained: `person`, `chair`,
  `couch`, `bed`, `tv`/`laptop`, `dining table`. COCO has **no** wheelchair,
  door or medical-equipment class. Custom weights can be supplied via
  `CM_YOLO_MODEL`; extra classes pass through automatically.
* **One phone, one feed.** Watching clinic A means not watching B. Covering all
  13 continuously needs a rotation scheduler, which does not exist yet.
* **Tile identity comes from position**, not from the on-screen OSD text.
  Reordering cameras in the app silently reorders the names.
* **App updates.** Tap targets are anchored to resource ids from build
  6.13.820.0729. A redesign will break navigation — loudly, not silently.
* **Screen lock.** A PIN cannot be entered from the PC. The phone must be
  unlocked.
* **Free-tier rate limits.** Sustained monitoring hits 429s; the pipeline
  degrades to YOLO-only events and recovers on its own.
* **No alerting.** A High-severity event at 3 a.m. becomes a red card on a
  dashboard nobody is watching. This is the largest practical gap.

---

## 10. Deliberately not implemented

RTSP, ONVIF, Hikvision API integration, automatic model switching, video
playback, timeline search, push notifications, multi-machine deployment.

The two most valuable next builds, in order:

1. **Rotation + alerting** — cycle all 13 clinics on a schedule and push a
   notification on High severity. This turns a tool you must remember to run
   into a system that watches on its own.
2. **A watchdog for the uiautomator kill** — if that recurs nightly, detect it
   and recover automatically instead of needing a manual reboot.

---

## 11. Running it

```bash
run.bat open hik connect and check curebay banamalipur for 1 min, tell me what is happening
ask.bat "CUREBAY BANAMALIPUR" 60
dashboard.bat
```

Prerequisites: phone connected by USB, **unlocked**, USB debugging enabled,
signed into Hik-Connect. `GEMINI_API_KEY` in `.env`.

Offline verification, no phone or API key required:

```bash
..\.venv\Scripts\python.exe tools\selftest.py
```

Data lives in `storage/events.db`, evidence in `screenshots/`, logs in
`logs/clinic_monitor.log`. Nothing leaves the machine except the frames sent to
Gemini.
