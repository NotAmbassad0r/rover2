# ROVER2 Test Inventory

Source of truth for offline pytest coverage. Updated as tests are added.

## Classification

| Code | Meaning |
|------|---------|
| A    | Pure logic — testable with no mocks |
| B    | Testable with mocked hardware boundary |
| C    | Hardware-only — out of scope for offline suite |
| seam | Logic is hardware-coupled; refactor needed before testing |

---

## Category A — Pure / Offline

### `pi/agent_knowledge.py`
- Module-level string constant `ROVER2_KNOWLEDGE`
- **Tests**: content not-empty, is a string — `test_agent_solutions.py`

### `pi/agent_solutions.py`
- `build_solutions(cpu, temps, ram, alerts, processes, log_snippet, user_asked_fix)` — actionable fix text
- **Tests**: `test_agent_solutions.py`
  - Hot CPU → cooling step
  - Critical temp → cooling step
  - High RAM → restart step
  - Disk alert → disk step
  - Ollama in log + hot CPU → stop-ollama step
  - Hailo busy in log → single-task step
  - No alerts, no fix requested → empty string
  - No alerts, user asked fix → generic steps present
  - Deduplication of identical steps

### `pi/arm_control.py`
- `arm_pwm(direction, speed_frac, config)` — PWM mapper
- **Tests**: `test_arm_control.py`
  - stop/off/empty → 0
  - up → positive PWM proportional to speed_frac * max_speed
  - down → negative PWM
  - invert=True → sign flipped
  - clamped to ±255
  - unknown direction → ValueError

### `pi/body_tracker_parse.py`
- `nms_output_to_tensor(nms_output)` — reshape Hailo NMS raw output
- `is_valid_person_box(y0, x0, y1, x1, score)` — geometry gate
- `parse_best_person(nms_output, confidence, ...)` — extract best detection
- **Tests**: `test_body_tracker_parse.py`
  - Empty (80, 501) → None returned
  - Synthetic (80, 501) with person at known coords → correct box (score=0.82)
  - Score is realistic (> 0.4, < 0.999, never 1.00) — regression for bug fix
  - Field order correct: y0, x0, y1, x1, score (not score-first)
  - Count-drop: count at col 0 is excluded from detection data
  - Confidence threshold respected (score 0.35 < 0.40 → None)
  - Min box width/height/area gates respected
  - Coord swap normalisation (x0 > x1 swapped, still valid)
  - Fixture-based test: @skipif absent, reason "capture with pi/capture_nms_fixture.py"

### `pi/chat_router.py`
- `route(text)` — regex NL router
- **Tests**: `test_chat_router.py`
  - "stop" → stop action
  - "follow me" → follow action, needs_camera=False
  - "what do you see" → describe, needs_camera=True
  - "move forward" / "advance" → forward
  - "turn left" / "turn right"
  - "open gripper" → grip_open
  - "arm up" → arm_up
  - "stop following" → unfollow (more specific, matched before generic stop)
  - Unknown input → None action, canned reply present
  - Case-insensitive matching

### `pi/config_public.py`
- `mask_config(data)` — deep secret redaction
- `public_config(config)` — full config mask
- **Tests**: `test_config_public.py`
  - Top-level `token` → "***"
  - Nested `api_key` → "***"
  - Non-secret key passes through
  - List items processed recursively
  - `password`, `secret`, `credential`, `private` all masked
  - Non-secret nested dict unmasked

### `pi/config_store.py`
- `_clamp(value, lo, hi, typ)` — range enforcer
- `extract_tuning_patch(body)` — validated patch extractor
- `merge_tuning(config, patch)` — deep merge
- `save_config(path, config)` — YAML persistence
- `tuning_schema_public()` — schema export
- **Tests**: `test_config_store.py`
  - `_clamp` int/float/bool/str
  - `_clamp` float at boundary (clamped to min, max)
  - Valid tuning body parsed correctly
  - Out-of-range values clamped (not rejected)
  - `avoid_default` invalid value → ValueError
  - Non-dict body → ValueError
  - Non-dict section → ValueError
  - Empty / unknown-only body → ValueError
  - `merge_tuning` merges sections
  - `save_config` writes valid YAML (round-trips)

### `pi/follow_nav.py`
- `steer_around_obstacle(wanted, forward_blocked, person_cx, ble_turn, default_avoid)`
- `ble_should_turn_in_place(rssi_history, min_samples, drop_dbm)`
- **Tests**: `test_follow_nav.py`
  - Non-forward command → unchanged
  - Forward, not blocked → unchanged
  - Forward, blocked, person cx < 0.45 → "left"
  - Forward, blocked, person cx > 0.55 → "right"
  - Forward, blocked, person cx centred → default_avoid
  - Forward, blocked, no cx, ble_turn="left" → "left"
  - Forward, blocked, no cx or ble_turn, default_avoid="right" → "right"
  - BLE turn: falling RSSI (last - third-from-last < -2) → True
  - BLE turn: stable RSSI → False
  - BLE turn: < min_samples → False

