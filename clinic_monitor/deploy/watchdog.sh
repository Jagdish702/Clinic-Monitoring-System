#!/usr/bin/env bash
# Notice when the patrol has stopped producing data, and restart it.
#
# Run from cron every 15 minutes:
#   */15 * * * * /opt/clinic-monitoring/clinic_monitor/deploy/watchdog.sh
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
STALE_MINUTES="${CM_WATCHDOG_STALE_MINUTES:-45}"
LOG="$HERE/logs/watchdog.log"

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

if systemctl is-enabled --quiet "clinic-patrol@$USER" 2>/dev/null; then
    sudo systemctl restart "clinic-patrol@$USER"
    log "restarted clinic-patrol@$USER"
else
    log "clinic-patrol service not installed - restart the patrol by hand"
fi
