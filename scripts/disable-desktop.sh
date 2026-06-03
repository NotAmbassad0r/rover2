#!/bin/bash
# Disable the desktop GUI on the Pi — one-time, idempotent.
# The Pi is a headless robot; the full desktop wastes ~300 MB RAM.
# Run via deploy_pi.sh or manually: sudo bash /opt/rover2/scripts/disable-desktop.sh
set -euo pipefail

CURRENT=$(systemctl get-default 2>/dev/null || echo "unknown")
if [ "$CURRENT" = "multi-user.target" ]; then
  echo "==> Already on multi-user.target — nothing to do."
  exit 0
fi

echo "==> Setting default runlevel to multi-user.target (was: $CURRENT)..."
sudo systemctl set-default multi-user.target

echo "==> Disabling desktop services..."
sudo systemctl disable labwc wf-panel-pi pcmanfm wireplumber \
  xdg-desktop-portal-wlr xdg-desktop-portal-gtk 2>/dev/null || true

echo "==> Desktop disabled. Reboot to recover ~300 MB RAM."
