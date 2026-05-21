#!/bin/bash
# Keep the Pi running on battery (Viking PN-964PD) during normal untethered days.
# Run on the Pi: sudo bash /opt/rover2/scripts/setup_normal_day_power.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${ROVER2_ROOT:-/opt/rover2}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [ -f "$SCRIPT_DIR/setup_virtual_usb_dongle.sh" ]; then
  bash "$SCRIPT_DIR/setup_virtual_usb_dongle.sh"
fi

systemctl enable --now rover2-virtual-usb-dongle.service
systemctl disable --now rover2-powerbank-keepalive.timer 2>/dev/null || true
systemctl disable --now stress-ng.service 2>/dev/null || true

systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target 2>/dev/null || true

install -d /etc/logind.conf.d
cat > /etc/logind.conf.d/rover2-no-suspend.conf <<'EOF'
[Login]
IdleAction=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
EOF

if command -v nmcli >/dev/null 2>&1; then
  nmcli dev set wlan0 powersave 2 2>/dev/null || true
fi

# Slightly stronger default for Viking MCU (override in /etc/default/rover2-virtual-usb-dongle)
if [ -f /etc/default/rover2-virtual-usb-dongle ]; then
  if ! grep -q '^VIRTUAL_DONGLE_CPU_LOAD=' /etc/default/rover2-virtual-usb-dongle; then
    echo 'VIRTUAL_DONGLE_CPU_LOAD=12' >> /etc/default/rover2-virtual-usb-dongle
  fi
fi
systemctl restart rover2-virtual-usb-dongle.service 2>/dev/null || true

systemctl enable rover2-api.service 2>/dev/null || true
systemctl enable rover-camera.service 2>/dev/null || true

echo ""
echo "Normal-day power profile applied."
echo "  - virtual USB keep-alive: $(systemctl is-active rover2-virtual-usb-dongle.service 2>/dev/null || echo '?')"
echo "  - Pi suspend: masked"
echo ""
echo "You must still double-tap the Viking ON/OFF after plugging USB-C (bank manual)."
echo "Disable for wall-power lab: sudo systemctl disable --now rover2-virtual-usb-dongle.service"
