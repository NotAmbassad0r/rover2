# ROVER2 Troubleshooting Guide

## Face PWA — Black screen / stale cache

### Symptoms
- Face page shows black screen on A32 or laptop browser
- Debug overlay visible but canvas empty
- Swiping does nothing
- Mic not working after update

### Pull live Pi files back to repo (after manual Pi edits)
If changes were made directly on the Pi (e.g. via sed), sync them back:
```bash
ssh ambassad0r@<ROVER_ETH_IP> "cat /opt/rover2/face/index.html" > ~/Documents/projects/rover2/face/index.html
ssh ambassad0r@<ROVER_ETH_IP> "cat /opt/rover2/face/sw.js" > ~/Documents/projects/rover2/face/sw.js
cd ~/Documents/projects/rover2
git diff face/
```

### Force cache clear on A32
Option A — Chrome site data (most reliable):
Chrome menu → Settings → Privacy and security → Clear browsing data
→ All time → Cached images and files + Cookies and site data → Clear
Then navigate fresh to https://<ROVER_WIFI_IP>:8082/face/

Option B — JavaScript console (Chrome address bar):
javascript:navigator.serviceWorker.getRegistrations().then(r=>r.forEach(x=>x.unregister())).then(()=>location.reload(true))

Option C — DevTools:
Application → Service Workers → Unregister → reload

### Bump cache version manually on Pi (emergency)
```bash
ssh ambassad0r@<ROVER_ETH_IP> "sed -i 's/rover-face-vN/rover-face-vN+1/' /opt/rover2/face/sw.js"
ssh ambassad0r@<ROVER_ETH_IP> "grep CACHE /opt/rover2/face/sw.js"
```
Then clear A32 cache and reload.

### Check what version the Pi is serving
```bash
ssh ambassad0r@<ROVER_ETH_IP> "grep CACHE /opt/rover2/face/sw.js"
ssh ambassad0r@<ROVER_ETH_IP> "curl -sk https://localhost:8082/face/index.html | grep -c '100vh'"
ssh ambassad0r@<ROVER_ETH_IP> "curl -sk https://localhost:8082/face/index.html | grep -c '_gotoPage'"
```

---

## Wake word not triggering

### Symptoms
- VAD fires (LEVEL non-zero, VAD shows "audio - checking wake")
- No POST /api/voice/wake in journal

### Check
```bash
journalctl -u rover2-api -f | grep -i wake
ls -la /tmp/wake_debug_last.wav
```

### What whisper actually heard
```bash
ssh ambassad0r@<ROVER_ETH_IP> "journalctl -u rover2-api -n 50 | grep 'voice/wake'"
```

### Pull last wake audio to dev machine
```bash
scp ambassad0r@<ROVER_ETH_IP>:/tmp/wake_debug_last.wav /tmp/wake_check.wav
```
Then play /tmp/wake_check.wav — if silent/inaudible, mic gain is the issue.

### Test whisper directly on last captured audio
```bash
python3 -c "
from faster_whisper import WhisperModel
m = WhisperModel('tiny', device='cpu', compute_type='int8')
segs, info = m.transcribe('/tmp/wake_debug_last.wav', language='en', beam_size=3, vad_filter=False)
segs = list(segs)
print('lang:', info.language, 'prob:', info.language_probability)
for s in segs: print(f'  [{s.start:.2f}-{s.end:.2f}] {s.text!r}')
"
```

---

## MegaPi not connected

### Symptoms
- POST /api/tracking returns 500
- Journal shows: MegaPi connect failed /dev/ttyUSB0

### Check
```bash
ls /dev/ttyUSB*
journalctl -u rover2-api -n 20 | grep megapi
```
MegaPi must be plugged in before rover2-api starts, or the service
must be restarted after plugging in:
```bash
sudo systemctl restart rover2-api
```

---

## WiFi not connecting after hard power cut

### Symptoms
- Pi boots but no WiFi
- eth0 works but WiFi unavailable

### Fix — connect SSD to laptop
```bash
sudo bash /tmp/fix-cmdline3.sh
sudo bash /tmp/fix-nm-state.sh
```
Known-good cmdline.txt:
console=serial0,115200 multipath=off dwc_otg.lpm_enable=0 console=tty1 root=PARTUUID=a6f9ddfb-02 rootfstype=ext4 rootwait fixrtc cfg80211.ieee80211_regdom=GB

---

## Boot partition read-only error

### Symptom
- Cannot write to /boot/firmware

### Fix
```bash
sudo mount -o remount,rw /boot/firmware
# make changes
sudo mount -o remount,ro /boot/firmware
```

