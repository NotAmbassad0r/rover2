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

## Notes

- Body tracker (YOLOv8m_h10.hef) works on both 5.1.1 and 5.2.0
- hailo-ollama must be restarted after runtime upgrade
- Check `/opt/rover/models/Qwen2-VL-2B-Instruct.hef` is present before testing VLM
- After upgrade, run T1 follow tests to confirm body tracker still works
