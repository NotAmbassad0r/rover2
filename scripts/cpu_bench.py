#!/usr/bin/env python3
"""CPU fallback LLM benchmark — ROVER2 agent.

Tests tool-use capability for: llama3.2:1b (baseline), gemma3:1b, gemma3:2b, qwen2.5:3b
Verbatim tool definitions and system prompt from pi/agent.py (HA-enabled, 38 tools).

Run as: python3 /tmp/cpu_bench.py
Requires: sudo access (to stop/start services), ollama on 127.0.0.1:11434.

rover2-api is stopped for the duration of the benchmark to prevent llama3.2:3b
keepalive pings from interfering with model scheduling. It is restarted at the end.

Decision criteria (all must pass to beat baseline):
  - PASS gate: model returns tool_calls (not just text) when given full 38-tool set
  - precision > baseline (llama3.2:1b score)
  - warm latency < 15 s
  - RSS < 1536 MB (1.5 GB)
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

OLLAMA_BASE = "http://127.0.0.1:11434"
TIMEOUT = 120

# ── Tool definitions (verbatim from pi/agent.py _ALL_TOOLS with HA enabled) ──

ALL_TOOLS = [
    # ── READ_TOOLS (31) ──
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Current robot status: websocket, serial, motors, camera, safety, tracking, BLE, uptime.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_processes",
            "description": "Top 12 processes by CPU right now with PID, name, full command, cpu_pct, mem_mb. "
                           "Always call this first when diagnosing high CPU or unknown load.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diagnostics",
            "description": "Full system diagnostics: CPU%, temperature, RAM, disk, network rates, rover2-api process stats, all service checks.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logs",
            "description": "Last N log lines from rover2-api (the main robot service).",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "Lines (max 80)", "default": 40},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_journal",
            "description": "Read systemd journal for any named service. "
                           "Use for: rover-camera, ollama, rover2-api, cpu-governor, bluetooth, NetworkManager, ssh.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "systemd service name (without .service)"},
                    "lines":   {"type": "integer", "default": 40},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "Status (active/inactive/failed/enabled/disabled) for all rover-related services: "
                           "rover2-api, rover-camera, ollama, cpu-governor, bluetooth, NetworkManager, ssh, "
                           "rover2-powerbank-keepalive, rover2-virtual-usb-dongle, hailo-ollama.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metric",
            "description": "Historical time-series for one metric. "
                           "Metric names: cpu_percent, temp_cpu_thermal, ram_percent, ram_used_mb, "
                           "process_memory_mb, process_cpu_percent, ultrasonic_cm, ble_rssi, "
                           "follow_state, person_detected, safety_blocked, disk_percent, "
                           "net_eth0_tx_bps, net_eth0_rx_bps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric":  {"type": "string"},
                    "minutes": {"type": "integer", "default": 15},
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wifi_info",
            "description": "WiFi connection: SSID, signal strength, IP addresses, default gateway, link details.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_disk_details",
            "description": "Disk usage: per-partition stats (total/used/free GB, %) plus du -sh for /opt/rover2, /opt/rover, /var/log, /tmp.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ping_host",
            "description": "Ping a host 3 times and return reachability, packet loss, RTT. "
                           "Use to diagnose network issues. Default host is the Pi's gateway.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "default": "192.168.70.1"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_hailo",
            "description": "Hailo AI chip status: hailo_platform installed, HEF files present, "
                           "body_tracker ready, VLM engine status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_camera_health",
            "description": "Camera service health: is rover-camera streaming, last frame age, HTTP status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_db_stats",
            "description": "Metrics database: SQLite file size, number of tracked metrics, latest sample values.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_robot_capabilities",
            "description": "Full list of robot features, supported modes, and available API endpoints. "
                           "Use this when asked what the robot can do.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_alerts",
            "description": "Current active threshold alerts: high temperature, high CPU, high RAM, low disk. "
                           "Returns empty list when everything is within safe limits.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_parameters",
            "description": "All tunable config parameters with types and min/max. Call before set_parameters.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": "Current config.yaml (secrets masked).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_parameters",
            "description": "Change one or more parameters and save to config.yaml (live apply). "
                           "Use list_parameters for valid keys. "
                           "Example patch: {body_tracker: {turn_speed: 90}, safety: {safe_distance_cm: 35}}",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "object",
                        "description": "section name -> {key: value}",
                    },
                },
                "required": ["patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "monitor_snapshot",
            "description": "One-shot health: robot status + full diagnostics (CPU, temp, RAM, disk, network) + top processes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hardware_reference",
            "description": "ROVER2 hardware/software reference: ports, IPs, services, BLE, Hailo, power notes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_camera",
            "description": "VLM describes what the camera sees. Optional custom prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "default": "Describe what you see in front of the robot."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_diagnostics",
            "description": "Force fresh diagnostics scan (same as RUN DIAGNOSTICS in web UI).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_status",
            "description": "Live status of the 5 key services (rover2-api, rover-camera, "
                           "rover2-virtual-usb-dongle, rover2-powerbank-keepalive, hailo-ollama) "
                           "plus current temperature, throttle state, and CPU frequency.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_thermal_status",
            "description": "Current CPU temperature, throttle state (current + since-boot), "
                           "and CPU frequency in MHz. Use when diagnosing heat, throttling, "
                           "or frequency scaling issues.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_robot_mode",
            "description": "Change the robot's operating mode: "
                           "follow (camera YOLO follow), detect (detect-only no movement), off (idle).",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["follow", "detect", "off"]},
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wave_arm",
            "description": "Wave the robot arm as a greeting gesture. Use when greeted or when owner says hello, hi, or wave.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_diagnostics",
            "description": "Full system snapshot: robot status + CPU/temp/RAM/disk/network + top processes. "
                           "Use for 'how are you', 'system check', 'run diagnostics', 'full check'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hailo_status",
            "description": "Hailo AI chip and body tracker status: platform available, HEF loaded, "
                           "body_tracker ready, VLM engine status, current temperature.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak_diagnostics_summary",
            "description": "Fetch live system status and services, return compact spoken summary. "
                           "Use for 'how are you', 'status check', 'all good?', 'are you okay?'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_fix",
            "description": "Fetch current diagnostics and propose or apply fixes. "
                           "Safe fixes (rover-camera restart) applied automatically. "
                           "Risky fixes (rover2-api restart, reboot, config changes) returned as proposals. "
                           "Use for 'fix it', 'what is wrong', 'any problems'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_follow_mode",
            "description": "Enable, disable, or change the person-follow mode. "
                           "enable=start camera YOLO follow. disable=stop all follow. "
                           "detect=detect-only no movement. fused=BLE+camera fused follow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["enable", "disable", "camera", "fused", "detect"],
                    },
                },
                "required": ["action"],
            },
        },
    },
    # ── HA_READ_TOOLS (2) ──
    {
        "type": "function",
        "function": {
            "name": "ha_get_status",
            "description": (
                "Get Home Assistant status — all lights, switches, sensors, "
                "temperature readings, and media players."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_get_temperatures",
            "description": "Get all room temperatures from Home Assistant sensors.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── DANGEROUS_TOOLS (4) ──
    {
        "type": "function",
        "function": {
            "name": "restart_service",
            "description": "Restart rover2-api.service. Use only when there is clear evidence the service is stuck or crashed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_service",
            "description": "Stop a named service. Useful for stopping rogue or unnecessary services (e.g. rover-camera v1 if it's consuming CPU). "
                           "Allowed: rover-camera, ollama, rover2-powerbank-keepalive, rover2-virtual-usb-dongle, hailo-ollama, bluetooth.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "reason":  {"type": "string"},
                },
                "required": ["service", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_service",
            "description": "Start a named service that is currently stopped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "reason":  {"type": "string"},
                },
                "required": ["service", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reboot_pi",
            "description": "Reboot the Raspberry Pi. Absolute last resort only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
    # ── HA_DANGEROUS_TOOLS (1) ──
    {
        "type": "function",
        "function": {
            "name": "ha_toggle",
            "description": (
                "Toggle a Home Assistant entity on or off. "
                "Use for lights, switches, power strips, and plugs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "HA entity_id, e.g. light.office_go or switch.hue_smart_plug_1",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
]

# ── System prompt (verbatim from pi/agent.py _SYSTEM_PROMPT, HA-enabled 38 tools) ──
_TOOL_NAMES = ", ".join(t["function"]["name"] for t in ALL_TOOLS)
SYSTEM_PROMPT = f"""You are ROVER2's onboard engineer: monitor hardware and software, change any allowed parameter, answer related questions.

