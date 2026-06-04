#!/bin/bash
# Renew rover2 TLS certificate from homelabca
# Run from central-computer when cert is near expiry
# Requires: step-cli installed, homelabca reachable at 192.168.70.14:9000

set -e

CA_URL="https://192.168.70.14:9000"
CA_ROOT="homelabca-root.pem"
CERT_OUT="rover.crt"
KEY_OUT="rover.key"

echo "==> Fetching CA root..."
curl -sk ${CA_URL}/roots.pem > ${CA_ROOT}

echo "==> Requesting certificate..."
/usr/bin/step-cli ca certificate rover.local ${CERT_OUT} ${KEY_OUT} \
  --ca-url ${CA_URL} \
  --root ${CA_ROOT} \
  --san 192.168.250.254 \
  --san 192.168.70.11 \
  --san 10.0.0.1 \
  --san 10.62.118.51 \
  --san rover.local \
  --san rover \
  --provisioner admin \
  --not-after 8760h

echo "==> Deploying to Pi..."
scp ${CERT_OUT} ambassad0r@192.168.250.254:/home/ambassad0r/rover.crt
scp ${KEY_OUT} ambassad0r@192.168.250.254:/home/ambassad0r/rover.key
ssh ambassad0r@192.168.250.254 "sudo mv /home/ambassad0r/rover.crt /opt/rover2/rover.crt && sudo mv /home/ambassad0r/rover.key /opt/rover2/rover.key && sudo systemctl restart rover2-api"

echo "==> Done. Cert valid for 1 year."
