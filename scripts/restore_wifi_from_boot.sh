#!/bin/bash
# Re-apply WiFi from Raspberry Pi Imager /boot/firmware/network-config (GUCZ-744 etc.).
# Use when wlan0 shows NO-CARRIER but WiFi worked before.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [ ! -f /boot/firmware/network-config ]; then
  echo "Missing /boot/firmware/network-config" >&2
  exit 1
fi

install -m 600 /boot/firmware/network-config /etc/netplan/50-rover-wifi.yaml
netplan apply
sleep 3
ip -4 -br addr show wlan0
nmcli -t -f DEVICE,STATE,CONNECTION dev status | grep wlan0 || true
