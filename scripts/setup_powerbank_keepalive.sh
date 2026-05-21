#!/bin/bash
# Install power-bank keepalive timer + disable Pi sleep (run on the Pi with sudo).
set -euo pipefail

ROOT="${ROVER2_ROOT:-/opt/rover2}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_SYSTEMD="$(cd "$SCRIPT_DIR/../systemd" && pwd)"

install -d "$ROOT/scripts"
install -m 0755 "$SCRIPT_DIR/powerbank_keepalive.sh" "$ROOT/scripts/powerbank_keepalive.sh"

for unit in rover2-powerbank-keepalive.service rover2-powerbank-keepalive.timer; do
  install -m 0644 "$REPO_SYSTEMD/$unit" "/etc/systemd/system/$unit"
done
if [ -f "$REPO_SYSTEMD/rover2-powerbank.env" ]; then
  install -m 0644 "$REPO_SYSTEMD/rover2-powerbank.env" /etc/default/rover2-powerbank
fi

systemctl daemon-reload

# Pi must not suspend on battery
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target 2>/dev/null || true

echo "Installed. Enable on battery with:"
echo "  sudo systemctl enable --now rover2-powerbank-keepalive.timer"
echo "Disable on wall power with:"
echo "  sudo systemctl disable --now rover2-powerbank-keepalive.timer"
