# ROVER2 Acceptance Checklist

Source of truth for human-confirmable acceptance criteria — things a person must
physically observe (a visible face change, audible speech, a device state) that
no offline test or automated smoke script can verify. Mirrors
`tests/TEST_INVENTORY.md` as an externalised state file: updated as features are
added, rows left `[ ]` unchecked until a human has actually performed the
confirmation on real hardware. Do not check a row from source-reading alone.

## How to use this file

Each row names one requirement, what to look/listen for, and the exact steps to
confirm it. Check the box only after physically doing those steps on the robot
/ A32 and observing the described result.

---

## Face emotion/state layer (pi/emotion_state.py, face/index.html v38)

| # | Requirement | What to confirm | How to confirm | Status |
|---|-------------|------------------|-----------------|--------|
| 1 | Face PWA loads the updated build | The emotion badge and EMOTION debug row exist at all (a stale cached SW would show neither) | On the A32: DevTools → Application → Service Workers → Unregister, then hard-reload `https://<host>:8082/face/`. Confirm `sw.js` reports `rover-face-v38` | [ ] |
| 2 | Emotion badge renders distinctly per state | A small labelled pill appears top-centre on the face page (PAGE 0), colour- and text-coded, for `attentive`/`acknowledging`/`working`/`concerned`/`alert`, and is hidden (no badge) for `idle` | Trigger each state live (see rows 3–6 below) and watch the badge on the face page while it happens | [ ] |
| 3 | Ambient state follows person detection | Badge shows `ATTENTIVE` while a person is in the camera's FOLLOW/DETECT view, and disappears (`idle`) a few seconds after they leave frame | Stand in front of the rover with FOLLOW or DETECT enabled; step out of frame; watch the badge transition | [ ] |
| 4 | Ambient state follows wake word | Saying "rover" sets the badge to `ATTENTIVE` (in addition to the existing wake-chime/response) | Say "rover" to the A32 from outside conversation mode; watch the badge the moment the wake chime fires | [ ] |
| 5 | Tool dispatch carries an audible + visible reaction | Asking a diagnostic question (voice or CHAT tab) makes the face say a short acknowledgement ("Sure." / "On it." / etc.) and show `ACKNOWLEDGING` then `WORKING` on the badge, before the final spoken answer arrives | Say or type "what is the temperature" / "run diagnostics"; listen for the short ack phrase firing before the full reply, and watch the badge cycle acknowledging → working → idle/attentive | [ ] |
| 6 | Watchdog concern/alert overrides the badge | A real watchdog-raised alert (warning or critical) turns the badge `CONCERNED` or `ALERT` respectively, taking priority over whatever ambient/tool state was showing, and clears back to normal once the underlying condition resolves | Induce a real watchdog condition (e.g. let CPU run sustained-high, or disconnect/reconnect wlan0) and watch the badge change, then clear, on the DIAG tab's live alert panel and the face badge simultaneously. **Easier now:** `POST /api/watchdog/selftest` (see the Watchdog LLM diagnosis section below, row 9) exercises this exact same real `_emit_alert -> _sync_emotion_state` path on demand, without waiting for a real fault — confirmed automated via `scripts/smoke_test_watchdog_diagnosis.py`, but still needs a human watching the actual face | [ ] |
| 7 | Debug overlay EMOTION row matches the badge | The EMOTION row in the top-left debug overlay always shows the same state name the visible badge is currently showing (or `idle`/`—` when the badge is hidden) | Tap the debug overlay to open it; compare the EMOTION row text against the badge through several state changes | [ ] |
| 8 | No stale/duplicate acknowledgement speech | The short acknowledgement phrase fires once per tool dispatch, not repeatedly while `acknowledging` is still showing on a later telemetry tick | Trigger one diagnostic query; confirm the ack phrase is spoken exactly once, not looping | [ ] |

---

## Watchdog LLM diagnosis layer + self-test hook (pi/watchdog.py, pi/agent.py)

`scripts/smoke_test_watchdog_diagnosis.py` confirmed the mechanics live (self-test
raises a real alert, face state maps to concerned, it auto-clears, and a
diagnosis record appeared in the log for the synthetic alert) — but a human
still needs to actually watch/listen to the face and dashboard to confirm the
*experience* is right, not just that the JSON is correct.

| # | Requirement | What to confirm | How to confirm | Status |
|---|-------------|------------------|-----------------|--------|
| 9 | Self-test hook exercises the real face override | `POST /api/watchdog/selftest` visibly turns the face badge `CONCERNED` (or `ALERT` with `{"severity":"critical"}` if you add that param support), and it visibly clears back to normal ~8s later — this is the same real `_emit_alert -> _sync_emotion_state` path a genuine fault uses, so this row is what makes row 6 above practical to check on demand | On the Pi or from central-computer: `curl -sk -X POST https://<host>:8082/api/watchdog/selftest`. Watch the face (and DIAG tab alert panel) turn concerned, then clear ~8s later. Confirm the alert message visibly says `[SELF-TEST]` — never mistake it for a real fault | [ ] |
| 10 | Diagnosis appears on the dashboard in plain language | For a REAL alert (not the self-test), a plain-English diagnosis line appears attached to the alert (e.g. in the DIAG tab's watchdog/alerts panel) within roughly the time a live "what's wrong" voice query would take, worded understandably (not raw JSON/tool-call syntax) | Wait for (or safely induce) a real alert — e.g. `camera_frames_stall` fires on its own if the camera service is bounced. Check `GET /api/watchdog/status` (or the DIAG tab) for a `diagnosis.text` field on that alert entry and read it — confirm it's a sensible plain-language explanation, not empty/garbled | [ ] |
| 11 | Diagnosis is advisory only — real alert still auto-remediates with the LLM offline | With `hailo-ollama`/the Hailo LLM path unavailable (or the GenAI session busy), a real alert still triggers its existing deterministic remediation (e.g. service restart) exactly as before this feature existed — diagnosis is absent or shows a "skipped" note, but nothing about the actual fix is delayed or skipped | Temporarily make the Hailo LLM path unavailable (e.g. during a VLM describe call, or by watching `journalctl -u rover2-api` for `hailo_session` being held), trigger a real remediable condition, and confirm the underlying service action still happens on schedule (check `action_history` in `/api/watchdog/status`) while the diagnosis field is missing or explicitly notes a skip | [ ] |
