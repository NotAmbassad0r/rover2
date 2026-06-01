# ROVER2 — Home Assistant Integration

ROVER parks facing the door and acts as an AI-powered presence sensor for Home Assistant Alarmo.
BLE beacon (Z Flip 6) arms/disarms automatically. No motors run during guard mode — pure vision sensor.

## Prerequisites

- Home Assistant with Alarmo integration installed
- ROVER2 `rover2-api` running and reachable from HA network
- `guard.ha_base_url` set in `pi/config.yaml`

---

## 1. Webhook setup in Home Assistant

Create three automations (one per webhook):

### A. Intruder detected → trigger alarm / notify

1. **Settings → Automations → New Automation**
2. Trigger: **Webhook** — ID: `rover_intruder`
3. Action options:
   - Alarmo: `alarm_control_panel.alarm_trigger`
   - OR: `notify.mobile_app_<your_phone>` with message from trigger data
4. Optional condition: `{{ trigger.json.beacon_rssi == null or trigger.json.beacon_rssi < -70 }}`

### B. Armed (owner left)

1. Trigger: Webhook — ID: `rover_armed`
2. Action: notify / set Alarmo to armed-away (optional)

### C. Disarmed (owner returned)

1. Trigger: Webhook — ID: `rover_disarmed`
2. Action: notify / set Alarmo to disarmed (optional)

Webhook payload fields (all POSTs include `timestamp: ISO8601`):

| Event | Extra fields |
|-------|--------------|
| `intruder_detected` | `confidence` (float), `beacon_rssi` (int\|null) |
| `armed` | — |
| `disarmed` | — |

---

## 2. ROVER2 config (`pi/config.yaml`)

```yaml
guard:
  enabled: true
  ha_base_url: "http://192.168.225.10:8123"   # your HA IP:port
  ha_token: "<long-lived token>"               # see section 3
  webhook_intruder_id: "rover_intruder"
  webhook_armed_id: "rover_armed"
  webhook_disarmed_id: "rover_disarmed"

  # Tuning (defaults are fine to start)
  rssi_present_threshold: -70      # dBm — beacon above this = owner home
  rssi_leaving_debounce_s: 10.0    # seconds below threshold before arming
  rssi_return_threshold: -65       # beacon above this = owner returned
  intruder_confidence: 0.55        # YOLO confidence threshold (higher = fewer false positives)
  alert_hold_s: 30                 # seconds before auto-clearing alert back to ARMED
```

Deploy: `./deploy_pi.sh` from dev machine.

---

## 3. Long-lived access token

**HA → Profile (bottom-left) → Long-lived access tokens → Create token**

Name it `rover2`. Copy and paste into `guard.ha_token` in `config.yaml`.

> **Security note:** The token is read-only from config at startup. It is never logged,
> never returned in API responses, and never included in WebSocket telemetry.

---

## 4. State machine

```
DISARMED  ← beacon RSSI ≥ -70 dBm (owner home)
    │ RSSI drops below -70 for 10 s
    ▼
 ARMING   (transitional)
    │ debounce elapsed
    ▼
  ARMED   ← Hailo DETECT active, motors off
    │ person detected with confidence ≥ 0.55 AND beacon absent
    ▼
  ALERT   → webhook fired → TTS: "Sir, someone's at the door"
    │ 30 s hold
    ▼
  ARMED   (resume watching)
    │ OR beacon returns ≥ -65 dBm at any time
    ▼
DISARMING (2 s hold)
    │
    ▼
DISARMED  → webhook fired → TTS: "Welcome back, sir"
```

---

## 5. Testing without Home Assistant

**Step 1 — Manual arm:**
```bash
curl -sk -X POST https://<ROVER_WIFI_IP>:8082/api/guard/override \
  -H 'Content-Type: application/json' \
  -d '{"action":"arm"}'
```

**Step 2 — Check status:**
```bash
curl -sk https://<ROVER_WIFI_IP>:8082/api/guard/status | python3 -m json.tool
```

**Step 3 — Walk in front of camera** → verify response shows `"state": "ALERT"` and `detections_this_session: 1`.

**Step 4 — Disarm:**
```bash
curl -sk -X POST https://<ROVER_WIFI_IP>:8082/api/guard/override \
  -H 'Content-Type: application/json' \
  -d '{"action":"disarm"}'
```

**Step 5 — Journal check:**
```bash
ssh ambassad0r@<ROVER_WIFI_IP> "journalctl -u rover2-api -n 30 --no-pager | grep -i guard"
```
Expected lines: `Guard: DISARMED → ARMED`, `Guard: ARMED → ALERT`, webhook POST result.

---

## 6. BLE trigger test (real arm/disarm)

1. Disable Z Flip 6 hotspot (or move out of BLE range)
2. Watch journal: after `rssi_leaving_debounce_s` (10 s), should see `Guard: DISARMED → ARMED`
3. Re-enable hotspot / walk back into range
4. Should see `Guard: ARMED → DISARMING → DISARMED` + TTS "Welcome back, sir"

---

## 7. DIAG tab

The **GUARD MODE** panel in the DIAG tab shows live state, beacon RSSI, armed timestamp,
detection count, and last webhook status. Use the ARM / DISARM / ENABLE / DISABLE buttons
for manual control without curl.

---

## 8. Tuning tips

| Problem | Fix |
|---------|-----|
| Arms while owner is home | Raise `rssi_present_threshold` (e.g. -65) |
| Doesn't arm when owner leaves | Lower `rssi_present_threshold` (e.g. -75) or increase `rssi_leaving_debounce_s` |
| False intruder alerts (pets, reflections) | Raise `intruder_confidence` (e.g. 0.65) |
| Misses real intruders | Lower `intruder_confidence` (e.g. 0.50) |
| Alert clears too quickly | Increase `alert_hold_s` |
| Too many TTS announcements | Guard intruder speech is debounced 60 s; other events 30 s |
