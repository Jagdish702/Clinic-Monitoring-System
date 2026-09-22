#!/usr/bin/env bash
# Restart the patrol on a fixed schedule, regardless of health, so the
# headless Android emulator never gets old enough to repeat what it did on
# 2026-08-29: qemu-system-x86 grew to ~15GB resident after about two weeks of
# unbroken uptime, and the kernel's OOM killer took it out - which wedged the
# whole VM (SSH and the dashboard both stopped answering) until a manual
# `gcloud compute instances reset`.
#
# This is deliberately unconditional. watchdog.sh in this same directory only
# restarts on a *symptom* - no fresh observation in 45 minutes - so it never
# fires while the emulator is still working but slowly leaking memory. This
# restarts on the calendar instead, well before any single run's uptime gets
# anywhere near the two weeks it took to repeat that.
#
# Installed as /etc/cron.d/clinic-periodic-restart, running as root once a
# day at 03:00 IST (Asia/Kolkata - the VM's own system timezone, confirmed
# with timedatectl) - see DEPLOY_GCP.md section 9:
#   0 3 * * * root /opt/clinic-monitoring/clinic_monitor/deploy/periodic_restart.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HERE/.venv/bin/python"
LOG="$HERE/logs/periodic_restart.log"

# Cron's root crontab sets no $USER, and hardcoding a name breaks silently the
# day the service account changes - see the identical derivation, and the
# reason for it, in watchdog.sh.
SVC_USER="${CM_SERVICE_USER:-$(stat -c %U "$HERE")}"
# root's cron also has none of the emulator environment.
export CM_ADB_PATH="${CM_ADB_PATH:-/home/$SVC_USER/android-sdk/platform-tools/adb}"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

# Kill the emulator first: control/emulator.py's ensure_running() reuses
# whatever is already up rather than booting a second copy (a real feature -
# it makes an ordinary patrol restart, e.g. after a crash, fast instead of
# paying a cold boot every time) - so "systemctl restart clinic-patrol" alone
# reattaches to the very same long-lived emulator this script exists to age
# out, defeating the whole point. Mirrors watchdog.sh's identical fix for the
# identical reason.
"$PY" -c "
import sys; sys.path.insert(0, '$HERE')
from control import emulator
s = emulator.running_serial()
if s:
    emulator._adb('-s', s, 'emu', 'kill')
    print('killed', s)
" >>"$LOG" 2>&1 || true

sleep 10
pkill -f "emulator.*-avd" 2>/dev/null || true
sleep 5

if systemctl is-enabled --quiet "clinic-patrol@$SVC_USER" 2>/dev/null; then
    systemctl restart "clinic-patrol@$SVC_USER"
    log "restarted clinic-patrol@$SVC_USER (scheduled, not health-triggered)"
else
    log "clinic-patrol@$SVC_USER is not installed - nothing to restart"
fi
