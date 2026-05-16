#!/bin/bash
# Link system HailoRT Python bindings into the ROVER2 venv (Pi only).
set -euo pipefail

VENV_SITE="${ROVER2_VENV_SITE:-/opt/rover2/venv/lib/python3.13/site-packages}"
SYS_SITE="${HAILO_SYS_SITE:-/usr/lib/python3/dist-packages}"

if [ ! -d "$VENV_SITE" ]; then
  echo "ROVER2 venv not found at $VENV_SITE" >&2
  exit 1
fi

for name in hailo_platform hailo.cpython-313-aarch64-linux-gnu.so; do
  src="$SYS_SITE/$name"
  if [ -e "$src" ]; then
    ln -sfn "$src" "$VENV_SITE/$name"
    echo "linked $name"
  fi
done

/opt/rover2/venv/bin/python3 -c "import hailo_platform; print('hailo_platform OK')"
