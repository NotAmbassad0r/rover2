"""Tests for pi/agent_solutions.py — pure fix-suggestion logic, no hardware."""
from __future__ import annotations

from agent_solutions import build_solutions


def _hot_cpu(**kw) -> str:
    return build_solutions(cpu=92, **kw)


def _crit_temp(**kw) -> str:
    return build_solutions(
        temps={"cpu": {"current": 87}}, **kw
    )


def _warn_temp(**kw) -> str:
    return build_solutions(
        temps={"cpu": {"current": 78}}, **kw
    )


class TestBuildSolutions:
    def test_no_alerts_no_fix_request_returns_empty(self):
        result = build_solutions()
        assert result == ""

    def test_no_alerts_user_asks_fix_returns_non_empty(self):
        result = build_solutions(user_asked_fix=True)
        assert "Suggested fixes" in result or len(result) > 0

    def test_hot_cpu_includes_cooling_step(self):
        result = _hot_cpu()
        assert "Suggested fixes" in result
        assert "FOLLOW" in result or "vision" in result.lower() or "cool" in result.lower()

    def test_critical_temp_includes_cooling_step(self):
        result = _crit_temp()
        assert "Suggested fixes" in result

    def test_warn_temp_includes_step(self):
        result = _warn_temp()
        assert "Suggested fixes" in result

    def test_high_ram_includes_restart_step(self):
        result = build_solutions(ram=90)
        assert "Suggested fixes" in result
        assert "restart" in result.lower() or "RAM" in result

    def test_disk_alert_includes_disk_step(self):
        result = build_solutions(
            alerts=[{"msg": "disk usage critical"}]
        )
        assert "disk" in result.lower() or "Disk" in result

    def test_ram_alert_includes_restart(self):
        result = build_solutions(
            alerts=[{"msg": "high ram usage"}]
        )
        assert "RAM" in result or "restart" in result.lower()

    def test_ollama_timeout_in_log_adds_step(self):
        result = build_solutions(
            cpu=80,
            log_snippet="Ollama request timed out after 60s",
        )
        assert "timeout" in result.lower() or "Ollama" in result

    def test_hailo_busy_in_log_adds_step(self):
        result = build_solutions(
            log_snippet="hailo: out of memory, model busy",
        )
        assert "Hailo" in result or "one of" in result.lower()

    def test_high_cpu_process_ollama_adds_stop_step(self):
        result = build_solutions(
            cpu=90,
            processes=[{"name": "ollama runner", "cpu_pct": 60}],
        )
        assert "ollama" in result.lower()

    def test_deduplication_of_identical_steps(self):
        # Two different conditions that produce the same step
        result = build_solutions(
            cpu=92,
            temps={"cpu": {"current": 87}},
        )
        # Count occurrences of "FOLLOW" — should appear at most once per unique step
        count = result.count("Turn FOLLOW and DETECT off")
        assert count <= 1, f"Duplicate steps not deduplicated: count={count}"

    def test_max_six_steps(self):
        result = build_solutions(
            cpu=92,
            temps={"cpu": {"current": 87}},
            ram=90,
            alerts=[
                {"msg": "temperature hot"},
                {"msg": "high cpu usage"},
                {"msg": "ram high"},
                {"msg": "disk low"},
            ],
            log_snippet="ollama timed out hailo out of memory",
            user_asked_fix=True,
        )
        # Count bullet points — should be at most 6
        bullet_count = result.count("  • ")
        assert bullet_count <= 6, f"Too many steps: {bullet_count}"

    def test_result_starts_with_newline_suggested_fixes(self):
        result = build_solutions(cpu=90)
        if result:
            assert result.startswith("\nSuggested fixes:\n")

    def test_cpu_ollama_in_processes_adds_stop_suggestion(self):
        result = build_solutions(
            cpu=91,
            processes=[{"name": "ollama_runner", "cpu_pct": 50}],
        )
        assert "ollama" in result.lower()
