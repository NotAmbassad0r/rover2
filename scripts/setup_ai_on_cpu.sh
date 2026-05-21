#!/bin/bash
# Legacy: LLM on Pi CPU (ollama.service). High CPU load — not recommended on robot.
set -euo pipefail

echo "==> Stopping hailo-ollama (HAT LLM)..."
sudo systemctl disable --now hailo-ollama.service 2>/dev/null || true

echo "==> Enabling CPU ollama.service..."
sudo systemctl enable ollama.service
sudo systemctl restart ollama.service

echo "Set agent.backend: cpu in /opt/rover2/config.yaml and restart rover2-api."
