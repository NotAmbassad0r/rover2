#!/bin/bash
# Run ROVER2 unit tests on the dev machine (no Pi required).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/pi"

echo "==> ROVER2 unit tests (pi/)"
if python3 -c "import serial" 2>/dev/null; then
  python3 -m unittest discover -s tests -p 'test_*.py' -v
else
  echo "    (pyserial missing — skipping test_safety.py)"
  python3 -m unittest tests.test_follow_nav tests.test_arm_control tests.test_body_tracker tests.test_diagnostics tests.test_config_public tests.test_config_store -v
fi

echo ""
echo "==> All selected tests passed."
