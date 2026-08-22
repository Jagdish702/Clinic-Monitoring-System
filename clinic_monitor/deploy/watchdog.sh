#!/usr/bin/env bash
# Notice when the patrol has stopped producing data, and restart it.
#
# Installed as /etc/cron.d/clinic-watchdog, running as root every 15 minutes:
#   */15 * * * * root /opt/clinic-monitoring/clinic_monitor/deploy/watchdog.sh
#
# Why this exists: on a server nobody is watching the screen. The patrol
# recovers from most faults itself, but an emulator can wedge in ways it
# cannot - a hung app dialog it fails to dismiss, a stream that never
# reconnects. The honest signal is not "is the process alive" (it usually is)
# but "has anything been recorded lately".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/.venv/bin/python"
DB="$HERE/storage/events.db"
LOG="$HERE/logs/watchdog.log"
STALE_MINUTES="${CM_WATCHDOG_STALE_MINUTES:-45}"

# The systemd units are templated on the username. Deriving it from the owner
# of the install is deliberate: cron sets no $USER, so building the unit name
# from $USER produced "clinic-patrol@" and the restart silently never
# happened - a watchdog that never fires is worse than none, because it looks
# like coverage.
SVC_USER="${CM_SERVICE_USER:-$(stat -c %U "$HERE")}"
# root's cron also has none of the emulator environment.
export CM_ADB_PATH="${CM_ADB_PATH:-/home/$SVC_USER/android-sdk/platform-tools/adb}"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

if [[ ! -f "$DB" ]]; then
    log "no database yet - nothing to check"
    exit 0
fi

age_minutes="$("$PY" - "$DB" <<'PY'
import sqlite3, sys, time
try:
    conn = sqlite3.connect(sys.argv[1])
    row = conn.execute("SELECT MAX(ts_epoch) FROM observations").fetchone()
    print(9999 if not row or not row[0] else int((time.time() - row[0]) / 60))
except Exception:
    print(9999)
PY
)"

if (( age_minutes <= STALE_MINUTES )); then
    exit 0
fi

log "no observation for ${age_minutes} min (limit ${STALE_MINUTES}) - restarting"

# Kill the emulator first: restarting the patrol alone would reuse a wedged
# one, which is usually the thing that is stuck.
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
    log "restarted clinic-patrol@$SVC_USER"
else
    log "clinic-patrol@$SVC_USER is not installed - restart the patrol by hand"
fi
