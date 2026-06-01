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
  --exclude 'voices/' \
  --exclude 'piper/' \
  --exclude 'whisper-models/' \
  --exclude 'rover.crt' \
  --exclude 'rover.key' \
  --exclude 'rover-ca.crt' \
  --exclude 'rover-ca.key' \
  "$LOCAL_PATH" "$PI_HOST:$PI_PATH"

echo "==> Syncing scripts to $PI_HOST:/opt/rover2/scripts/"
ssh "$PI_HOST" "mkdir -p /opt/rover2/scripts"
rsync -avz "$SCRIPTS_PATH" "$PI_HOST:/opt/rover2/scripts/"

FACE_PATH="$ROOT/face/"
if [ -d "$FACE_PATH" ]; then
  echo "==> Syncing face/ PWA to $PI_HOST:/opt/rover2/face/"
  ssh "$PI_HOST" "mkdir -p /opt/rover2/face"
  rsync -avz "$FACE_PATH" "$PI_HOST:/opt/rover2/face/"
fi
ssh "$PI_HOST" "chmod +x /opt/rover2/scripts/*.sh 2>/dev/null || true"
ssh "$PI_HOST" "chmod +x /opt/rover2/scripts/setup_ai_on_hailo.sh /opt/rover2/scripts/setup_ai_on_cpu.sh 2>/dev/null || true"

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

# bleak: BLE library — also blocked by TCP/443 on the lab gateway, and dbus_fast
# needs a platform-specific cp313/aarch64 wheel (not none-any), so pip can't fall
# back to a generic sdist either. Sideload all three wheels from the dev machine.
echo ""
echo "==> Checking bleak (sideload from dev machine if pip couldn't install it)..."
if ! ssh "$PI_HOST" "/opt/rover2/venv/bin/python3 -c 'import bleak' 2>/dev/null"; then
  echo "    bleak not installed — downloading wheels on dev machine and transferring..."
  BLEAK_TMP=$(mktemp -d)
  python3 - "$BLEAK_TMP" <<'PYEOF'
import urllib.request, json, os, sys

def best_wheel(files, pkg):
    """Pick none-any first; for platform pkgs fall back to cp313 aarch64."""
    for f in files:
        fn = f["filename"]
        if fn.endswith(".whl") and "none-any" in fn:
            return f
    # platform-specific fallback: cp313 + aarch64 manylinux
    for f in files:
        fn = f["filename"]
        if fn.endswith(".whl") and "cp313" in fn and "aarch64" in fn:
            return f
    return None

dest = sys.argv[1]
for pkg in ["bleak", "async_timeout", "dbus_fast"]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json") as r:
        data = json.load(r)
    ver = data["info"]["version"]
    files = data["releases"][ver]
    chosen = best_wheel(files, pkg)
    if chosen is None:
        print(f"  WARNING: no suitable wheel for {pkg} — skipping", file=sys.stderr)
        continue
    print(f"  downloading {chosen['filename']}")
    urllib.request.urlretrieve(chosen["url"], os.path.join(dest, chosen["filename"]))
