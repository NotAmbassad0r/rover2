#!/bin/bash
# Renew rover2 TLS certificate from homelabca (step-ca at 192.168.70.14:9000)
#
# Automated path: uses 'step ca renew' (mTLS — no admin password needed).
# Manual fallback: 'step ca certificate' via admin provisioner (interactive).
#
# Usage:
#   ./scripts/renew-cert.sh               # auto — skips if >60 days remaining
#   FORCE=1 ./scripts/renew-cert.sh       # renew regardless of remaining time
#
# Cron: called by scripts/cert-renew-cron.sh on the 1st of each month.
# Certs stored in certs/ (rover.crt tracked; rover.key gitignored).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

CA_URL="https://192.168.70.14:9000"
CERT_DIR="${REPO_DIR}/certs"
CA_ROOT="${CERT_DIR}/homelabca-root.pem"
CERT_OUT="${CERT_DIR}/rover.crt"
KEY_OUT="${CERT_DIR}/rover.key"

# Threshold in days: skip renewal if cert has more time left than this.
RENEW_DAYS_THRESHOLD="${RENEW_DAYS_THRESHOLD:-60}"

# Days remaining on the cert (0 if cert missing or unreadable)
_days_remaining() {
    if [ ! -f "$CERT_OUT" ]; then echo 0; return; fi
    openssl x509 -in "$CERT_OUT" -noout -enddate 2>/dev/null \
        | awk -F= '{print $2}' \
        | python3 -c "
import sys, datetime
s = sys.stdin.read().strip()  # 'Jun  3 13:46:48 2027 GMT'
exp = datetime.datetime.strptime(s, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
print(max(0, (exp - now).days))
"
}

mkdir -p "$CERT_DIR"

# Skip early if cert has enough time left (unless FORCE=1)
if [ "${FORCE:-0}" != "1" ] && [ -f "$CERT_OUT" ]; then
    days=$(_days_remaining)
    if [ "$days" -gt "$RENEW_DAYS_THRESHOLD" ]; then
        echo "==> Cert valid for ${days} more days (threshold: ${RENEW_DAYS_THRESHOLD} days) — no renewal needed."
        exit 0
    fi
    echo "==> Cert expires in ${days} days — renewing now."
fi

# Verify CA is reachable before proceeding
echo "==> Checking CA reachability (${CA_URL})..."
if ! curl -sk --max-time 10 "${CA_URL}/health" >/dev/null 2>&1; then
    echo "ERROR: Cannot reach CA at ${CA_URL}" >&2
    echo "       Check that homelabca is running and you are on the home network." >&2
    exit 1
fi

# Update the CA root cert (safe to re-fetch — it's public)
echo "==> Fetching CA root..."
if ! curl -sk --max-time 10 "${CA_URL}/roots.pem" > "${CA_ROOT}.tmp"; then
    echo "ERROR: Failed to download CA root" >&2; rm -f "${CA_ROOT}.tmp"; exit 1
fi
if ! openssl x509 -in "${CA_ROOT}.tmp" -noout 2>/dev/null; then
    echo "ERROR: CA returned an invalid certificate" >&2; rm -f "${CA_ROOT}.tmp"; exit 1
fi
mv "${CA_ROOT}.tmp" "$CA_ROOT"

# Attempt automated renewal via mTLS (no admin password required)
RENEWED=0
if [ -f "$CERT_OUT" ] && [ -f "$KEY_OUT" ]; then
    echo "==> Trying automated renewal (mTLS)..."
    if step-cli ca renew \
            --ca-url "$CA_URL" \
            --root "$CA_ROOT" \
            --force \
            "$CERT_OUT" "$KEY_OUT" 2>/dev/null; then
        RENEWED=1
        echo "==> mTLS renewal succeeded."
    else
        echo "==> mTLS renewal failed — falling back to admin provisioner (requires password)."
    fi
fi

# Fallback: issue new cert via admin provisioner (interactive password prompt)
if [ "$RENEWED" -eq 0 ]; then
    echo "==> Requesting new certificate via admin provisioner..."
    step-cli ca certificate rover.local "$CERT_OUT" "$KEY_OUT" \
        --ca-url "$CA_URL" \
        --root "$CA_ROOT" \
        --san 192.168.250.254 \
        --san 192.168.70.11 \
        --san 10.0.0.1 \
        --san 10.62.118.51 \
        --san rover.local \
        --san rover \
        --provisioner admin \
        --not-after 8760h
fi

# Keep root-level copies in sync (used by old deploy workflow; gitignored)
cp "$CERT_OUT" "${REPO_DIR}/rover.crt"
cp "$KEY_OUT"  "${REPO_DIR}/rover.key"

# Deploy to Pi
echo "==> Deploying to Pi (rover)..."
scp "$CERT_OUT" ambassad0r@rover:/home/ambassad0r/rover.crt
scp "$KEY_OUT"  ambassad0r@rover:/home/ambassad0r/rover.key
ssh ambassad0r@rover "
    sudo mv /home/ambassad0r/rover.crt /opt/rover2/rover.crt
    sudo mv /home/ambassad0r/rover.key /opt/rover2/rover.key
    sudo chmod 640 /opt/rover2/rover.key
    sudo systemctl restart rover2-api
    sleep 2
    curl -sk https://localhost:8082/api/status | python3 -c \"import sys,json; d=json.load(sys.stdin); print('rover2-api OK:', d.get('uptime_s','?'), 's uptime')\" 2>/dev/null || echo 'rover2-api health check failed'
"

EXPIRY=$(openssl x509 -in "$CERT_OUT" -noout -enddate | cut -d= -f2)
echo "==> Done. Cert valid until: ${EXPIRY}"
