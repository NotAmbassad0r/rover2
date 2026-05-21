#!/bin/bash
# Save WiFi on the Pi (NetworkManager). Run ON THE PI or via eth0 SSH.
#
#   sudo WIFI_SSID='YourSSID' WIFI_PASSWORD='your-pass' bash /opt/rover2/scripts/setup_wifi.sh
#
set -euo pipefail

SSID="${WIFI_SSID:-}"
PASSWORD="${WIFI_PASSWORD:-}"
CON_NAME="${WIFI_CONNECTION_NAME:-rover-wifi}"
IFACE="${WIFI_IFACE:-wlan0}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

if [ -z "$SSID" ]; then
  echo "Scanning nearby networks..." >&2
  nmcli device wifi rescan 2>/dev/null || true
  sleep 2
  nmcli -f IN-USE,SSID,SIGNAL,SECURITY device wifi list | head -20
  echo "" >&2
  echo "Set WIFI_SSID and WIFI_PASSWORD, then re-run." >&2
  exit 1
fi

nmcli radio wifi on
nmcli device set "$IFACE" managed yes 2>/dev/null || true

if [ -n "$PASSWORD" ]; then
  nmcli device wifi connect "$SSID" password "$PASSWORD" name "$CON_NAME" ifname "$IFACE"
else
  nmcli device wifi connect "$SSID" name "$CON_NAME" ifname "$IFACE"
fi

nmcli connection modify "$CON_NAME" \
  connection.autoconnect yes \
  connection.autoconnect-priority 20 \
  ipv4.route-metric 600 \
  ipv6.route-metric 600

# Prefer eth0 when plugged (matches HANDOFF: eth metric 100, WiFi 600)
if nmcli -t -f NAME connection show | grep -q '^Wired connection 1$'; then
  nmcli connection modify 'Wired connection 1' ipv4.route-metric 100 ipv6.route-metric 100
fi

# Reduce WiFi dropouts on Pi
if nmcli -e wifi.powersave connection modify "$CON_NAME" wifi.powersave 2 2>/dev/null; then
  :
else
  iw dev "$IFACE" set power_save off 2>/dev/null || true
fi

echo ""
echo "WiFi connected:"
ip -4 -br addr show "$IFACE"
nmcli -f GENERAL.STATE,IP4.ADDRESS dev show "$IFACE"
