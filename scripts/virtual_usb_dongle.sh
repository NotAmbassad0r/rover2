#!/bin/bash
# Emulate a ~50–100 mA USB keep-alive dongle by holding a small constant CPU load.
# All extra power is drawn through the Pi’s USB-C cable (same port as today).
set -euo pipefail

CPU_LOAD="${VIRTUAL_DONGLE_CPU_LOAD:-10}"
CPU_WORKERS="${VIRTUAL_DONGLE_WORKERS:-1}"

if ! command -v stress-ng >/dev/null 2>&1; then
  echo "virtual_usb_dongle: stress-ng missing — install stress-ng on the Pi" >&2
  exec bash -c 'while true; do :; done'
fi

exec stress-ng \
  --cpu "$CPU_WORKERS" \
  --cpu-load "$CPU_LOAD" \
  --cpu-load-slice 50 \
  --timeout 0 \
  --quiet
