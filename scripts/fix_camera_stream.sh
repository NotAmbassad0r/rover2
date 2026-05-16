#!/bin/bash
# One-shot: deploy camera proxy + httpx and restart ROVER2 API.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ROVER2_PI_HOST="${ROVER2_PI_HOST:-192.168.70.11}"
exec "$ROOT/deploy_pi.sh"