### `pi/hailo_session.py`
- `genai_session(holder)` — exclusive async lock
- `session_holder()` — current holder query
- **Tests**: `test_hailo_session.py`
  - Acquire lock → session_holder() returns name
  - Release → session_holder() returns None
  - Two sequential sessions (non-overlapping) both succeed
  - Module-level lock is shared (singleton)

### `pi/log_buffer.py`
- `RingLogHandler` — thread-safe ring buffer
- `install_log_ring_buffer()`, `get_log_handler()`
- **Tests**: `test_log_buffer.py`
  - emit → record appears in get_records()
  - clear → buffer empty
  - maxlen enforced (oldest record dropped)
  - install is idempotent (handler added only once)
  - get_log_handler() returns singleton

### `pi/metrics_store.py`
- `write(points, ts)`, `query(name, since_ts)`, `query_latest(names, since_ts)`
- `disable_writes()`, `enable_writes()`, `writes_enabled()`
- `purge(retention_days)`, `available()`, `db_size_mb()`
- **Tests**: `test_metrics_store.py`
  - write then query returns point
  - write then query_latest returns latest
  - writes_disabled → write is no-op
  - enable_writes restores writes
  - purge removes old entries
  - available() lists metric names
  - db_size_mb() returns float

---

## Category B — Mocked Hardware Boundary

### `pi/camera_idle.py`
- `CameraIdleManager._feed()` — sliding window stability check
- `CameraIdleManager.wake()`, `notify_tracking_active()`, `notify_stream_connect()`
- `CameraIdleManager.sleeping` property
- **Tests**: `test_camera_idle.py`
  - Full window of stable readings → sleeping after _sleep_after_s reached
  - Window not full yet → no sleep decision
  - Variance too high → stable_since reset
  - Wake on ultrasonic delta > variance_cm
  - `notify_stream_connect` wakes immediately
  - `set_tracking_active(True)` wakes + suppresses sleep
  - `set_tracking_active(False)` allows idle to resume
  - `sleeping` returns False when `_enabled=False`

### `pi/guard.py`
- `GuardController._transition(new_state)` — state machine
- `GuardController._on_person_detected(confidence, bbox)` — intruder callback
- `GuardController._handle_intruder(confidence, bbox)`
- `GuardController.get_stats()`, `get_state()`, `set_enabled()`
- `GuardController.manual_override(action)`
- **Tests**: `test_guard.py`
  - DISARMED → ARMED manual override → state is ARMED
  - ARMED → DISARMED manual override → state is DISARMED
  - `_on_person_detected` when ARMED + confidence >= threshold → ALERT
  - `_on_person_detected` when DISARMED → no state change
  - `_on_person_detected` confidence below threshold → no state change
  - `_on_person_detected` beacon still present (RSSI >= return) → no state change
  - `set_enabled(False)` → transitions to DISARMED
  - `get_stats()` contains all expected keys

### `pi/ha_client.py`
- `friendly_name(entity_id, ha_name)` — pure lookup (A-class)
- `HAClient.get_states()`, `get_state()`, `call_service()`, `toggle()`
- `HAClient.get_temperature_sensors()`, `get_toggleable_entities()`
- `HAClient.ping()`
- **Tests**: `test_ha_client.py`
  - `friendly_name` known entity_id → override name
  - `friendly_name` unknown entity, ha_name provided → ha_name
  - `friendly_name` unknown entity, no ha_name → formatted entity_id
  - `get_states()` HTTP 200 → list returned
  - `get_states()` network error → empty list (never raises)
  - `get_state()` 200 → dict
  - `get_state()` 404 → None
  - `toggle()` media_player domain → calls media_play_pause
  - `toggle()` light domain → calls homeassistant.toggle
  - `get_temperature_sensors()` filters by unit (°C/°F)
  - `get_toggleable_entities()` filters by domain
  - `ping()` success → True; failure → False

### `pi/safety.py`
- `SafetyMonitor._is_forward(left, right)` — motor sign check
- `SafetyMonitor._should_block(command)` — forward+close obstacle gate
- `SafetyMonitor._guarded_send(command)` — intercept + replace
- `SafetyMonitor._ingest_ultrasonic(msg)` — sensor message parser
- `SafetyMonitor.set_safe_distance_cm()`, `clear_block()`
- **Tests**: `test_safety.py`
  - `_is_forward` matches forward_signs
  - `_is_forward` fails for reverse
  - `_is_forward` fails for arc turn (one motor zero)
  - `_is_forward` zero-zero → False
  - `_should_block` distance < threshold, forward command → True
  - `_should_block` distance > threshold → False
  - `_should_block` distance None → False
  - `_should_block` non-drive command → False
  - `_guarded_send` forward+close → stop command sent, forward_blocked=True
  - `_guarded_send` reverse → passed through, forward_blocked cleared
  - `_guarded_send` non-drive → passed through unchanged
  - `_ingest_ultrasonic` valid sensor message → updates _distance_cm
  - `_ingest_ultrasonic` non-sensor message → ignored
  - `set_safe_distance_cm` updates threshold
  - `clear_block` resets forward_blocked

