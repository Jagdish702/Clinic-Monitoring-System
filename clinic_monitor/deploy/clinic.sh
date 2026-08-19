#!/usr/bin/env bash
# One entry point for the other tools, so a server install has a single
# command to remember.
#
#   ./clinic.sh ask "CUREBAY BANAMALIPUR" 60
#   ./clinic.sh run check curebay banamalipur for 1 min
#   ./clinic.sh report                     today, every clinic patrolled
#   ./clinic.sh report 2026-08-19
#   ./clinic.sh dashboard                  serve it in the foreground
#   ./clinic.sh clinics                    list every device in the app
#   ./clinic.sh emulator --tune            emulator maintenance
#   ./clinic.sh selftest                   offline checks, no phone needed
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "No Python environment at $PY - see deploy/DEPLOY_GCP.md" >&2
    exit 1
fi

cd "$HERE"
cmd="${1:-help}"
shift || true

case "$cmd" in
    ask)
        name="${1:?usage: clinic.sh ask \"CLINIC NAME\" [seconds]}"
        secs="${2:-60}"
        exec "$PY" ask.py --open "$name" --duration "$secs" \
            --question "what is happening there right now?"
        ;;
    run)       exec "$PY" run.py "$@" ;;
    report)    [[ $# -gt 0 ]] && exec "$PY" report.py --day "$1" || exec "$PY" report.py ;;
    dashboard) exec "$PY" dashboard/app.py ;;
    clinics)   exec "$PY" ask.py --list-clinics ;;
    emulator)  exec "$PY" -m control.emulator "$@" ;;
    selftest)  exec "$PY" tools/selftest.py ;;
    *)
        sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
