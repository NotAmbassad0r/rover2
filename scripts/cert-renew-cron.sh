#!/bin/bash
# Monthly cron wrapper for rover2 certificate renewal.
# Skips silently when cert has >60 days remaining.
# Run: 0 9 1 * * /home/ambassad0r/Documents/projects/rover2/scripts/cert-renew-cron.sh
#
# Logs to ~/.rover2/cert-renew.log (created if absent).

REPO_DIR="/home/ambassad0r/Documents/projects/rover2"
LOG_DIR="${HOME}/.rover2"
LOG_FILE="${LOG_DIR}/cert-renew.log"

mkdir -p "$LOG_DIR"

echo "$(date -Iseconds) [cert-renew] Monthly check starting..." >> "$LOG_FILE"

if RENEW_DAYS_THRESHOLD=60 bash "${REPO_DIR}/scripts/renew-cert.sh" >> "$LOG_FILE" 2>&1; then
    echo "$(date -Iseconds) [cert-renew] Done (exit 0)" >> "$LOG_FILE"
else
    EXIT=$?
    echo "$(date -Iseconds) [cert-renew] FAILED (exit ${EXIT}) — check log above" >> "$LOG_FILE"
fi

# Trim log to last 500 lines so it doesn't grow unbounded
tail -500 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
