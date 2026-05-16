#!/bin/bash
# Install avrdude + libs under /opt/rover2/tools on the Pi. Run ON the Pi (or via ssh).
set -euo pipefail

DEST="/opt/rover2/tools"
sudo mkdir -p "$DEST"

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "This script targets Pi arm64. Run on the Raspberry Pi."
  exit 1
fi

WORKDIR="${TMPDIR:-/tmp}/rover2_avrdude_install"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

fetch() {
  if [[ ! -f "$(basename "$1")" ]]; then
    wget -q "$1"
  fi
}

fetch "http://ftp.us.debian.org/debian/pool/main/a/avrdude/avrdude_7.1+dfsg-3+b3_arm64.deb"
fetch "http://ftp.us.debian.org/debian/pool/main/libf/libftdi/libftdi1_0.20-4+b2_arm64.deb"
fetch "http://ftp.us.debian.org/debian/pool/main/libu/libusb/libusb-0.1-4_0.1.12-35+b1_arm64.deb"
fetch "http://ftp.de.debian.org/debian/pool/main/h/hidapi/libhidapi-libusb0_0.14.0-1+b2_arm64.deb"

for deb in *.deb; do
  dpkg-deb -x "$deb" "$WORKDIR/extract"
done

sudo cp "$WORKDIR/extract/usr/bin/avrdude" "$DEST/"
sudo cp "$WORKDIR/extract/etc/avrdude.conf" "$DEST/"
sudo cp "$WORKDIR/extract/usr/lib/aarch64-linux-gnu/libftdi.so.1" "$DEST/" 2>/dev/null || true
sudo cp "$WORKDIR/extract/usr/lib/aarch64-linux-gnu/libusb-0.1.so.4" "$DEST/" 2>/dev/null || true
sudo cp "$WORKDIR/extract/usr/lib/aarch64-linux-gnu/libhidapi-libusb.so.0" "$DEST/" 2>/dev/null || true
sudo chmod +x "$DEST/avrdude"

echo "Installed to $DEST"
LD_LIBRARY_PATH="$DEST" "$DEST/avrdude" --version | head -1
