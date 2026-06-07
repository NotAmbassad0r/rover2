# ROVER2 Backlog

Items here are confirmed desirable but not yet scheduled.
Format: ## Category / ### Item / bullet details / Priority line.

---

## Web UI

### Improve web GUI and verify all functions

- Audit every tab (CONTROL, TOOLS, DIAG, CHAT, METRICS) for visual consistency
- Check every button, toggle, slider, and input actually works end-to-end
- Verify STATUS table values are live and accurate (WEBSOCKET, SERIAL, MOTORS, FIRMWARE,
  ULTRASONIC, SAFETY, FOLLOW, SOURCE, PERSON, BLE BEACON, CAMERA, UPTIME)
- Verify follow mode selector (DETECT / CAMERA / FUSED / BLE) transitions correctly
- Verify D-pad, arm lift, gripper all respond
- Verify METRICS tab charts update in real time
- Verify DIAG tab shows current values, not stale cache
- Verify CHAT tab agent responses and VLM button work
- Verify TOOLS tab scripts execute and return output
- Verify alerts appear in UI when watchdog fires
- Verify person detection overlay appears on camera feed when Hailo active
- Verify face PWA debug overlay and status text update correctly
- Verify speech bubble appears and dismisses on TTS
- Fix any broken, missing, or inconsistent UI elements found
- Ensure UI works on both desktop browser and mobile (Android Chrome)

Priority: medium — after watchdog expansion and HailoRT 5.2.0 upgrade

---

## Voice

### ✓ Increase TTS speech speed slightly — DONE 2026-06-07

- Web Speech API rate: 0.88 → 0.95 (face/index.html `_makeTtsUtterance()`)
- Piper TTS length_scale: 1.05 → 0.92 (config.yaml `tts.length_scale`; voice_engine.py reads from config, no code change)
- Config keys: `tts.length_scale` (Piper), `tts.web_speech_rate` (documentation only — face PWA reads constant directly)
- Service worker: rover-face-v34 → rover-face-v35
- Deployed and tested: Piper `/api/voice/speak` returned 200 OK

---

## AI / Voice

### Re-enable hailo-ollama alongside VLM (GenAI session multiplexing)

HailoRT 5.2.0 firmware only supports one GenAI session at a time. hailo-ollama
(voice agent LLM fast-path, ~3s) and VLM (scene description) cannot run simultaneously.
Current state: hailo-ollama disabled, voice agent falls back to CPU llama3.2:1b (~20-30s).

Options to investigate:
1. **Time-multiplexed sessions** — rover2-api opens a GenAI session per request and
   closes it immediately after, rather than holding it open. Requires changes to
   hailo-ollama and vlm_engine.py session lifecycle. Risk: session open/close overhead
   may add latency.
2. **Request queue with single session token** — a shared asyncio lock that hailo-ollama
   and vlm_engine.py both acquire before opening a GenAI session. One runs at a time,
   other waits. Simple but adds latency when both are requested concurrently.
3. **Hailo firmware update** — monitor Hailo developer zone for 5.x firmware that lifts
   the single-session limit. No code change needed if this lands.
4. **Dedicated session per model at startup, shared via ROUND_ROBIN** — check if
   ROUND_ROBIN scheduler used by body tracker applies to GenAI sessions in 5.2.0.
   May not — body tracker uses VDMA not GenAI.

Impact: voice agent response time 20-30s vs 3s. High priority for demo quality.
Priority: high

---

### Upgrade LLM from llama3.2:1b to Llama 3 8B

Current CPU fallback is llama3.2:1b (1B parameters, ~20-30s, mediocre quality).
Llama 3 8B would give significantly better instruction following, more natural conversation,
and better tool use — at the cost of longer response time on CPU (~60-90s) and higher RAM.