TOOLS: {_TOOL_NAMES}

WORKFLOW:
1. Monitoring (CPU, temp, RAM, disk, serial, camera, follow, BLE): call monitor_snapshot() OR get_diagnostics() + get_top_processes().
2. Change settings: call list_parameters() then set_parameters(patch) with one or more sections.
3. Hardware/software facts (ports, IPs, wiring, services): call get_hardware_reference() and/or get_robot_capabilities().
4. Vision: describe_camera() for what the camera sees.
5. Robot control: set_robot_mode(follow|detect|off). Drive/grip/arm via status only unless user asks to change modes.

RULES:
- ALWAYS use tools for live data. Never invent temperatures, CPU %, or RSSI.
- Temperature: read system.temperature or extended temps from get_diagnostics — warn above 80 C, critical above 85 C.
- CPU high: name top process pid, name, cpu_pct from get_top_processes.
- After finding a fixable config issue, call set_parameters — do not only suggest edits.
- Dangerous actions (restart_service, reboot_pi, stop_service): use proposal only.
- Plain text, short paragraphs. No markdown.
- Always end with concrete suggested fixes (what to stop, restart, or change)."""

# ── T1-T8 test cases ─────────────────────────────────────────────────────────
# Queries bypass chat_router.py fast-path AND _SPOKEN_FAST_PATTERNS.
# Expected tool is the FIRST tool the model should call (some could accept alternates).
TESTS = [
    # label         query (bypasses all fast-paths)                                 expected_first_tool        alternates
    ("T1_diag",   "Something seems wrong with performance. Please investigate.",    "get_full_diagnostics",    {"monitor_snapshot", "get_diagnostics", "get_top_processes"}),
    ("T2_ha_temp","Are any rooms in the house currently overheated?",               "ha_get_temperatures",     set()),
    ("T3_vlm",    "What objects are visible to the robot's camera right now?",      "describe_camera",         set()),
    ("T4_ha_tog", "Switch the office lamp on.",                                     "ha_toggle",               set()),
    ("T5_multi",  "Run a comprehensive health scan of all robot subsystems.",       "get_full_diagnostics",    {"monitor_snapshot", "get_diagnostics"}),
    ("T6_disk",   "How much storage space is left on the Pi's drive?",              "get_disk_details",        set()),
    ("T7_cpu",    "What's consuming the most processor time at this moment?",       "get_top_processes",       {"get_diagnostics"}),
    ("T8_none",   "Tell me about your own design and operating purpose.",            None,                      set()),
]

CANDIDATES = ["llama3.2:1b", "gemma3:1b", "gemma3:2b", "qwen2.5:3b"]


def svc(cmd, name, timeout=30):
    """Run sudo systemctl <cmd> <name>."""
    r = subprocess.run(["sudo", "systemctl", cmd, name],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0


def ollama_post(path, data, timeout=TIMEOUT):
    url = OLLAMA_BASE + path
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def wait_ollama_ready(max_wait=30):
    """Return True when /api/tags responds."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def pull_model(model):
    """Pull model; skip if exact name already present."""
    print(f"  Checking {model}...", flush=True)
    resp = ollama_post("/api/tags", {}, timeout=10)
    names = [m.get("name", "") for m in resp.get("models", [])]
    if model in names:
        print(f"  Already present.", flush=True)
        return True
    print(f"  Pulling {model} from registry...", flush=True)
    proc = subprocess.run(
        ["ollama", "pull", model],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        print(f"  PULL FAILED: {proc.stderr[:200]}", flush=True)
        return False
    print(f"  Pulled OK.", flush=True)
    return True


def unload_model(model):
    """Unload model immediately (keep_alive=0)."""
    ollama_post("/api/generate", {
        "model": model, "prompt": "", "stream": False, "keep_alive": 0,
    }, timeout=20)
    time.sleep(2)


def gate_test(model):
    """Gate: does model return tool_calls when given full 38-tool set?"""
    print("  Gate test (full tool set)...", flush=True)
    resp = ollama_post("/api/chat", {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": "Call get_status now."},
        ],
        "tools": ALL_TOOLS,
        "stream": False,
    }, timeout=TIMEOUT)
    if "error" in resp:
        print(f"  GATE FAIL (error: {resp['error'][:120]})", flush=True)
        return False
    tool_calls = resp.get("message", {}).get("tool_calls") or []
    if tool_calls:
        names = [tc["function"]["name"] for tc in tool_calls]
        print(f"  GATE PASS — tool_calls: {names}", flush=True)
        return True
    content = resp.get("message", {}).get("content", "")
    print(f"  GATE FAIL — no tool_calls. Reply: {content[:80]}", flush=True)
    return False


