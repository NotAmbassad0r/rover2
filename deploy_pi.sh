#!/bin/bash
# Deploy ROVER2 Pi code to the robot. Run on central-computer.
set -euo pipefail

# Override: ROVER2_PI_HOST=rover ./deploy_pi.sh
# If unset, pick the first reachable SSH target.
PI_HOST="${ROVER2_PI_HOST:-${PI_HOST:-}}"
if [ -z "$PI_HOST" ]; then
  for candidate in rover-eth 192.168.70.11 rover 192.168.250.254 rover-ts; do
    if ssh -o ConnectTimeout=3 -o BatchMode=yes "$candidate" 'true' 2>/dev/null; then
      PI_HOST="$candidate"
      echo "==> Using reachable host: $PI_HOST"
      break
    fi
  done
fi
PI_HOST="${PI_HOST:-192.168.70.11}"
PI_PATH="/opt/rover2/"
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOCAL_PATH="$ROOT/pi/"
SCRIPTS_PATH="$ROOT/scripts/"

echo "==> Syncing $LOCAL_PATH to $PI_HOST:$PI_PATH"
rsync -avz --progress --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'venv/' \
  --exclude 'tools/' \
  --exclude 'scripts/' \
  "$LOCAL_PATH" "$PI_HOST:$PI_PATH"

echo "==> Syncing scripts to $PI_HOST:/opt/rover2/scripts/"
ssh "$PI_HOST" "mkdir -p /opt/rover2/scripts"
rsync -avz "$SCRIPTS_PATH" "$PI_HOST:/opt/rover2/scripts/"
ssh "$PI_HOST" "chmod +x /opt/rover2/scripts/*.sh 2>/dev/null || true"

echo ""
echo "==> Installing Python deps (if venv exists)..."
ssh "$PI_HOST" 'if [ -d /opt/rover2/venv ]; then
  /opt/rover2/venv/bin/pip install -r /opt/rover2/requirements.txt || true
  cd /opt/rover2 && /opt/rover2/venv/bin/python3 -c "import megapi, arm_control, ws_control"
fi'

# httpx: TCP/443 to PyPI may be blocked by the lab gateway (ICMP passes, TCP does not).
# If pip failed to install httpx, sideload it from the dev machine via scp.
echo ""
echo "==> Checking httpx (sideload from dev machine if pip couldn't install it)..."
if ! ssh "$PI_HOST" "/opt/rover2/venv/bin/python3 -c 'import httpx' 2>/dev/null"; then
  echo "    httpx not installed — downloading wheels on dev machine and transferring..."
  WHEEL_TMP=$(mktemp -d)
  python3 -c "
import urllib.request, json, os, sys
pkgs = ['httpx', 'httpcore', 'certifi', 'anyio', 'sniffio', 'idna', 'h11']
dest = sys.argv[1]
for pkg in pkgs:
    with urllib.request.urlopen(f'https://pypi.org/pypi/{pkg}/json') as r:
        data = json.load(r)
    ver = data['info']['version']
    for f in data['releases'][ver]:
        if f['filename'].endswith('.whl') and 'none-any' in f['filename']:
            print('  downloading', f['filename'])
            urllib.request.urlretrieve(f['url'], os.path.join(dest, f['filename']))
            break
" "$WHEEL_TMP"
  scp "$WHEEL_TMP"/*.whl "$PI_HOST:/tmp/"
  ssh "$PI_HOST" "/opt/rover2/venv/bin/pip install --no-index /tmp/httpx-*.whl /tmp/httpcore-*.whl /tmp/certifi-*.whl /tmp/anyio-*.whl /tmp/sniffio-*.whl /tmp/idna-*.whl /tmp/h11-*.whl 2>&1 | tail -3"
  rm -rf "$WHEEL_TMP"
  echo "    httpx sideloaded OK"
else
  echo "    httpx already installed"
fi

echo ""
echo "==> Linking system Hailo into ROVER2 venv (Pi)..."
if ssh "$PI_HOST" "[ -d /opt/rover2/venv ] && [ -d /usr/lib/python3/dist-packages/hailo_platform ]"; then
  ssh "$PI_HOST" "bash -s" < "$ROOT/scripts/link_hailo_for_rover2.sh"
fi

echo ""
echo "==> Installing systemd unit (if present)..."
if [ -f "$ROOT/systemd/rover2-api.service" ]; then
  scp "$ROOT/systemd/rover2-api.service" "$PI_HOST:/tmp/rover2-api.service"
  ssh "$PI_HOST" "sudo cp /tmp/rover2-api.service /etc/systemd/system/rover2-api.service && sudo systemctl daemon-reload"
fi

echo ""
echo "==> Restarting rover2-api.service (if installed)..."
if ssh "$PI_HOST" "systemctl is-enabled rover2-api.service 2>/dev/null"; then
  ssh "$PI_HOST" "sudo systemctl restart rover2-api.service"
  sleep 2
  ssh "$PI_HOST" "systemctl status rover2-api.service --no-pager -n 8"
else
  echo "    rover2-api.service not installed yet — see systemd/rover2-api.service"
fi

echo ""
echo "==> Done. Web UI: http://192.168.70.11:8082/ (eth0 — override PI_HOST if needed)"
