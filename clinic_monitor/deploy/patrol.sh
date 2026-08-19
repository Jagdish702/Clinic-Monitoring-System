#!/usr/bin/env bash
# Continuous patrol - the Linux equivalent of patrol.bat.
#
#   ./patrol.sh              60 seconds per clinic, until Ctrl+C
#   ./patrol.sh 120          two minutes per clinic
#   ./patrol.sh 90 3         90 seconds each, three rounds
#   ./patrol.sh 60 0 --clinics BANAMALIPUR,NAYAHAT
#
# On a server this is normally run by systemd (clinic-patrol.service) rather
# than by hand.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "No Python environment at $PY" >&2
    echo "Create it with:" >&2
    echo "  python3 -m venv $ROOT/.venv" >&2
    echo "  $ROOT/.venv/bin/pip install -r $HERE/requirements.txt" >&2
    exit 1
fi

SECS="${1:-60}"
ROUNDS="${2:-0}"
shift $(( $# > 2 ? 2 : $# )) || true

DEVICE=(--emulator)
EXTRA=()
for arg in "$@"; do
    if [[ "$arg" == "--phone" ]]; then
        DEVICE=()
    else
        EXTRA+=("$arg")
    fi
done

# Start the dashboard unless something already holds the port.
PORT="${CM_DASHBOARD_PORT:-8000}"
if ! (command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":$PORT ") &&
   ! (command -v netstat >/dev/null && netstat -ltn 2>/dev/null | grep -q ":$PORT "); then
    echo "Starting the dashboard on port $PORT..."
    nohup "$PY" "$HERE/dashboard/app.py" >>"$HERE/logs/dashboard.log" 2>&1 &
    sleep 3
else
    echo "Dashboard already running on port $PORT."
fi

echo
echo "============================================================"
echo " Patrolling every clinic - ${SECS}s each"
[[ "$ROUNDS" == "0" ]] && echo " Rounds: unlimited (Ctrl+C to stop)" || echo " Rounds: $ROUNDS"
[[ ${#DEVICE[@]} -gt 0 ]] && echo " Source: Android emulator" || echo " Source: phone over USB"
echo " Dashboard: http://127.0.0.1:$PORT"
echo "============================================================"
echo

cd "$HERE"
exec "$PY" patrol.py --duration "$SECS" --rounds "$ROUNDS" \
    "${DEVICE[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
