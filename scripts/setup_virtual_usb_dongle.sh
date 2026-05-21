#!/bin/bash
# Install virtual USB dongle service (run on Pi with sudo).
set -euo pipefail

ROOT="${ROVER2_ROOT:-/opt/rover2}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_SYSTEMD="$(cd "$SCRIPT_DIR/../systemd" && pwd)"

install -d "$ROOT/scripts"
install -m 0755 "$SCRIPT_DIR/virtual_usb_dongle.sh" "$ROOT/scripts/virtual_usb_dongle.sh"
install -m 0644 "$REPO_SYSTEMD/rover2-virtual-usb-dongle.service" \
  /etc/systemd/system/rover2-virtual-usb-dongle.service
if [ -f "$REPO_SYSTEMD/rover2-virtual-usb-dongle.env" ]; then
  install -m 0644 "$REPO_SYSTEMD/rover2-virtual-usb-dongle.env" \
    /etc/default/rover2-virtual-usb-dongle
fi

systemctl daemon-reload
systemctl disable --now rover2-powerbank-keepalive.timer 2>/dev/null || true

echo "Enable (battery / untethered):"
echo "  sudo systemctl enable --now rover2-virtual-usb-dongle.service"
echo "Disable (wall power / eth lab):"
echo "  sudo systemctl disable --now rover2-virtual-usb-dongle.service"