def precision_test(model):
    """T1-T8 precision suite. Returns (score, rows)."""
    score = 0
    rows = []
    for label, query, expected, alternates in TESTS:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": query},
        ]
        t0 = time.time()
        resp = ollama_post("/api/chat", {
            "model": model,
            "messages": messages,
            "tools": ALL_TOOLS,
            "stream": False,
        }, timeout=TIMEOUT)
        elapsed = time.time() - t0

        if "error" in resp:
            got = None
            err = resp["error"][:50]
            ok = False
        else:
            tool_calls = resp.get("message", {}).get("tool_calls") or []
            got = tool_calls[0]["function"]["name"] if tool_calls else None
            err = ""
            ok = (got == expected) or (expected is None and got is None) or (got in alternates)

        if ok:
            score += 1
        status = "PASS" if ok else "FAIL"
        got_str = f"{got}" + (f" ERR:{err}" if err and got is None else "")
        rows.append((label, expected, got_str, status, elapsed))
        print(f"    {label:12s} exp:{str(expected):32s} got:{got_str:32s} {status}({elapsed:.1f}s)", flush=True)
    return score, rows


def latency_test(model):
    """3 timed runs with a neutral query. First run warm (post-precision). Returns (run1, run2, run3)."""
    times = []
    for i in range(3):
        t0 = time.time()
        resp = ollama_post("/api/chat", {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": "Is the CPU temperature within safe limits right now?"},
            ],
            "tools": ALL_TOOLS,
            "stream": False,
        }, timeout=TIMEOUT)
        elapsed = time.time() - t0
        times.append(elapsed)
        tool_calls = resp.get("message", {}).get("tool_calls") or []
        got = tool_calls[0]["function"]["name"] if tool_calls else "text"
        err = resp.get("error", "")
        print(f"    run{i+1}: {elapsed:.1f}s → {got}" + (f"  ERR:{err[:40]}" if err else ""), flush=True)
    warm_avg = sum(times[1:]) / max(len(times) - 1, 1)
    return times[0], warm_avg


