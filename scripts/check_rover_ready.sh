#!/bin/bash
# Preflight checks on the Pi (run from dev machine). Usage: ./scripts/check_rover_ready.sh [host]
set -euo pipefail

HOST="${1:-192.168.70.11}"
API="http://${HOST}:8082"
CAM="http://${HOST}:8081"
FAIL=0

pass() { echo "  OK  $*"; }
fail() { echo "  FAIL $*"; FAIL=1; }

echo "==> ROVER2 preflight: $HOST"
echo ""

echo "==> Ping"
if ping -c1 -W3 "$HOST" >/dev/null 2>&1; then
  pass "ping $HOST"
else
  fail "ping $HOST"
fi

echo ""
echo "==> HTTP"
if curl -sf --max-time 5 "${CAM}/health" | grep -q ok; then
  pass "rover-camera ${CAM}/health"
else
  fail "rover-camera ${CAM}/health"
fi

STATUS_JSON="$(curl -sf --max-time 5 "${API}/api/status" 2>/dev/null || true)"
if [ -n "$STATUS_JSON" ]; then
  pass "rover2-api ${API}/api/status"
  python3 - "$STATUS_JSON" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
checks = [
    ("serial_connected", True),
    ("tracking_available", True),
    ("tracking_hailo_ready", True),
]
for key, want in checks:
    val = d.get(key)
    ok = val == want
    mark = "OK " if ok else "FAIL"
    print(f"  {mark} {key}={val!r} (want {want!r})")
    if not ok:
        sys.exit(1)
PY
  if [ $? -ne 0 ]; then FAIL=1; fi
else
  fail "rover2-api ${API}/api/status"
fi

echo ""
echo "==> SSH services"
if ssh -o ConnectTimeout=5 -o BatchMode=yes "ambassad0r@${HOST}" \
  'systemctl is-active rover2-api rover-camera >/dev/null && echo ok' 2>/dev/null | grep -q ok; then
  pass "systemd rover2-api + rover-camera"
else
  fail "systemd rover2-api or rover-camera"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "==> Preflight PASSED — run manual tests from docs/TEST_PLAN.md (Today T1–T3)"
  echo "    UI: http://${HOST}:8082/"
  exit 0
else
  echo "==> Preflight FAILED — fix above before manual tests"
  exit 1
fi
