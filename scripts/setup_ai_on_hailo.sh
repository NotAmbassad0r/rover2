#!/bin/bash
# Route LLM inference to AI HAT+ (hailo-ollama), not Pi CPU (ollama.service).
# Pi stays a bridge: Web API, MegaPi serial, tool calls — not CPU LLM.
set -euo pipefail

echo "==> Stopping CPU Ollama (frees port 11434 and CPU)..."
sudo systemctl disable --now ollama.service 2>/dev/null || true

echo "==> hailo-ollama: share HAT with follow/VLM (group_id=rover2)..."
sudo mkdir -p /etc/systemd/system/hailo-ollama.service.d
sudo tee /etc/systemd/system/hailo-ollama.service.d/rover2.conf >/dev/null <<'EOF'
[Service]
Environment="HAILO_OLLAMA_VDEVICE_GROUP_ID=rover2"
Environment="HAILO_OLLAMA_GENERATION_TIMEOUT=120"
EOF

echo "==> Enabling hailo-ollama on AI HAT+..."
sudo systemctl daemon-reload
sudo systemctl enable hailo-ollama.service
sudo systemctl restart hailo-ollama.service

echo "==> Waiting for hailo-ollama API (port 8000 on HAT)..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/api/tags >/dev/null 2>&1; then
    echo "    hailo-ollama ready on :8000"
    curl -s http://127.0.0.1:8000/api/tags | head -c 200
    echo ""
    echo "Set agent.backend: hailo in /opt/rover2/config.yaml and restart rover2-api."
    exit 0
  fi
  sleep 2
done

echo "WARN: hailo-ollama did not respond in 60s — check: journalctl -u hailo-ollama -n 40"
exit 1