def rss_mb():
    """Current RSS of ollama serve process in MB."""
    try:
        proc = subprocess.run(["pgrep", "-f", "ollama serve"], capture_output=True, text=True)
        pids = proc.stdout.strip().split()
        if not pids:
            return -1
        pid = pids[0]
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


# ── Main ──────────────────────────────────────────────────────────────────────

print(f"\n{'='*65}")
print(f"ROVER2 CPU fallback LLM benchmark — {time.strftime('%Y-%m-%d %H:%M')}")
print(f"Candidates: {', '.join(CANDIDATES)}")
print(f"Tests: {len(TESTS)}  Tools: {len(ALL_TOOLS)}")
print(f"{'='*65}")

# Stop rover2-api to prevent llama3.2:3b keepalive pings from interfering.
print("\nStopping rover2-api (prevents 3b keepalive interference)...", flush=True)
svc("stop", "rover2-api")
time.sleep(2)

# (Re)start ollama clean
print("Restarting ollama clean...", flush=True)
svc("stop", "ollama")
time.sleep(3)
svc("start", "ollama")
if not wait_ollama_ready(30):
    print("FATAL: ollama did not start within 30s", flush=True)
    svc("start", "rover2-api")
    sys.exit(1)
print(f"ollama ready. RSS baseline: {rss_mb()} MB", flush=True)

