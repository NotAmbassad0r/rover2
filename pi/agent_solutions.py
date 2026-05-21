"""Actionable fix suggestions for agent fast-path replies (no LLM)."""
from __future__ import annotations

from typing import Any


def build_solutions(
    *,
    cpu: float | None = None,
    temps: dict[str, Any] | None = None,
    ram: float | None = None,
    alerts: list[dict] | None = None,
    processes: list[dict] | None = None,
    log_snippet: str = "",
    user_asked_fix: bool = False,
) -> str:
    """Return a short 'Suggested fixes' block from live telemetry."""
    steps: list[str] = []
    temps = temps or {}
    alerts = alerts or []
    processes = processes or []

    hot_cpu = isinstance(cpu, (int, float)) and cpu >= 70
    crit_temp = False
    warn_temp = False
    for t in temps.values():
        cur = t.get("current") if isinstance(t, dict) else None
        if cur is None:
            continue
        if cur >= 85:
            crit_temp = True
        elif cur >= 75:
            warn_temp = True

    if hot_cpu or crit_temp or warn_temp:
        steps.append(
            "Keep Pi CPU cool: agent.backend should stay tools_only (no CPU Ollama). "
            "Use CHECK LOGS / TEMPS / alert ASK AI — they avoid loading the LLM on the Pi."
        )
        steps.append(
            "Turn FOLLOW and DETECT off when not needed; vision runs on the AI HAT but still uses some Pi CPU."
        )
        if "ollama" in log_snippet.lower() or any(
            "ollama" in (p.get("name") or "").lower() for p in processes[:5]
        ):
            steps.append(
                "Stop CPU Ollama: sudo systemctl stop ollama.service "
                "(LLM belongs on AI HAT: sudo bash /opt/rover2/scripts/setup_ai_on_hailo.sh with FOLLOW off)."
            )
        if crit_temp or (isinstance(cpu, (int, float)) and cpu >= 90):
            steps.append(
                "Let the Pi cool 2–5 minutes idle; ensure case airflow; "
                "unplug optional USB loads; confirm governor is schedutil not performance."
            )
        elif warn_temp or hot_cpu:
            steps.append(
                "Reduce load: close extra browser tabs to the robot; disable DIAG AUTO 5s; "
                "avoid long free-form agent chat until CPU is below 50%."
            )

    for a in alerts:
        msg = (a.get("msg") or "").lower()
        if "temperature" in msg or "hot" in msg:
            if "Ensure case has airflow" not in str(steps):
                steps.append("Check heatsink/fan on Pi 5; avoid stacking HAT in an enclosed box without ventilation.")
        if "cpu" in msg and "high" in msg:
            steps.append(
                "Run TOOLS → RESTART SERVICE only if stuck; prefer finding the top CPU process above first."
            )
        if "ram" in msg:
            steps.append("High RAM: restart rover2-api from TOOLS, or reboot Pi if memory does not drop.")
        if "disk" in msg:
            steps.append("Free disk: TOOLS → logs CLEAR; remove old files under /opt/rover2/data if metrics DB is huge.")

    if "timed out" in log_snippet.lower() and "ollama" in log_snippet.lower():
        steps.append(
            "Ollama timeout: do not use CPU LLM while FOLLOW is on. Use preset buttons or enable hailo-ollama on port 8000."
        )
    if "hailo" in log_snippet.lower() and "out of" in log_snippet.lower():
        steps.append(
            "Hailo busy: only one of FOLLOW, VLM describe, and hailo-ollama LLM should run at once; turn off FOLLOW before HAT chat."
        )

    top = processes[:3] if processes else []
    for p in top:
        name = (p.get("name") or "").lower()
        pct = p.get("cpu_percent")
        if isinstance(pct, (int, float)) and pct >= 25:
            if "ollama" in name:
                steps.append("Top CPU is ollama — run: sudo systemctl stop ollama.service")
            elif name.startswith("python") and hot_cpu:
                steps.append(
                    "High python3 CPU may be rover2-api + camera/BLE; pause FOLLOW/DETECT to confirm."
                )

    if isinstance(ram, (int, float)) and ram >= 85:
        steps.append("Restart rover2-api from TOOLS tab if RAM stays high after stopping follow.")

    if not steps and not alerts and not user_asked_fix:
        return ""

    if not steps:
        if user_asked_fix and not alerts:
            steps.append(
                "No active alerts in the API right now — metrics look OK. "
                "If the banner still shows a warning, hard-refresh the browser (Ctrl+Shift+R)."
            )
        steps.append(
            "Keep agent.backend on tools_only so the Pi stays a bridge (no CPU Ollama). "
            "Use TEMPS / CHECK LOGS / DIAG ASK AI for instant answers without heating the Pi."
        )

    uniq: list[str] = []
    for s in steps:
        if s not in uniq:
            uniq.append(s)
    return "\nSuggested fixes:\n" + "\n".join(f"  • {s}" for s in uniq[:6])