---

## Power bank cuts off

### Symptom
- Pi shuts down after ~46s on battery

### Cause
Viking PN-964PD + standard USB-C cable = 15W max. Pi 5 + Hailo needs 18-20W.
Fix: use 5A/100W e-marked USB-C cable.

### Keepalive (prevents bank auto-shutoff at idle)
stress-ng keepalive service — only on battery, always off on mains.
Check: `systemctl status rover2-virtual-usb-dongle`

---

## General diagnostics commands

```bash
# Service status
sudo systemctl status rover2-api hailo-ollama rover-camera

# Live logs
journalctl -u rover2-api -f

# Memory
ssh ambassad0r@<ROVER_ETH_IP> "ps aux --sort=-%mem | head -10"

# CPU + temp
vcgencmd measure_temp
vcgencmd pmic_read_adc

# Hailo status
curl -sk https://localhost:8082/api/diagnostics/hailo | python3 -m json.tool

# API status
curl -sk https://localhost:8082/api/status | python3 -m json.tool
```

---

## WiFi Access Point (ROVER2)

### Prerequisites
- RTL8812AU USB dongle on wlan1 (`fc:22:1c:20:02:ff`)
- `hostapd` and `dnsmasq` installed: `sudo apt install -y hostapd dnsmasq`
- Config files in `scripts/ap/`

### Manual setup (fresh Pi)
```bash
sudo cp scripts/ap/hostapd.conf /etc/hostapd/hostapd.conf
sudo cp scripts/ap/rover2-ap.network /etc/systemd/network/10-rover2-ap.network
sudo cp scripts/ap/rover2-ap-dnsmasq.conf /etc/dnsmasq.d/rover2-ap.conf
sudo cp scripts/ap/rover2-ap-nm.conf /etc/NetworkManager/conf.d/rover2-ap.conf
sudo cp scripts/ap/rover2-wlan1-ip.service /etc/systemd/system/rover2-wlan1-ip.service
sudo sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd
sudo systemctl unmask hostapd
sudo systemctl enable hostapd dnsmasq rover2-wlan1-ip
sudo systemctl daemon-reload
sudo systemctl restart NetworkManager dnsmasq rover2-wlan1-ip hostapd
```

### Verify
```bash
systemctl is-active hostapd dnsmasq rover2-wlan1-ip
ip addr show wlan1 | grep 10.0.0.1
/usr/sbin/iw dev wlan1 info | grep -E 'type|ssid'
```

### Access
Connect to **ROVER2** WiFi → **`https://10.0.0.1:8082/`**  
Password: see `wpa_passphrase` in `scripts/ap/hostapd.conf`

### Notes
- wlan0 home WiFi continues to work simultaneously
- Static IP assigned by `rover2-wlan1-ip.service` (oneshot, not dhcpcd — Pi uses NetworkManager)
- NM excluded from managing wlan1 via `scripts/ap/rover2-ap-nm.conf`

---

## TLS Certificate Renewal

The TLS cert covers all Pi IPs: `10.0.0.1, 192.168.250.254, 192.168.70.11, 10.62.118.51, rover.local`.

### Renew (from central-computer)
```bash
cd ~/Documents/projects/rover2
./scripts/renew-cert.sh
```

Requires:
- `step-cli` installed on central-computer
- homelabca reachable at `192.168.70.14:9000`

Cert is valid 1 year. Script deploys to Pi and restarts rover2-api automatically.

### Install CA root on new devices
Fetch the homelabca root cert to trust it:
```
https://192.168.250.254:8082/static/homelabca.crt
```
Or from the Pi: `curl -sk https://192.168.70.14:9000/roots.pem`

---

## /etc/rover2.env (Pi system file — not in repo)

rover2-api reads this via `EnvironmentFile=-/etc/rover2.env` in the systemd unit.

**Required contents:**
```
HA_URL=http://192.168.225.10:8123
HA_TOKEN=<home-assistant-long-lived-token>
```

**Create on Pi:**
```bash
sudo tee /etc/rover2.env << 'EOF'
HA_URL=http://192.168.225.10:8123
HA_TOKEN=your-token-here
EOF
sudo chmod 600 /etc/rover2.env
sudo systemctl restart rover2-api
```

Without this file: `ha_available: false` in `/api/status`, all HA voice commands silently disabled.

**Also see `/etc/rover.env`** (read by rover-camera, *not* rover2-api):
```
ROVER_TTS_RATE=0.85
ROVER_TTS_PITCH=0.9
ROVER_PI_IP=192.168.250.254
ROVER_CAMERA_FPS=10
```
