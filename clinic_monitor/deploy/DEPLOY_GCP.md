# Deploying to Google Cloud (Option B - everything on a GCE VM)

The whole system runs on one Compute Engine VM: a headless Android emulator
running Hik-Connect, the capture pipeline, the dashboard and the reports. No
phone, no cable, no PC in the office.

**What makes this possible:** Hik-Connect is a cloud service - the app pulls
streams over the internet, so it does not need to be near the cameras. Any
machine that can run Android and reach the internet can be the viewer.

---

## Before you start

**This is the expensive option.** The emulator needs nested virtualisation,
which rules out cheap machine types. Expect **roughly $100-160/month** for an
n2-standard-4 running continuously - check Google's pricing calculator for
`asia-south1` (Mumbai), the closest region to Odisha. The hybrid alternative
(capture stays on a mini-PC, only the dashboard in cloud) is about a tenth of
that. This guide assumes you have decided the tradeoff is worth it.

**Settle the compliance question first.** This uploads footage of patients and
staff off-premises. The dashboard has no authentication of its own; §7 keeps it
bound to localhost for that reason. India's DPDP Act very likely applies to
CureBay here - get a decision from whoever owns compliance before the first
frame leaves the building.

---

## 1. Create the VM

Nested virtualisation is required, and only some machine families support it -
N2, N2D, C2, C3. **Not E2, and not the ARM T2A.**

```bash
gcloud compute instances create clinic-monitor \
    --zone=asia-south1-a \
    --machine-type=n2-standard-4 \
    --enable-nested-virtualization \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=100GB \
    --boot-disk-type=pd-balanced
```

Sizing comes from what the system actually uses: the AVD alone is configured
for 4 GB, plus YOLO, plus the OS. 4 vCPU is the realistic floor - the emulator
hangs under load, and a hung app is the main failure mode (§10).

Do **not** give it an external IP you then open up. Reach it over SSH only.

---

## 2. Get the code onto it

```bash
gcloud compute ssh clinic-monitor --zone=asia-south1-a

sudo mkdir -p /opt/clinic-monitoring && sudo chown "$USER" /opt/clinic-monitoring
git clone https://github.com/Jagdish702/Clinic-Monitoring-System.git \
    /opt/clinic-monitoring
cd /opt/clinic-monitoring/clinic_monitor
chmod +x deploy/*.sh
```

---

## 3. Install everything

```bash
./deploy/setup_vm.sh
```

It checks for VMX/SVM and stops with an explanation if nested virtualisation is
missing, then installs KVM, the Android SDK, an `android-31` x86_64 system
image, creates an AVD called `clinic`, tunes it to 4096 MB RAM / 512 MB heap,
and builds the Python environment - using the **CPU-only** torch index, because
PyPI's Linux wheels bundle CUDA and would pull ~2.5 GB for nothing.

Open a new shell afterwards so the environment variables take effect.

---

## 4. Configure

```bash
cp .env.example .env
nano .env          # set GEMINI_API_KEY
```

`CM_EMULATOR_HEADLESS` already defaults to true, which is what you want here.

---

## 5. Install Hik-Connect and sign in

**This is the one step that needs a human.** Nobody can type your password for
you, and it cannot be automated.

### Get the APK

There is no Play Store on a plain system image, so sideload it. Pull it from a
device that already has it - verified on the emulator here, a single 283 MB
`base.apk` with no split parts:

```bash
# on the machine that already has the app
adb shell pm path com.hikvision.hikconnect
adb pull /data/app/.../base.apk hikconnect.apk

# copy it up and install
gcloud compute scp hikconnect.apk clinic-monitor:~ --zone=asia-south1-a
adb install ~/hikconnect.apk
```

### Sign in

Start the emulator, then drive its screen from your own laptop over an SSH
tunnel to the VM's adb server:

```bash
# on the VM
~/android-sdk/emulator/emulator -avd clinic -no-window &

# on your laptop
ssh -L 5037:localhost:5037 -N user@<vm-ip> &
adb devices          # now lists the VM's emulator
scrcpy               # an interactive window onto it
```

