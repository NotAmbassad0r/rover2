#!/bin/bash
# Run restore_wifi_from_boot.sh only when wlan0 is not connected.
set -euo pipefail

if command -v nmcli >/dev/null 2>&1; then
  state="$(nmcli -t -f STATE dev show wlan0 2>/dev/null || echo "")"
  if [ "$state" = "connected" ]; then
    exit 0
  fi
fi

if [ ! -f /boot/firmware/network-config ]; then
  exit 0
fi

exec /opt/rover2/scripts/restore_wifi_from_boot.sh