Prerequisites:
- Verify available RAM headroom: rover2-api RSS target <150 MB; Pi 5 has 8 GB total
- Confirm Ollama can pull and serve Llama 3 8B: `ollama pull llama3:8b`
- Benchmark response time: acceptable if <90s for non-time-critical queries (diagnostics, questions)
- The hailo-ollama fast-path (issue #26) would make 8B viable even for voice if re-enabled

Options:
1. **CPU-only 8B** — pull llama3:8b, update voice_engine/agent to use it; accept longer latency
2. **Quantised 8B** — use `llama3:8b-instruct-q4_0` (~4.7 GB) for better quality/speed tradeoff
3. **Wait for hailo-ollama (issue #26)** — 8B on Hailo would be <5s; better to fix GenAI sessions first

Priority: medium

---

### Natural language understanding, multilingual support, advanced reasoning

Improve the agent's ability to handle ambiguous, multilingual, and multi-step reasoning queries.

**NLU improvements:**
- Expand `_SPOKEN_FAST_PATTERNS` and `_FAST_PATTERNS` (chat_router.py / agent.py) to cover paraphrases, contractions, and common misspellings
- Add intent confidence scoring — route to LLM when regex match is weak rather than forcing a mismatch
- Add slot extraction for parameterised commands ("set speed to 180", "follow at distance 1 metre")
- Add disambiguation prompt when intent is ambiguous ("do you want detect-only or full follow?")

**Multilingual support:**
- faster-whisper already transcribes non-English; voice pipeline assumes English throughout
- Add language detection on transcribed text (langdetect, <1 MB)
- Route non-English to a multilingual model path or translate → English → respond → translate back
- Priority languages: French, German, Spanish (most likely users at office demos)

**Advanced reasoning tier (DeepSeek-R1 or equivalent):**
- For complex multi-step queries ("why is the CPU high and what should I do?"), invoke a reasoning model
- DeepSeek-R1:1.5b runs on Pi 5 CPU; benchmark against llama3.2:1b for diagnostic accuracy
- Add a third routing tier: fast-path → hailo-ollama → reasoning model (CPU, 60-90s acceptable for
  complex diagnostics where user expects to wait)
- Gate: only invoke reasoning tier if hailo-ollama returns low-confidence or the query contains
  multi-step logical connectives ("and", "because", "why", "what if")

Priority: low — do after structured output tool-calling is working

---

## Resource Optimisation

### Full resource audit — CPU, memory, battery, temperature

Review every component of ROVER2 for resource usage reduction opportunities.
Nothing is out of scope.

Areas to cover:

**CPU**
- Every background task, loop, and timer — is it event-driven or polling?
- Service startup order — anything initialising too early and holding CPU?
- Python asyncio task count — any redundant tasks?
- faster-whisper model — loaded permanently or on demand?
- Metrics collection interval — can it be reduced further?
- WebSocket telemetry push rate — can it be reduced when no client connected?
- Camera proxy — pulling frames when no client and tracking off?
- BLE scan interval — minimum viable?
- Any busy-wait or sleep(0) patterns?

**Memory**
- rover2-api base RSS — can it be reduced below 92 MB?
- faster-whisper model unload after N minutes voice inactivity
- Ollama KEEP_ALIVE already 1m — verify it is working
- Any large data structures held in memory unnecessarily?
- SQLite metrics DB — in-memory cache size
- Log ring buffer size

**Battery**
- wlan1 AP auto-toggle already implemented — verify saving
- RTL8812AU TX power — can it be reduced from 20 dBm when client nearby?
- Hailo frame_interval_s — already 1fps, review if lower is viable when not demoing
- arm_freq — already 1800 MHz, review if 1500 MHz is viable at idle
- Camera — can rover-camera.service be stopped entirely when tracking off and
  no stream client connected? (camera idle sleep partially covers this)
- USB devices — any drawing power unnecessarily?
- viking bank keepalive dongle — virtual USB dongle service, confirm only on battery

**Temperature**
- Hailo sustained inference temperature profile — log over a full follow session
- Pi 5 thermal throttle headroom — how much margin above 80°C throttle point?
- Physical placement — is Hailo HAT airflow adequate?
- Any software changes that could reduce sustained Hailo load temperature?

Outcome: produce a prioritised list of changes with estimated impact (high/medium/low)
for each. Implement only after review and approval — this is an audit task first.

Priority: high (standing goal per HANDOFF.md)

---

## Documentation

### Presentation booklet for IT middle management

Create a professional presentation booklet (PDF or slides) suitable for IT middle
management at a large enterprise IT company. Audience has technical awareness but
is not embedded in the project.

Content to cover:
- What ROVER2 is — overview and purpose
- What it does — capabilities (follow, voice, VLM, guard mode, HA integration)
- Key differentiator: fully local, no cloud, no data egress
- Architecture overview — Pi 5, Hailo AI HAT, on-device LLM/VLM, voice pipeline
- How it works — simplified flow diagrams (voice pipeline, follow pipeline, agent)
- Technology stack — hardware and software components
- Demo scenarios — office demo, home use, guard mode
- Roadmap / what comes next
- Any other angle that would be interesting to IT management (security, privacy,
  edge AI trend, potential enterprise applications)

Format: visually appealing, infographic-heavy, minimal dense text per slide/page.
Professional tone. No internal IP addresses, no credentials, no secrets.

Priority: medium

---

## Code Hygiene

### Delete all unused code and files

Audit the entire repo for dead code and unnecessary files:

- Unused Python imports in every pi/*.py file
- Dead functions or classes no longer called anywhere
- Commented-out code blocks that are no longer relevant
- Duplicate logic that has been superseded
- Old migration scripts or one-off setup scripts that have already been run
- Any files left over from ROVER v1 that are not used by ROVER2
- Test files or scratch files not part of the test suite
- Any __pycache__ or .pyc files tracked in git
- docs/ files that are fully superseded by HANDOFF.md
- Anything in scripts/ that is no longer referenced or needed

Process:
1. Audit and list everything found before deleting anything
2. Confirm each deletion is safe (not called, not referenced, not needed for setup)
3. Delete confirmed dead items
4. Run ./deploy_pi.sh and verify rover2-api starts cleanly after deletions
5. Check journalctl for any import errors
6. Commit with message "chore: remove dead code and unused files"

Priority: low — do after resource audit so audit findings may also identify dead code

---

## Agent / Architecture

### Structured output tool-calling on hailo-ollama — eliminate CPU fallback

hailo-ollama currently returns HTTP 500 when the `tools` parameter is passed (confirmed in hailo-ollama
benchmark 2026-06-06). This forces all tool-calling web chat queries to fall back to llama3.2:1b on CPU
(~120–150s warm with 38 tools), which is borderline for the 180s production timeout.

**Goal:** enable tool-calling on hailo-ollama so web chat tool queries run at 6.3 TPS on Hailo instead
of falling back to CPU.

**Approach options:**
1. **Structured output / constrained decoding** — pass a JSON schema to hailo-ollama using the
   `/api/generate` format parameter (`format: "json"` + schema). If supported, force model to emit
   valid tool-call JSON without the `tools` API. Parse response in `_converse_hailo()`.
2. **Prompt-engineered tool-calling** — include tool definitions as text in the system prompt, instruct
   the model to emit `{"tool": "...", "args": {...}}` JSON when a tool is needed. Parse with regex.
   More fragile but works with any model.
3. **Wait for hailo-ollama native tool support** — hailo-ollama is under active development; check
   release notes for `tools` parameter support before implementing workaround.

**Implementation plan (option 2 — prompt-engineered):**
- Reduce tool set to 10–15 essentials (do this first — see below)
- Serialise reduced tool list as plain text descriptions in `_HAILO_SYSTEM_WITH_TOOLS`
- Add JSON extraction + validation in `_converse_hailo()` response handler
- Route structured tool response through existing tool dispatch logic
- Fallback: if hailo response is not valid JSON tool-call, treat as plain text reply

**Prerequisites:**
- CPU fallback benchmark complete (done — keep llama3.2:1b)
- Reduce tool context (task below) — must have ≤15 tools before including them in Hailo prompt
- Verify hailo-ollama GenAI session isolation (issue #26) — VLM and hailo-ollama contention

Priority: high — eliminates the 120-150s CPU fallback latency for web chat tool queries

---

### Reduce CPU fallback tool context from 38 to 10-15 essential tools

`_call_model_cpu()` in pi/agent.py passes all 38 tools (`_ALL_TOOLS`) to llama3.2:1b. The resulting
context is ~3000–5000 tokens; prefill alone takes ~120s warm on Pi 5 ARM, leaving almost no margin
before the 180s production timeout.

**Goal:** reduce to 10–15 essential tools, cutting warm latency to ~20–25s.

**Analysis needed:**
- Read `_FAST_PATTERNS` (lines 1078–1089) — queries handled before LLM is called
- Read `_converse_hailo()` — queries handled by hailo-ollama (voice path)
- Identify which of the 38 tools are actually reachable via `_call_model_cpu` (web chat only)
- `_SPOKEN_TOOLS` (line 484) is confirmed dead code — delete it

**Candidate tools to keep (estimated):**
- `monitor_snapshot` — single-call system snapshot
- `get_diagnostics` — extended health
- `get_top_processes` — CPU investigation
- `list_parameters` / `set_parameters` — config read/write
- `get_hardware_reference` — wiring / port facts
- `get_robot_capabilities` — capabilities overview
- `describe_camera` — VLM describe
- `set_robot_mode` — follow/detect/off
- `restart_service` / `reboot_pi` — dangerous actions (keep with proposal guard)
- 2–3 HA tools if HA enabled

**Candidate tools to remove from CPU path:**
- Fine-grained metrics tools already covered by `monitor_snapshot`
- `get_network_interfaces` (covered by diagnostics)
- Any tool whose query is intercepted by `_FAST_PATTERNS` before reaching LLM

**Also:** remove `_SPOKEN_TOOLS` dead code from pi/agent.py line 484 in same PR.

Priority: high — prerequisite for structured output tool-calling on hailo-ollama

---

## Watchdog / System Maintenance

### Weekly software update check and safe auto-install

Add a weekly maintenance task to the watchdog that checks for available
updates and installs only what is safe to auto-apply. Everything else
is surfaced as an alert for manual review.

#### What runs weekly (Sunday 03:00 via asyncio scheduled task)

**Phase 1 — fetch update list (always safe, zero risk)**
```
sudo apt-get update
apt list --upgradable 2>/dev/null
```
Parse output and categorise every pending update:

| Category | Examples | Auto-install safe? |
|----------|----------|--------------------|
| Hailo packages | hailort, hailo-all, hailo-ai/* | NO — requires procedure |
| Kernel packages | linux-image-*, linux-headers-* | NO — DKMS rebuild risk |
| Python system packages | python3-*, pip packages | NO — pinned deps |
| DKMS modules | any dkms package | NO — manual rebuild needed |
| rover2 dependencies | turbojpeg, libcamera-* | NO — test first |
| Security-only patches | openssl, openssh-server, curl | YES — if not in above |
| General system packages | nano, git, rsync, htop, etc | YES — low risk |

**Phase 2 — alert on available updates (always)**
Emit WebSocket alert with full categorised update list:
```
severity: "info"
message: "Weekly update check: N updates available (X safe to auto-install,
          Y require manual review)"
action_taken: "none" or "installed X packages"
details: {
  "safe_to_install": ["package1=version", ...],
  "manual_review": ["hailort=5.3.0", "linux-image-6.18.34", ...],
  "held_back": ["package=version (reason)"]
}
```
Always show manual_review list prominently — these are the ones that matter.

**Phase 3 — auto-install safe packages (if any)**
Only if safe_to_install list is non-empty AND:
- rover2-api is idle (no active voice or follow session)
- CPU < 50% (not in the middle of something)
- Disk > 2 GB free

```
sudo apt-get install -y --only-upgrade <safe_packages>
```

After install:
- Verify rover2-api still responds: `curl -sk https://localhost:8082/api/status`
- If unhealthy → log critical, emit alert "POST-UPGRADE HEALTH CHECK FAILED"
- Log all installed packages with versions to journal with `[watchdog-update]` prefix

**Phase 4 — pip check (never install, always alert only)**
```
pip list --outdated 2>/dev/null | grep -v "^Package"
```
Alert with any outdated pip packages — never auto-upgrade pip packages.
Reason: requirements.txt has pinned versions for a reason (PyTurboJPEG<2.0,
hailo_platform==5.2.0, etc). Pip upgrades require manual testing.

**Phase 5 — Hailo-specific update check**
Check for HailoRT updates separately from general apt packages:
```
apt-cache policy hailort
# compare installed vs candidate version
```
If a newer HailoRT version is available:
- Do NOT auto-install — always manual per HAILORT_UPGRADE.md
- Emit a prominent alert:
  ```
  severity: "warning"
  message: "HailoRT update available: X.X.X → Y.Y.Y —
            check release notes for issue #26 fix (single GenAI session
            limit). If resolved: follow HAILORT_UPGRADE.md to upgrade."
  ```
- Include Hailo release notes URL in alert details:
  `https://community.hailo.ai/c/release-notes`

**Phase 6 — known issue package tracking**
For each known issue in HANDOFF.md that is blocked on a package update,
check whether the relevant package has a new version available and alert
prominently if so. Known issues to watch:

- **Issue #26:** hailort — single GenAI session limit
  Watch for: hailort >= 5.3.0 or any version above current
  Alert: "HailoRT update available — may resolve issue #26 (GenAI
          single-session limit). Check release notes before upgrading."

- **Issue #24:** linux-image — kernel upgrade knocks out DKMS modules
  Watch for: any linux-image-* update available
  Alert: "Kernel update available: X → Y — do NOT auto-install.
          After manual upgrade, verify DKMS rebuild:
          `sudo dkms autoinstall && lsmod | grep hailo`"

- **hailo_model_zoo_genai:** watch for new LLM models becoming available
  Check: `curl -s http://localhost:8000/hailo/v1/list | python3 -m json.tool`
  Compare model list against last known list stored in
  `/opt/rover2/last_hailo_models.json`
  If new models detected:
  Alert: "New Hailo LLM models available: \<model_names\> —
          consider upgrading hailo_model_zoo_genai for better LLM quality"
  Update `/opt/rover2/last_hailo_models.json` after alerting

Same principle applies to any future known issues added to HANDOFF.md —
if a package update corresponds to a known issue, flag it prominently
rather than listing it as a generic available update.

---

#### Packages to permanently hold (never auto-upgrade)

Create `/etc/apt/preferences.d/rover2-hold` during initial setup
(not on every watchdog cycle — check if file exists first):
```
Package: hailort
Pin: version *
Pin-Priority: -1
Package: hailo-all
Pin: version *
Pin-Priority: -1
Package: linux-image-*
Pin: version *
Pin-Priority: -1
Package: linux-headers-*
Pin: version *
Pin-Priority: -1
```
These packages will not appear in `apt upgrade` output at all.
Hailo upgrades follow HAILORT_UPGRADE.md procedure only.
Kernel upgrades are manual only — DKMS rebuild verification required.

---

#### Config (pi/config.yaml)
```yaml
watchdog:
  update_check:
    enabled: true
    schedule: "sunday_03:00"
    auto_install_safe: true   # set false to alert-only, never install
    notify_manual_review: true
    known_issue_tracking: true
```

---

#### Web UI additions (DIAG tab, watchdog section)
Add the following rows to the existing watchdog section in the DIAG tab:
- Last update check: \<timestamp\> or "never"
- Safe packages installed: \<count\> (\<date\>) or "none"
- Pending manual review: \<count\> — expandable list on click
- HailoRT: \<installed version\> / \<available version\> or "up to date"
- New Hailo models: \<count\> or "none"
- Next scheduled check: \<date\>

---

#### Implementation notes

**Scheduling:**
Use asyncio inside the watchdog loop — no cron, no systemd timer:
- On watchdog start, calculate seconds until next Sunday 03:00 local time
- `asyncio.sleep()` for that duration
- Run check, then sleep 7 days
- If Pi reboots mid-week, next boot recalculates time to next Sunday 03:00

**Safety rules (non-negotiable):**
- Never run `apt-get upgrade` (upgrades everything)
- Always use `apt-get install --only-upgrade <specific packages>`
- Never touch hailort, linux-image-*, linux-headers-*, python3-* automatically
- Always verify rover2-api health after any install
- Never install during active voice or follow session
- Never install if CPU > 50% or disk < 2 GB

**Logging:**
All update check actions log to journal with `[watchdog-update]` prefix:
```
[watchdog-update] Weekly check started
[watchdog-update] 3 safe packages found: curl=8.x, openssl=3.x, git=2.x
[watchdog-update] 2 manual review items: hailort=5.3.0, linux-image-6.19
[watchdog-update] HailoRT update available: 5.2.0 → 5.3.0
[watchdog-update] Installed: curl=8.x openssl=3.x — rover2-api health OK
[watchdog-update] Next check: 2026-06-14 03:00
```

**State persistence:**
Store last check results in `/opt/rover2/last_update_check.json`:
```json
{
  "last_check_ts": "<ISO>",
  "safe_installed": ["pkg=version", ...],
  "manual_review": ["pkg=version", ...],
  "hailo_update_available": "<version or null>",
  "next_check_ts": "<ISO>"
}
```
Read on rover2-api startup to populate DIAG tab immediately without
waiting for first weekly cycle.

**Watchdog must NEVER:**
- Reboot the Pi after updates
- Upgrade hailort automatically
- Upgrade kernel automatically
- Upgrade pip packages automatically
- Run `apt-get upgrade` without a package list
- Install anything if rover2-api health check fails post-install

Priority: low — after issue #26, resource audit, and formal follow tests
