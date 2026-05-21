#!/bin/bash
# Short load burst so MCU power banks (e.g. Viking PN-964PD) do not auto-off on low current.
set -euo pipefail

BURST_SEC="${POWERBANK_BURST_SEC:-2}"
CPU_WORKERS="${POWERBANK_CPU_WORKERS:-1}"

if command -v stress-ng >/dev/null 2>&1; then
  exec stress-ng --cpu "$CPU_WORKERS" --timeout "${BURST_SEC}" --quiet
fi

# Fallback if stress-ng missing
head -c 4M </dev/urandom >/dev/null 2>&1 || true
sync