PYEOF
  scp "$BLEAK_TMP"/*.whl "$PI_HOST:/tmp/"
  ssh "$PI_HOST" "/opt/rover2/venv/bin/pip install --no-index /tmp/bleak-*.whl /tmp/dbus_fast-*.whl /tmp/async_timeout-*.whl 2>&1 | tail -5"
  rm -rf "$BLEAK_TMP"
  echo "    bleak sideloaded OK"
else
  echo "    bleak already installed"
fi

# piper TTS binary — piper-phonemize has no cp313 aarch64 wheel; use the
# statically-linked piper binary (bundles espeak-ng, RPATH=$ORIGIN).
echo ""
echo "==> Checking piper TTS binary..."
if ! ssh "$PI_HOST" "[ -x /opt/rover2/piper/piper ]" 2>/dev/null; then
  echo "    piper binary not on Pi — downloading on dev machine (~13 MB)..."
  PIPER_BIN_TMP=$(mktemp -d)
  python3 - "$PIPER_BIN_TMP" <<'PYEOF'
import urllib.request, sys, os
dest = os.path.join(sys.argv[1], "piper.tar.gz")
url = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz"
print("  downloading piper aarch64 binary...")
urllib.request.urlretrieve(url, dest)
print(f"  ok: {os.path.getsize(dest)//1024//1024} MB")
PYEOF
  if [ -f "$PIPER_BIN_TMP/piper.tar.gz" ]; then
    scp "$PIPER_BIN_TMP/piper.tar.gz" "$PI_HOST:/tmp/piper.tar.gz"
    ssh "$PI_HOST" "mkdir -p /opt/rover2/piper && cd /opt/rover2/piper && tar xzf /tmp/piper.tar.gz --strip-components=1 && chmod +x piper && rm /tmp/piper.tar.gz"
    echo "    piper binary installed at /opt/rover2/piper/piper"
  else
    echo "    WARN: piper binary download failed — TTS disabled"
  fi
  rm -rf "$PIPER_BIN_TMP"
else
  echo "    piper binary already present"
fi

echo ""
echo "==> Linking system Hailo into ROVER2 venv (Pi)..."
if ssh "$PI_HOST" "[ -d /opt/rover2/venv ] && [ -d /usr/lib/python3/dist-packages/hailo_platform ]"; then
  ssh "$PI_HOST" "bash -s" < "$ROOT/scripts/link_hailo_for_rover2.sh"
fi

echo ""
echo "==> AI backend (agent.backend in config.yaml)..."
AGENT_BACKEND="$(awk '/^[[:space:]]*backend:[[:space:]]+[[:alnum:]_]+/ && !/^[[:space:]]*#/ {print $2; exit}' "$LOCAL_PATH/config.yaml" 2>/dev/null | cut -d# -f1 | tr -d ' ')"
AGENT_BACKEND="${AGENT_BACKEND:-hailo}"
echo "    agent.backend=$AGENT_BACKEND"
ssh "$PI_HOST" "sudo systemctl disable --now hailo-tappas-playground 2>/dev/null || true"
if [ "$AGENT_BACKEND" = "cpu" ]; then
  ssh "$PI_HOST" "sudo bash -s" < "$SCRIPTS_PATH/setup_ai_on_cpu.sh"
elif [ "$AGENT_BACKEND" = "tools_only" ]; then
  ssh "$PI_HOST" "sudo systemctl disable --now ollama hailo-ollama 2>/dev/null || true"
  echo "    LLM services stopped (tools-only / Pi bridge mode)"
else
  ssh "$PI_HOST" "sudo bash -s" < "$SCRIPTS_PATH/setup_ai_on_hailo.sh" || {
    echo "    WARN: hailo-ollama setup failed — agent will use fast-path tools until HAT LLM is up"
  }
fi

echo ""
echo "==> Installing systemd units (if present)..."
for unit in rover2-api.service rover2-powerbank-keepalive.service rover2-powerbank-keepalive.timer rover2-virtual-usb-dongle.service rover2-restore-wifi.service; do
  if [ -f "$ROOT/systemd/$unit" ]; then
    scp "$ROOT/systemd/$unit" "$PI_HOST:/tmp/$unit"
    ssh "$PI_HOST" "sudo cp /tmp/$unit /etc/systemd/system/$unit"
  fi
done
if [ -f "$ROOT/systemd/rover2-powerbank.env" ]; then
  scp "$ROOT/systemd/rover2-powerbank.env" "$PI_HOST:/tmp/rover2-powerbank.env"
  ssh "$PI_HOST" "sudo cp /tmp/rover2-powerbank.env /etc/default/rover2-powerbank"
fi
if [ -f "$ROOT/systemd/rover2-virtual-usb-dongle.env" ]; then
  scp "$ROOT/systemd/rover2-virtual-usb-dongle.env" "$PI_HOST:/tmp/rover2-virtual-usb-dongle.env"
  ssh "$PI_HOST" "sudo cp /tmp/rover2-virtual-usb-dongle.env /etc/default/rover2-virtual-usb-dongle"
fi
if [ -f "$ROOT/systemd/rover2-wifi.sudoers" ]; then
  scp "$ROOT/systemd/rover2-wifi.sudoers" "$PI_HOST:/tmp/rover2-wifi.sudoers"
  ssh "$PI_HOST" "sudo cp /tmp/rover2-wifi.sudoers /etc/sudoers.d/rover2-wifi && sudo chmod 440 /etc/sudoers.d/rover2-wifi && sudo visudo -cf /etc/sudoers.d/rover2-wifi"
fi
ssh "$PI_HOST" "sudo systemctl daemon-reload 2>/dev/null || true"
ssh "$PI_HOST" "sudo systemctl enable rover2-restore-wifi.service 2>/dev/null || true"
if [ -f "$ROOT/scripts/setup_powerbank_keepalive.sh" ]; then
  ssh "$PI_HOST" "sudo bash /opt/rover2/scripts/setup_powerbank_keepalive.sh" 2>/dev/null || true
fi
if [ "${ROVER2_SKIP_VIRTUAL_DONGLE:-}" != "1" ]; then
  if [ -f "$ROOT/scripts/setup_normal_day_power.sh" ]; then
    ssh "$PI_HOST" "sudo bash /opt/rover2/scripts/setup_normal_day_power.sh" 2>/dev/null || true
  fi
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
echo "==> Done. Web UI: https://192.168.70.11:8082/ (eth0 — override PI_HOST if needed)"
