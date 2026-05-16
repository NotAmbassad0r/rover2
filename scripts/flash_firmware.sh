#!/bin/bash
# Compile ROVER2 basic firmware on central-computer and flash via Pi.
# Usage: ./scripts/flash_firmware.sh [pi_host]
set -euo pipefail

PI_HOST="${1:-${ROVER2_PI_HOST:-192.168.70.11}}"
SKETCH="${ROVER2_SKETCH:-$(cd "$(dirname "$0")/.." && pwd)/firmware/rover2_basic}"
BUILD="/tmp/rover2_fw_build"
HEX="${BUILD}/rover2_basic.ino.hex"
ARDUINO_CLI="${ARDUINO_CLI:-$HOME/.local/bin/arduino-cli}"

echo "==> Compile (central-computer)"
"$ARDUINO_CLI" compile --fqbn arduino:avr:mega --build-path "$BUILD" "$SKETCH"

echo "==> Copy hex to Pi"
scp "$HEX" "ambassad0r@${PI_HOST}:/tmp/rover2_firmware.hex"

echo "==> Flash on Pi (stops rover2-api)"
ssh "ambassad0r@${PI_HOST}" \
  "sudo systemctl stop rover2-api.service 2>/dev/null || true; \
   LD_LIBRARY_PATH=/opt/rover2/tools /opt/rover2/tools/avrdude \
     -C/opt/rover2/tools/avrdude.conf -v -p atmega2560 -c wiring \
     -P /dev/ttyUSB0 -b 115200 -D -U flash:w:/tmp/rover2_firmware.hex:i && \
   sudo systemctl start rover2-api.service"

echo "==> Done. Check logs:"
ssh "ambassad0r@${PI_HOST}" "sleep 3; journalctl -u rover2-api -n 15 --no-pager"
