# ROVER Face PWA — Installation Guide

## Samsung Galaxy A32 setup

### 1. Connect to ROVER WiFi hotspot

Connect the A32 to the Galaxy Z Flip 6 hotspot (same network as the Pi).
The Pi must be reachable at `192.168.250.254:8082`.

---

### 2. Install ROVER CA certificate (one-time — eliminates all cert warnings)

The Pi serves a self-signed certificate. Install the CA cert once and all future
visits will load silently with no warnings.

1. Connect A32 to ROVER hotspot
2. Open Chrome, navigate to: **`https://192.168.250.254:8082/rover-ca.crt`**
3. Accept the one-time warning (last time you'll ever see it for this device)
4. Android prompts: **"Name the certificate"** → type `ROVER CA` → tap **OK**
5. Tap **CA certificate** when asked for credential use
6. Done — from now on `https://192.168.250.254:8082/` loads silently

> If Android says "Can't install certificate": go to  
> **Settings → Security → More security settings → Install from storage** and pick the downloaded `.crt` file.

#### Fallback (if CA install fails on this Android version)

`chrome://flags/#unsafely-treat-insecure-origin-as-secure`  
Add: `https://192.168.250.254:8082`  
(This is not needed if CA cert is installed correctly.)

---

### 3. Open the PWA in Chrome

Navigate to: **`https://192.168.250.254:8082/face/`**

No cert warning should appear if CA was installed in step 2.

---

### 4. Install as PWA (add to home screen)

- Tap Chrome menu (⋮) → **"Add to Home screen"** → **"Add"**
- Or Chrome shows an install banner automatically
- Launch from home screen → enters fullscreen mode

---

### 5. Grant microphone permission

On first use of the **MIC** button, Chrome will ask for microphone access.
Grant it. Permission persists for the origin. **HTTPS is required** — the CA cert
install in step 2 is the prerequisite.

---

## Pi setup — voice dependencies

### Install Whisper STT

The Pi uses **faster-whisper** (ctranslate2, no PyTorch). Already in `requirements.txt` and
deployed by `deploy_pi.sh`. On first transcription call the `tiny` model downloads (~75 MB).

### Piper TTS

Statically-linked binary at `/opt/rover2/piper/piper`. Voice model at
`/opt/rover2/voices/en_GB-alan-medium.onnx`. Both deployed by `deploy_pi.sh`.

---

## Audio architecture

```
A32 PWA (browser)
  → wss://    /ws           WebSocket telemetry → drives face expressions
  → wss://    /ws/audio     WebSocket binary PCM → proactive TTS speech
  → HTTPS POST /api/voice/transcribe   → faster-whisper STT on Pi
  → HTTPS POST /api/agent/chat         → ROVER personality agent response
  → HTTPS POST /api/voice/speak        → Piper TTS WAV stream → played in browser

Pi audio_router (audio_router.py)
  → Receives PCM frames from voice_engine.speak_event()
  → UDP to A32 IP:8085 (future native app; browser uses /ws/audio)
  → WebSocket broadcast to /ws/audio clients
```

---

## Voice fetch timeouts (face/index.html)

| Step | Timeout | Retry |
|------|---------|-------|
| `/api/voice/transcribe` | 35 s | 1× after 2 s |
| `/api/agent/chat` | 65 s | 1× after 2 s |
| `/api/voice/speak` | 35 s | none |

Debug overlay shows each step in real time: `transcribing… → transcribed: … → sending to agent… → speaking…`

---

## RSS budget with voice loaded

| Component | RSS |
|-----------|-----|
| Base rover2-api | ~100 MB |
| faster-whisper tiny (after first call) | +150 MB |
| Piper (subprocess, no persistent RSS) | 0 MB |
| **Total** | ~250 MB |

Target: <300 MB. Warning logged at 350 MB.

---

## Troubleshooting

**Cert warning on every visit:**
CA cert not installed. Follow step 2 above.

**Face stays OFFLINE:**
Pi not reachable or WebSocket failing.
Test: `curl -sk https://192.168.250.254:8082/api/health` from A32 browser.

**MIC button does nothing / permission denied:**
HTTPS is required for `getUserMedia`. Ensure CA cert is installed (step 2) so
Chrome treats the origin as secure. No `chrome://flags` needed with CA cert installed.

**Voice debug overlay shows "transcribe failed":**
- Whisper model still loading (first call after restart) — wait 10 s, try again
- Pi unreachable — check hotspot connection
- Check journal: `journalctl -u rover2-api -f | grep -i transcribe`

**"sending to agent…" hangs > 60 s:**
Agent LLM (hailo-ollama or CPU Ollama) is slow or unresponsive.
Check: `curl -sk https://192.168.250.254:8082/api/status | python3 -m json.tool`

**Audio format shown in debug as `fmt:audio/mp4`:**
Normal on some Android Chrome versions. faster-whisper handles mp4/webm/ogg via ffmpeg.
