# HailoRT 5.1.1 → 5.2.0 Upgrade Plan

## Why

VLM (`Qwen2-VL-2B-Instruct.hef`) requires HailoRT ≥ 5.2.0.  
Current: 5.1.1 (installed)  
Target: 5.2.0

Body tracker (YOLOv8m) is unaffected by the version; only VLM requires 5.2.0.

## Prerequisites

- Hailo HAT physically connected and recognised (`hailortcli fw-control identify`)
- Pi on ethernet (eth0) — do not upgrade over WiFi
- 2 GB free disk space on Pi
- Backup `/opt/rover2/` before starting:
  ```bash
  sudo cp -a /opt/rover2 /opt/rover2.bak
  ```

## Steps

### 1. Check current version
```bash
hailortcli fw-control identify
python3 -c "import hailo_platform; print(hailo_platform.__version__)"
```

### 2. Stop services that use Hailo
```bash
sudo systemctl stop rover2-api hailo-ollama
```

### 3. Get HailoRT 5.2.0 source
```bash
# If ~/hailort already exists:
cd ~/hailort && git fetch && git checkout v5.2.0

# If not:
git clone https://github.com/hailo-ai/hailort.git ~/hailort
cd ~/hailort && git checkout v5.2.0
```

### 4. Build
```bash
cd ~/hailort
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j4   # takes ~15 min on Pi 5
```

### 5. Install
```bash
sudo make install
sudo ldconfig
```

### 6. Verify
```bash
hailortcli fw-control identify
python3 -c "import hailo_platform; print(hailo_platform.__version__)"
# Expected: 5.2.0
```

### 7. Restart services
```bash
sudo systemctl start hailo-ollama rover2-api
sleep 5
sudo systemctl status rover2-api --no-pager | head -10
```

### 8. Test VLM
```bash
curl -sk https://localhost:8082/api/vision/describe | python3 -m json.tool
# Expected: description field populated (not error)
```

## Rollback

If 5.2.0 breaks body tracker or other functionality:
```bash
sudo systemctl stop rover2-api hailo-ollama
cd ~/hailort
git checkout v5.1.1
mkdir -p build_511 && cd build_511
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j4
sudo make install && sudo ldconfig
sudo systemctl start hailo-ollama rover2-api
```

## Actual steps required (discovered 2026-06-05)

The steps above were the initial plan. The actual upgrade required additional work:

### A. PCIe driver rebuild (required — not in original plan)
```bash
git clone https://github.com/hailo-ai/hailort-drivers.git ~/hailort-drivers
cd ~/hailort-drivers && git checkout v5.2.0
cd linux/pcie && make
# Copy built module (replace version with actual kernel):
sudo cp hailo1x_pci.ko.xz /lib/modules/$(uname -r)/updates/dkms/hailo1x_pci.ko.xz
sudo depmod -a && sudo modprobe -r hailo1x_pci && sudo modprobe hailo1x_pci
```

### B. Firmware update (required — not in original plan)
The chip firmware must match the driver. Download 5.2.0 firmware and reboot:
```bash
wget "https://hailo-hailort.s3.eu-west-2.amazonaws.com/Hailo10H/5.2.0/FW/hailo10h_fw.tar.gz"
tar -xzf hailo10h_fw.tar.gz
sudo cp -r hailo10h/* /lib/firmware/hailo/hailo10h/
sudo reboot   # full reboot required — rmmod alone doesn't reflash firmware
```

### C. Python bindings (required — installed separately from cmake build)
```bash
cd ~/hailort/hailort/libhailort/bindings/python/platform
/opt/rover2/venv/bin/pip install .
```

### D. hailo-ollama rebuild (required if using hailo-ollama)
```bash
cd ~/hailo_model_zoo_genai
cmake -B build -DHailoRT_DIR=/usr/local/lib/cmake/HailoRT && cmake --build build -j4
sudo cp build/hailo-ollama /usr/local/bin/hailo-ollama
```

### E. Pillow (required — VLM inference dependency)
```bash
/opt/rover2/venv/bin/pip install Pillow
```

## Critical discovery: hailo-ollama and VLM are mutually exclusive

**HailoRT 5.2.0 firmware only allows ONE GenAI session at a time.** This means:
- `hailo-ollama` (LLM on port 12145) AND `vlm_engine.py` (VLM on port 12147) cannot run simultaneously
- If hailo-ollama is running, VLM gets `HAILO_COMMUNICATION_CLOSED(62)` at connect time
- Body tracker (VDMA inference, not GenAI) is unaffected

**Current decision:** hailo-ollama disabled (see HANDOFF.md issue #26). Voice agent uses CPU `llama3.2:1b` fallback. VLM works.

To disable hailo-ollama:
```bash
sudo mkdir -p /etc/systemd/system/hailo-ollama.service.d
sudo tee /etc/systemd/system/hailo-ollama.service.d/override.conf << 'EOF'
[Service]
ExecStart=
ExecStart=/bin/true
Restart=no
EOF
sudo systemctl daemon-reload && sudo systemctl stop hailo-ollama
```

Also edit `rover2-api.service` to remove `hailo-ollama.service` from `Wants=` and `After=`.

## Notes

- Body tracker (YOLOv8m_h10.hef) works on both 5.1.1 and 5.2.0
- VLM (`Qwen2-VL-2B-Instruct.hef`) takes ~80s to load on first preload (3GB model transfer to chip SRAM)
- During VLM loading, body tracker inference is paused (chip busy with PCIe model transfer)
- VLM preloads at t+40s after startup (background thread in vlm_engine.py)
- After upgrade, run T1 follow tests to confirm body tracker still works
- `hailortcli fw-control identify` must show `Firmware Version: 5.2.0` to confirm chip firmware updated
