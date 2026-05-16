#!/bin/bash
# Flash /tmp/rover2_firmware.hex on the Pi. Invoked in background from POST /api/firmware/flash.
set -euo pipefail

HEX="${ROVER2_HEX:-/tmp/rover2_firmware.hex}"
PORT="${ROVER2_SERIAL:-/dev/ttyUSB0}"
TOOLS="/opt/rover2/tools"
SERVICE="rover2-api.service"
LOG="/tmp/rover2_flash.log"

exec >>"$LOG" 2>&1
echo "=== flash start $(date -Is) ==="

sleep 2

if [[ ! -f "$HEX" ]]; then
  echo "ERROR: hex missing at $HEX"
  exit 1
fi

if [[ ! -x "$TOOLS/avrdude" ]]; then
  echo "ERROR: avrdude not installed — run scripts/install_avrdude_on_pi.sh"
  exit 1
fi

sudo systemctl stop "$SERVICE" || true
export LD_LIBRARY_PATH="$TOOLS:${LD_LIBRARY_PATH:-}"

"$TOOLS/avrdude" -C"$TOOLS/avrdude.conf" -v -p atmega2560 -c wiring \
  -P "$PORT" -b 115200 -D -U "flash:w:${HEX}:i"

sudo systemctl start "$SERVICE" || true
echo "=== flash done $(date -Is) ==="