### `pi/watchdog.py`
- `Watchdog._can_act()`, `_record_action()`, `_prune_window()` — global rate limit
- `Watchdog._can_act_for(key)`, `_record_attempt(key)` — per-check rate limit
- `Watchdog._emit_alert()`, `_clear_alert()`, `get_persistent_alerts()`
- `Watchdog.reset_actions()`
- **Tests**: `test_watchdog.py`
  - `_can_act()` True when under limit
  - `_can_act()` False after max_actions reached
  - `_record_action()` 3× → limit_reached=True
  - `reset_actions()` → limit cleared, alerts cleared
  - Per-check: 3 attempts for same key → persistent alert emitted
  - `_emit_alert()` adds entry with correct fields
  - `_emit_alert()` replaces previous alert with same key
  - `_clear_alert()` removes by key
  - `get_persistent_alerts()` returns copy (modifying it doesn't affect internal list)

### `pi/voice_engine.py` (pure parts only)
- `_preprocess_for_tts(text)` — TTS normalisation
- `is_active()` / `set_active(active)` — session flag
- `speak_event(event_key, ...)` — debounced TTS dispatch
- **Tests**: `test_voice_routing.py`
  - `_preprocess_for_tts`: 42.3°C → "42.3 degrees Celsius"
  - `_preprocess_for_tts`: 75% → "75 percent"
  - `_preprocess_for_tts`: markdown stripped (*bold* → bold)
  - `_preprocess_for_tts`: thousands separator removed (1,234,567 → 1234567)
  - `_preprocess_for_tts`: RSSI → "signal strength"
  - `_preprocess_for_tts`: BLE → "Bluetooth"
  - `is_active()` / `set_active()` round-trip
  - `speak_event` debounce: second call within window → silent (no audio_router call)
  - `speak_event` after debounce window → fires again

---

## Category C — Hardware-Only (Out of Scope)

| Module | Reason |
|--------|--------|
| `pi/camera_server.py` | Requires `/dev/videoX` (physical camera) |
| `pi/firmware_flash.py` | Requires Arduino + avrdude binary |
| `pi/main.py` | Bootstrap only; not unit-testable |
| `scripts/cpu_bench.py` | Standalone benchmark; not part of service |

---

## Needs Seam — Testable After Refactoring

These modules contain testable logic but lack a hardware-injection seam. Filed for later refactoring.

| Module | Blocker | Test to add once seam exists |
|--------|---------|------------------------------|
| `pi/megapi.py` | Direct `serial.Serial()` in `__init__` | JSON parse/write protocol tests |
| `pi/body_tracker.py` | `_HailoInference` inner class creates VDevice directly | Follow-logic tests (steer decision); parse integration |
| `pi/ble_tracker.py` | `bleak.BleakScanner` instantiated in thread; no injection point | EMA smoothing, RSSI timeout |
| `pi/safety.py` | `monkey-patches megapi.send`; works with mock but seam is fragile | Already tested via mock |
| `pi/vlm_engine.py` | Direct `hailo_platform.genai.VLM` in `_open_and_generate()` | Cooldown/status logic is testable |
| `pi/voice_engine.py` | `_load_whisper()` calls `faster_whisper.WhisperModel` | Wake-word detection, STT routing |
| `pi/audio_router.py` | UDP socket created in `start()` | Mode switching, client management |
| `pi/thermal.py` | `subprocess vcgencmd` in `read_thermal()` | Threshold logic in `evaluate()` |

---

## Coverage Summary

| Test file | Modules covered | Status |
|-----------|----------------|--------|
| `test_arm_control.py` | arm_control | ✅ |
| `test_agent_solutions.py` | agent_solutions | ✅ |
| `test_body_tracker_parse.py` | body_tracker_parse | ✅ |
| `test_camera_idle.py` | camera_idle | ✅ |
| `test_chat_router.py` | chat_router | ✅ |
| `test_config_public.py` | config_public | ✅ |
| `test_config_store.py` | config_store | ✅ |
| `test_follow_nav.py` | follow_nav | ✅ |
| `test_guard.py` | guard | ✅ |
| `test_ha_client.py` | ha_client | ✅ |
| `test_hailo_session.py` | hailo_session | ✅ |
| `test_log_buffer.py` | log_buffer | ✅ |
| `test_metrics_store.py` | metrics_store | ✅ |
| `test_safety.py` | safety | ✅ |
| `test_voice_routing.py` | voice_engine (pure parts) | ✅ |
| `test_watchdog.py` | watchdog | ✅ |
