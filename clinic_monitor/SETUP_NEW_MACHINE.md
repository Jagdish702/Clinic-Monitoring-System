# Setting the project up on another machine

Written for moving to a second laptop. The system is **already deployed and
running** on a GCE VM, so the first question is what you actually want to do
from the new machine — the answer changes the install list from "a browser" to
"about 3 GB and a working hypervisor".

| I want to… | What to install | Time |
| --- | --- | --- |
| **A. Watch the dashboard** | Nothing. It is a public HTTPS URL. | 0 min |
| **B. Edit code and deploy to the VM** | Git, Google Cloud CLI | ~15 min |
| **C. Run the whole pipeline locally** | Everything in section C | ~1–2 h |

Most day-to-day work is **B**. You only need **C** to develop against a real
Hik-Connect screen without touching the production VM.

---

## What git does *not* carry

`.gitignore` deliberately excludes these, so cloning the repo is not enough.
Copy them across by hand:

| File | What it is | How to move it |
| --- | --- | --- |
| `clinic_monitor/.env` | **Gemini API key** and tuning | USB stick or a password manager — **never** email or chat |
| `clinic_monitor/clinics.json` | Static clinic/grid config | Copy, or let the patrol read the device list live |
| `clinic_monitor/storage/events.db` | All event history | Only if you want the history locally; the VM has its own |
| `clinic_monitor/screenshots/` | Evidence images | Usually not worth copying (large) |
| `~/.ssh/google_compute_engine*` | SSH key the VM already trusts | See the gotcha in section B |

The `.env` holds a live API key. Treat it like a password: if it goes through
anything you would not put a password through, rotate it at
<https://aistudio.google.com/apikey> afterwards.

---

## A. Just watching

Open the dashboard URL in any browser and sign in with the shared username and
password. Nothing to install, works from any machine or phone.

The URL and credentials are not written down in this repo on purpose. Get them
from whoever set it up.

---

## B. Editing code and deploying (the usual case)

### 1. Git

<https://git-scm.com/download/win> — accept the defaults.

```bash
git clone https://github.com/Jagdish702/Clinic-Monitoring-System.git
cd "Clinic-Monitoring-System"
```

Then copy `.env` and `clinics.json` into `clinic_monitor/` as above.

### 2. Google Cloud CLI

<https://cloud.google.com/sdk/docs/install> — the Windows installer.

```bash
gcloud auth login
gcloud config set project curebay-innovation
```

Sign in with the Google account that already has access to the project. If it
says the account lacks permission, someone with project admin has to grant it —
that is not something the CLI can fix.

### 3. The SSH gotcha

`gcloud compute ssh` makes a new key pair per machine and pushes the public key
to **project** metadata. On this project that write currently fails:

```
Updating project ssh metadata... failed.
```

The old laptop still works because its key is already in the **instance**
metadata. A brand-new laptop's key is in neither, so SSH will fail with
"Remote side unexpectedly closed network connection".

Two ways out, easiest first:

1. **Copy the existing key.** Move `~/.ssh/google_compute_engine`,
   `.pub` and `.ppk` from the old laptop to the same path on the new one.
   The VM already trusts it.
2. **Add the new key to instance metadata** — needs someone with
   `compute.instances.setMetadata` on the project.

Also note the login user is `HP@`, not your email:

```bash
gcloud compute ssh HP@clinic-monitor --zone=asia-south1-a --project=curebay-innovation
```

Do **not** add `--tunnel-through-iap`; IAP is not working on this project and
that flag makes the connection fail.

### 4. Deploying a change

The VM pulls from GitHub — you never copy files to it.

```bash
git add -A
git commit -m "what changed and why"
git push origin main
```

```bash
gcloud compute ssh HP@clinic-monitor --zone=asia-south1-a --project=curebay-innovation --command='cd /opt/clinic-monitoring && sudo -u jagdish_sahoo git pull --ff-only && sudo systemctl restart clinic-dashboard@jagdish_sahoo clinic-patrol@jagdish_sahoo'
```

Restart only `clinic-dashboard@…` for a dashboard or report change; the patrol
restart costs a lap and a cold emulator boot, so skip it unless you changed
capture, navigation or patrol code.

Git on the VM must run as `jagdish_sahoo` (the directory's owner) or it refuses
with "dubious ownership".

---

## C. Running the full pipeline locally

Only needed to develop against a live Hik-Connect screen.

### 1. Python 3.12

<https://www.python.org/downloads/> — tick **"Add python.exe to PATH"** in the
installer. 3.9+ works; 3.12 is what this was built and run on.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r clinic_monitor\requirements.txt
```

About **2.5 GB**, mostly torch. On a metered or slow connection, start this
first and read the rest while it runs.

`yolov8n.pt` (~6 MB) downloads itself on first use.

### 2. Android platform-tools (adb)

Frames are captured with `adb exec-out screencap`. There is no other path — no
RTSP, no ONVIF, no camera API.

Download <https://developer.android.com/tools/releases/platform-tools>, unzip
to e.g. `C:\Android\Sdk\platform-tools`, then either add it to PATH or set the
full path in `.env`:

```
CM_ADB_PATH=C:\Android\Sdk\platform-tools\adb.exe
```

Check it:

```bash
adb devices
```

### 3. A device showing Hik-Connect

Either works:

**A physical Android phone** — enable Developer options → USB debugging, plug
it in, accept the RSA prompt. Simplest, and no hypervisor needed.

**An emulator** — install Android Studio (or just the command-line tools plus
the emulator package), create an AVD named `clinic`, and set:

```
CM_EMULATOR_AVD=clinic
CM_EMULATOR_HEADLESS=true
```

The emulator needs hardware acceleration: **WHPX** on Windows (Windows
Features → *Windows Hypervisor Platform*, then reboot). **This is the step most
likely to be blocked on a corporate laptop** — it needs admin rights, and it
conflicts with some endpoint-security tools. Check this before planning around
the emulator; if it is blocked, use a physical phone.

Then install Hik-Connect on the device and **sign in**, and unlock camera
encryption if the cameras ask for it. The app package is detected
automatically, so no configuration is needed for it.

### 4. The Gemini key

```bash
copy clinic_monitor\.env.example clinic_monitor\.env
```

Put a key from <https://aistudio.google.com/apikey> into `GEMINI_API_KEY`, or
copy the `.env` from the old laptop.

Keep `CM_GEMINI_MODEL` on a rolling alias like `gemini-flash-lite-latest`. New
API keys get `429 … limit: 0` on pinned ids such as `gemini-2.0-flash`.

### 5. Check it works

```bash
.venv\Scripts\python.exe clinic_monitor\main.py --list-devices
```

```bash
clinic_monitor\dashboard.bat
```

The dashboard comes up on <http://127.0.0.1:8000>. It reads whatever is in the
local database, so on a fresh machine it will be empty until a patrol runs.

```bash
clinic_monitor\patrol.bat 60
```

One minute per clinic, round after round, until Ctrl+C.

---

## Minimum, if you only take one thing

For managing the deployment — which is most of the work — you need **Git and
the Google Cloud CLI**, the `.env` file, and the SSH key from the old laptop.
That is it. Everything in section C is for local development only, and the
production system will keep running whether or not you ever install it.
