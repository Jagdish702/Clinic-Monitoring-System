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
# day at 03:15 server time (low patrol traffic, offset from logrotate and the
# other stock cron jobs) - see DEPLOY_GCP.md section 9:
#   15 3 * * * root /opt/clinic-monitoring/clinic_monitor/deploy/periodic_restart.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$HERE/logs/periodic_restart.log"

# Cron's root crontab sets no $USER, and hardcoding a name breaks silently the
# day the service account changes - see the identical derivation, and the
# reason for it, in watchdog.sh.
SVC_USER="${CM_SERVICE_USER:-$(stat -c %U "$HERE")}"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

if systemctl is-enabled --quiet "clinic-patrol@$SVC_USER" 2>/dev/null; then
    systemctl restart "clinic-patrol@$SVC_USER"
    log "restarted clinic-patrol@$SVC_USER (scheduled, not health-triggered)"
else
    log "clinic-patrol@$SVC_USER is not installed - nothing to restart"
fi
