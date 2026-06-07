# ROVER2 Backlog

Items here are confirmed desirable but not yet scheduled.
Format: ## Category / ### Item / bullet details / Priority line.

---

## Code Quality

### ✓ Full code hygiene audit — DONE 2026-06-07

- Read all 31 pi/*.py files and face/index.html in full before any changes
- Removed `current_holder()` and `is_locked()` from `pi/hailo_session.py` (defined but never called)
- Removed dead first `_days_remaining()` definition from `scripts/renew-cert.sh` (shadowed by second definition; first had broken `${CERT_OUT}` in single-quoted heredoc)
- Added `*.tmp`, `last_update_check.json`, `last_hailo_models.json` to `.gitignore`
- Kept (ambiguous): `watchdog.py` `_hailo_500_streak` ("kept for compat" comment), `agent.py` `_SPOKEN_TIMEOUT_S` (no callers but clear intent comment)
- No dead imports, no dead functions, no stale hailo-ollama HTTP calls, no webkitSpeechRecognition anywhere
- `python3 -m py_compile` passed on all modified Python files

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

### Präsentation — ROVER2 für Papa (Deutsch, ~78 Jahre, Hardware-technisch)

A polished PDF in the style of a quality German engineering magazine
(think Der Spiegel Wissen or a Spektrum der Wissenschaft article).
Informed, respectful, explains new concepts once with a good analogy
then moves on. Large photos, purposeful layout, meaningful numbers.

**Audience:** 78-year-old German-speaking father. Hardware-technical
(understands electronics, circuits, chips, motors, sensors, engineering
effort). Not software-technical. From a generation before the internet.
Son will fill gaps in person — PDF just needs to tell the story clearly
and make him proud.

**Tone:** warm, personal, like a quality magazine article written by
someone who loves the subject. Not corporate. Not a spec sheet.
Not condescending. Intelligent but accessible.

**Language:** German throughout. Technical terms only where they add
meaning and are immediately explained. English only for proper names
(ROVER2, Raspberry Pi, AI, Hailo).

**Format:**
- PDF, A4 portrait
- 8-10 pages
- Large text (minimum 12pt body, 16pt+ headings) — accessible for older reader
- Magazine-style layout — large photos, white space, pull quotes
- Warm colour scheme — not cold corporate blue
- Use ODT as intermediate format, export to PDF

**Photos needed (collect before building):**
- docs/images/rover2_physical.png — robot upright, clean background
- docs/images/rover2_face.png — A32 face display glowing (already have this screenshot)
- docs/images/rover2_following.png — robot following a person if available
- docs/images/rover2_hardware.png — top-down showing all components

If photos not available, use placeholder boxes with captions.

---

**Content structure — 8-10 pages:**

**Seite 1 — Einleitung (personal)**
Large hero photo of ROVER2 with face display active.
Short personal paragraph — what this is, why it was built.
"ROVER2 ist ein autonomer Roboter, den ich von Grund auf selbst
gebaut und programmiert habe — Hardware, Elektronik und die gesamte
Software. Er kann sehen, hören, sprechen, sich selbst bewegen und
Entscheidungen treffen. Alles läuft lokal, auf seiner eigenen Hardware —
kein Internet, kein Server, keine monatlichen Kosten."

**Seite 2 — Der Roboter (hardware overview with labels)**
Full-page photo of physical robot with labeled callouts:
- Raspberry Pi 5 → "Das Gehirn"
- Hailo AI HAT+ 2 → "Der KI-Prozessor"
- Samsung Galaxy A32 → "Das Gesicht"
- Makeblock Chassis → "Das Skelett"
- MegaPi Controller → "Die Muskeln"
- Ultrasonic Sensor → "Die Ohren für Hindernisse"
- Camera → "Die Augen"
- Powerbank → "Das Herz / Energiequelle"
- RTL8812AU Dongle → "Das eigene WLAN"

**Seite 3 — Das Gehirn**
Explain Pi 5 + Hailo in plain terms with one key analogy:
"Der Raspberry Pi 5 ist ein vollständiger Computer — so groß wie
eine Handfläche, aber leistungsfähig genug um ein komplettes
Betriebssystem zu betreiben."

"Der Hailo AI HAT+ 2 ist ein spezieller Prozessor, der nur für
künstliche Intelligenz entwickelt wurde. Er schafft 26 Billionen
Rechenoperationen pro Sekunde — und verbraucht dabei weniger Strom
als eine Glühbirne. Früher brauchte man dafür ein ganzes Rechenzentrum."

Include simple comparison:
| | ROVER2 | Rechenzentrum (früher) |
|-|--------|----------------------|
| KI-Leistung | 26 TOPS | Vergleichbar |
| Stromverbrauch | ~15 Watt | ~10.000 Watt |
| Kosten | ~550 € | ~50.000 € |
| Internetverbindung | Nicht nötig | Erforderlich |

**Seite 4 — Die Sinne**
Four sections with simple icons or photos:

SEHEN — "Eine Kamera filmt kontinuierlich. Ein KI-Modell erkennt
in Echtzeit Personen im Bild — Position, Abstand, Bewegungsrichtung.
Alles auf dem Hailo-Chip, 4 Mal pro Sekunde."

HÖREN — "Ein Mikrofon hört permanent zu. Sobald das Wort 'Rover'
erkannt wird, beginnt der Roboter zuzuhören. Die Spracherkennung
läuft vollständig lokal — keine Verbindung zu Google oder Amazon."

SPRECHEN — "Eine KI-Stimme antwortet in natürlichem Englisch.
Die Stimme wird in Echtzeit auf dem Gerät erzeugt — kein
Text-to-Speech-Dienst, keine Cloud."

FÜHLEN — "Ein Ultraschallsensor misst den Abstand zu Hindernissen
20 Mal pro Sekunde. Erkennt er etwas näher als 40 cm, stoppt der
Roboter automatisch."

**Seite 5 — Was er kann**
Capability list with screenshots where available:

✓ Person erkennen und folgen
  "Er erkennt eine Person mit seiner Kamera und folgt ihr durch
  den Raum — hält automatisch Abstand, weicht Hindernissen aus."

✓ Sprachbefehle verstehen
  "Sag 'Rover, wie geht es dir?' — er antwortet mit einer
  Diagnose seiner eigenen Systeme. Vollständig offline."

✓ Sehen und beschreiben
  "Auf die Frage 'Was siehst du?' beschreibt er die Szene
  vor seiner Kamera in natürlicher Sprache."

✓ Smart Home steuern
  "Er ist mit dem Smart Home verbunden und kann Lichter,
  Geräte und Sensoren steuern — per Sprachbefehl."

✓ Sich selbst überwachen und reparieren
  "Ein integriertes Überwachungssystem prüft alle 30 Sekunden
  den Zustand aller Komponenten und behebt Probleme automatisch —
  ohne dass jemand eingreifen muss."

✓ Immer erreichbar
  "Er hat sein eigenes WLAN-Netzwerk. Auch ohne Heimnetzwerk
  ist er jederzeit steuerbar."

**Seite 6 — Die KI (simple explanation)**
"Künstliche Intelligenz klingt kompliziert — ist aber im Grunde
ein sehr ausgefeiltes Muster-Erkennungssystem."

Three simple analogies:

Spracherkennung: "Wie ein sehr geübter Stenograph — hört,
erkennt Muster, schreibt mit."

Sprachmodell (LLM): "Wie ein sehr belesener Assistent, der
Millionen von Texten gelesen hat und dadurch sinnvolle Antworten
formulieren kann."

Bilderkennung (VLM): "Wie ein Mensch, der gelernt hat was
'Stuhl', 'Tisch', 'Person' bedeutet — und es sofort erkennt."

"Der entscheidende Unterschied zu kommerziellen Produkten wie
Alexa oder Siri: Alles passiert im Gerät selbst. Kein einziges
Wort verlässt den Roboter."

**Seite 7 — Die Zahlen**
Hardware cost breakdown — visual (simple table or graphic):

| Komponente | Zweck | Kosten |
|------------|-------|--------|
| Raspberry Pi 5 8GB | Hauptcomputer | ~90 € |
| Hailo AI HAT+ 2 | KI-Prozessor | ~110 € |
| Makeblock Ultimate 2.0 | Chassis + Motoren | ~180 € |
| Samsung Galaxy A32 | Gesicht + Audio | ~80 € |
| Viking Powerbank 65W | Stromversorgung | ~60 € |
| Kleinteile, Kabel, Dongle | — | ~30 € |
| **Gesamt** | | **~550 €** |

Note: verify costs against actual purchases before finalising.

Development effort:
"Entwicklungszeit: über 200 Stunden
 Laufende Kosten: 0 € (kein Internet, keine Abonnements)
 Stromverbrauch: ~15 Watt (weniger als eine Schreibtischlampe)"

**Seite 8 — Was noch kommt**
Short, simple, forward-looking:
"ROVER2 ist fertig — aber nie wirklich fertig."

- Personen wiedererkennen und beim Namen nennen
- Auf Deutsch antworten
- Sich an Vorlieben und Gewohnheiten erinnern
- Noch bessere KI wenn neue Chips verfügbar

**Seite 9 — Persönliche Notiz**
Short, warm, personal paragraph from the builder to the father.
Leave a placeholder: [PERSÖNLICHE NOTIZ — wird vom Autor ergänzt]
This page is for a handwritten or personally typed note —
do not generate the content, just create the space for it.

---

**Technical implementation:**
- Use Python with odfpy or python-docx for ODT generation
- Read skill file first: /mnt/skills/public/docx/SKILL.md
- Export to PDF via LibreOffice headless:
  libreoffice --headless --convert-to pdf presentation.odt
- Save to /mnt/user-data/outputs/ROVER2_Praesentation.pdf
- If photos exist in docs/images/, embed them
- If photos missing, insert placeholder grey boxes with captions

**Timing:** needed by 2026-06-14 (one week)
Priority: high — hard deadline

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

---

## Web UI — Primary Control Interface

### Web UI is the primary interaction surface — must always be complete and current

The web UI at https://rover-ip:8082/ is the main way ROVER2 is controlled
and monitored. It must reflect the current state of the robot at all times
and expose every capability that exists.

#### Standing rule (applies to every future Cline session)

Every new feature, endpoint, or capability added to rover2-api must be
accompanied by a web UI update in the same commit. No feature is complete
until it is accessible and visible in the web UI.

Checklist for every new feature:
- Is it visible in the STATUS table or DIAG tab?
- Can it be controlled from the UI (if it has controllable state)?
- Does it update live via WebSocket telemetry or auto-refresh?
- Is it accessible on mobile (Android Chrome)?
- Are errors and edge cases shown clearly (not silent failure)?

#### Current known gaps to address (in priority order)

**1. Voice pipeline visibility** ✅ DONE 2026-06-07
- DIAG VOICE PIPELINE panel: STATE, VAD, TTS, SESSION, WHISPER, LAST STT, STT LATENCY, WAKE LOGPROB
- `GET /api/voice/status` includes `vad_active`, `tts_active`
- WebSocket telemetry includes `vad_active`, `tts_active`

**2. LLM / agent visibility**
- Current: agent stats panel exists but limited
- Needed:
  - Current routing tier: fast-path / hailo-tools / cpu-fallback
  - Last query and response (truncated, last 1 only)
  - Hailo GenAI session state: locked / free
  - VLM cooldown remaining (already in /api/agent/stats — verify visible)
  - Tool call history: last 5 tool calls with latency
  - Success/fallback rate over last 20 calls

**3. Memory and resource live view** ✅ DONE 2026-06-07
- DIAG RESOURCES LIVE panel: PI TEMP, DISK, rover2-api RSS+CPU, hailo-ollama, ollama — auto-refresh 5s
- `GET /api/system/resources`

**4. Full follow pipeline control** ✅ DONE 2026-06-07
- DIAG FOLLOW PIPELINE panel: detection conf, person, mode, fps, infer latency, safety, BLE RSSI sparkline
- Manual override buttons: STOP, DETECT, FOLLOW, BLE
- `GET /api/follow/status`

**5. Home Assistant integration panel** ✅ DONE (previous session)
- DIAG HOME ASSISTANT panel with entity toggles and temperatures

**6. Voice command console** ✅ DONE (previous session)
- CHAT tab VOICE COMMAND CONSOLE panel

**7. Config editor** ✅ DONE 2026-06-07
- TOOLS tab CONFIG panel shows config.yaml values (read-only view)
- TOOLS tab FOLLOW TUNING panel — editable sliders for key follow params → POST /api/config/tuning

**8. Log viewer** ✅ DONE 2026-06-07
- DIAG LOG VIEWER panel: service selector, lines selector, filter input, colour-coded output
- `GET /api/diagnostics/journal?service=&lines=&filter=`

**9. Watchdog action history** ✅ DONE 2026-06-07
- DIAG WATCHDOG RECENT ACTIONS section shows last 10 auto-fix actions with timestamps
- `GET /api/watchdog/status` now includes `action_history` field
- `watchdog.py` maintains `_action_history: deque(maxlen=10)` ring buffer

**10. Alert history** ✅ DONE 2026-06-07
- HISTORY button in alert bar → modal with last 50 alerts + CLEAR button
- `GET /api/alerts/history`, `DELETE /api/alerts/history`
- `server.py` maintains `_alert_history: deque(maxlen=50)` ring buffer

#### UI quality standards

Every panel in the web UI must meet these standards:
- Live data: updates without page refresh (WebSocket or 5s poll)
- Error state: shows clearly when data unavailable (not blank, not stale)
- Mobile: works on Android Chrome at 375px width
- Loading: shows spinner or "loading..." for operations > 500ms
- Confirmation: dangerous actions (restart, reboot, stop service) require
  a confirm dialog before executing
- Consistency: all panels use same visual language (colours, fonts, spacing)

#### Process for keeping UI current

After every Cline session that adds a feature:
- Check the web UI gap list above
- If the new feature adds something not yet in the UI, add it
- Update this backlog entry to mark gaps as resolved
- The UI is never "done" — it grows with the robot

Priority: high — ongoing, applied to every future session