Sign in through that window, open a clinic once to confirm streams play, then
close scrcpy. The session persists in the AVD.

> **Alternative:** copy the whole AVD from a working machine
> (`~/.android/avd/`), which carries the signed-in session with it. It is
> ~17 GB nominal here (mostly sparse), so the transfer is awkward - fine over a
> fast link, painful otherwise.

---

## 6. Verify before automating

```bash
./deploy/clinic.sh selftest     # offline checks, no device needed
./deploy/clinic.sh clinics      # should list all 13 clinics
./deploy/clinic.sh ask "CUREBAY BANAMALIPUR" 60
```

If `clinics` lists your sites, everything below is just scheduling.

---

## 7. Install the services

```bash
sudo cp deploy/clinic-patrol@.service deploy/clinic-dashboard@.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now "clinic-patrol@$USER" "clinic-dashboard@$USER"
systemctl status "clinic-patrol@$USER"
```

The units are templated on the username, so the paths to the SDK in that
user's home directory resolve. **The `@` in the filename is required** - a unit
using `%i` but named without it cannot be instantiated at all, and
`systemctl enable clinic-patrol@someone` fails with "No such file or
directory".

If the emulator was set up under a different account than the one the service
runs as (easy to do: the Cloud Console's SSH button logs in as a different
Linux user than `gcloud compute ssh`), make sure that account owns the install
and is in the `kvm` group:

```bash
sudo chown -R "$USER" /opt/clinic-monitoring
sudo adduser "$USER" kvm
```

### Reaching the dashboard safely

It binds to **127.0.0.1 deliberately**. It has no login of its own and shows
clinic footage, so it must never be exposed directly. Tunnel to it:

```bash
gcloud compute ssh clinic-monitor --zone=asia-south1-a --ssh-flag="-L 8000:localhost:8000"
# then open http://localhost:8000 on your laptop
```

(`--ssh-flag` rather than a bare `-- -L ...`: gcloud on Windows shells out to
PuTTY/plink, not OpenSSH, and plink does not accept `-L` the way `-- -L`
assumes - `--ssh-flag` is the one form that works on both backends.)

For team access, put an authenticating proxy in front - Identity-Aware Proxy is
the native option - rather than opening the port.

---

## 8. Add a second cluster (the collector)

One dashboard can show more than one cluster (a second Hik-Connect
account/emulator on its own VM - e.g. a Bhubaneswar cluster alongside this
Puri one) without a shared network database. Every VM keeps writing to its
own local SQLite database exactly as it does today; it additionally
best-effort pushes a copy of every write to one central collector, which is
just this VM's own dashboard process listening on a second port.

```bash
# on THIS VM (the one already running the dashboard) - generate a token,
# add it to .env, then install the collector service
python3 -c "import secrets; print(secrets.token_hex(32))"   # -> CM_COLLECTOR_TOKEN

cat >> .env <<'EOF'
CM_COLLECTOR_TOKEN=<paste the token above>
EOF

sudo cp deploy/clinic-collector@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now "clinic-collector@$USER"
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8001/collect/health
```

It binds `0.0.0.0:8001` deliberately - unlike the dashboard, it must be
reachable from other VMs on the internal network, so it carries its own
bearer-token check on every request instead of hiding behind localhost. Do
**not** open port 8001 to the internet; it only needs to be reachable from
other VMs inside this project's VPC, which the default internal firewall
rule already covers.

On every *other* cluster's VM, set in `.env` (leave this VM's own `.env`
alone - it already writes straight into the database the collector shares):

```bash
CM_STATE_NAME=Odisha            # or whichever state that cluster is in
CM_CLUSTER_NAME=Bhubaneswar     # must match clusters.json exactly
CM_COLLECTOR_URL=http://<this VM's internal IP>:8001
CM_COLLECTOR_TOKEN=<same token as above>
```

Then restart that VM's `clinic-patrol` service. Its events start appearing
on this dashboard within one patrol round - no dashboard restart needed.

---

## 9. Keep it honest with the watchdog