results = []   # (model, status, score, warm1, warm_avg, rss)

for model in CANDIDATES:
    print(f"\n{'─'*65}", flush=True)
    print(f"MODEL: {model}", flush=True)
    print(f"{'─'*65}", flush=True)

    if not pull_model(model):
        results.append((model, "PULL_FAILED", None, None, None, None))
        continue

    if not gate_test(model):
        unload_model(model)
        results.append((model, "GATE_FAIL", None, None, None, None))
        continue

    rss = rss_mb()
    print(f"  RSS with model loaded: {rss} MB", flush=True)

    print(f"\n  Precision tests (T1-T8):", flush=True)
    score, _ = precision_test(model)
    print(f"  Score: {score}/8", flush=True)

    print(f"\n  Latency (3 warm runs):", flush=True)
    run1, warm_avg = latency_test(model)
    print(f"  run1={run1:.1f}s  warm_avg={warm_avg:.1f}s", flush=True)

    results.append((model, "OK", score, run1, warm_avg, rss))

    print(f"\n  Unloading {model}...", flush=True)
    unload_model(model)
    rss_after = rss_mb()
    print(f"  Unloaded. RSS: {rss_after} MB", flush=True)

# ── Stop ollama and restart rover2-api ───────────────────────────────────────
print(f"\n{'─'*65}", flush=True)
print("Stopping ollama...", flush=True)
svc("stop", "ollama")
time.sleep(3)
print(f"RSS after stop: {rss_mb()} MB", flush=True)
print("Restarting rover2-api...", flush=True)
svc("start", "rover2-api")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print("BENCHMARK SUMMARY")
print(f"{'='*65}")
hdr = f"{'Model':20s} {'Status':12s} {'Score':8s} {'Run1(s)':9s} {'WarmAvg':9s} {'RSS(MB)':8s}"
print(hdr)
print("─" * len(hdr))

baseline_score = None
for model, status, score, run1, warm_avg, rss in results:
    if status == "OK":
        if model == "llama3.2:1b" and baseline_score is None:
            baseline_score = score
        flag = ""
        if model != "llama3.2:1b" and baseline_score is not None:
            beats = score is not None and score > baseline_score
            fast  = warm_avg is not None and warm_avg < 15.0
            fit   = rss is not None and rss < 1536
            flag = " ← CANDIDATE" if (beats and fast and fit) else ""
        print(f"{model:20s} {'OK':12s} {score}/8      {run1:6.1f}    {warm_avg:6.1f}    {rss:5}{flag}")
    else:
        print(f"{model:20s} {status}")

ok_candidates = [
    (model, score, run1, warm_avg, rss)
    for model, status, score, run1, warm_avg, rss in results
    if status == "OK"
    and model != "llama3.2:1b"
    and score is not None
    and baseline_score is not None
    and score > baseline_score
    and warm_avg < 15.0
    and rss < 1536
]

print(f"\nBaseline (llama3.2:1b): {baseline_score}/8")
if ok_candidates:
    winner_m, winner_s, _, winner_w, _ = sorted(ok_candidates, key=lambda x: (-x[1], x[3]))[0]
    print(f"DECISION: SWITCH to {winner_m}  (score {winner_s}/8, warm_avg {winner_w:.1f}s)")
else:
    print("DECISION: KEEP llama3.2:1b  (no candidate improves on all criteria)")
print(f"{'='*65}")