```bash
sudo tee /etc/cron.d/clinic-watchdog >/dev/null <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/15 * * * * root /opt/clinic-monitoring/clinic_monitor/deploy/watchdog.sh
EOF
sudo chmod 644 /etc/cron.d/clinic-watchdog
```

Run as **root** via `/etc/cron.d` rather than a user crontab: it needs to
restart a systemd unit, and this keeps it working regardless of whether the
service user has sudo.

Verify it actually fires - a watchdog you have not tested is an assumption:

```bash
sudo CM_WATCHDOG_STALE_MINUTES=0 bash deploy/watchdog.sh   # forces a restart
sudo tail -5 clinic_monitor/logs/watchdog.log
```

It does not ask "is the process alive" - it usually is. It asks **"has anything
been recorded lately"**, and if nothing has for 45 minutes it kills the
emulator and restarts the patrol. That is the failure that actually happens:
everything running, nothing being captured.

---

## 10. Keep the emulator young with a periodic restart

```bash
sudo tee /etc/cron.d/clinic-periodic-restart >/dev/null <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 3 * * * root /opt/clinic-monitoring/clinic_monitor/deploy/periodic_restart.sh
EOF
sudo chmod 644 /etc/cron.d/clinic-periodic-restart
```

The watchdog above only reacts once the patrol has already stopped producing
data - it never fires while the emulator is still working but slowly leaking
memory. This restarts on the calendar instead, unconditionally, once a day at
03:00 IST (`Asia/Kolkata` - confirmed with `timedatectl` as the VM's own
system timezone): `qemu-system-x86` reached ~15GB resident and got OOM-killed
by the kernel on 2026-08-29 after about two weeks of unbroken uptime, which
wedged the whole VM (SSH and the dashboard both stopped answering) until a
manual `gcloud compute instances reset`. A daily recycle keeps any single run
nowhere near old enough for that to happen again.

Verify it once by hand rather than trusting the crontab syntax:

```bash
sudo /opt/clinic-monitoring/clinic_monitor/deploy/periodic_restart.sh
sudo tail -5 clinic_monitor/logs/periodic_restart.log
sudo systemctl status "clinic-patrol@$USER" --no-pager   # Active since just now
```

---

## 11. What will go wrong

Honest list, from what we have already hit in practice:

| Failure | Handling |
| --- | --- |
| App hangs on cold boot, ANR dialog blocks the screen | The navigator detects and dismisses it automatically. Seen twice here. |
| Emulator wedges beyond recovery | Watchdog restarts it; systemd restarts the patrol |
| Emulator memory grows unbounded over very long uptime, OOM-kills the VM itself (hit once, 2026-08-29 - see §9) | Daily periodic restart recycles the emulator well before any run gets that old |
| Streams time out mid-patrol | Clinic is skipped, retried next round |
| Gemini rate limits | Falls back to local descriptions, recovers on its own |
| **Hik-Connect signs the emulator out** | **No automatic recovery.** The system goes blind until someone signs in again through §5. Watch for it. |
| Hik-Connect app update changes resource ids | Navigation fails loudly, not silently. Needs a code change. |

The sign-out risk is the one to plan for: everything else self-heals, that one
needs a person.

---

## 12. Running costs

| Item | Approximate |
| --- | --- |
| n2-standard-4, 24/7 | $100-160/month |
| 100 GB pd-balanced | ~$10/month |
| Egress | negligible - video is *inbound*, only small Gemini calls go out |
| Gemini API | free tier today; a paid tier removes the throttling |

A 1-year committed-use discount takes roughly 30% off the VM. Do **not** use
spot/preemptible instances - eviction mid-patrol defeats the purpose.

---

## 13. Updating

```bash
cd /opt/clinic-monitoring && git pull
sudo systemctl restart "clinic-patrol@$USER" "clinic-dashboard@$USER"
```

Data lives in `clinic_monitor/storage/events.db`, evidence in `screenshots/`,
reports in `reports/` - none of which git touches. Back up the database and
screenshots directory; everything else is reproducible from the repository.
