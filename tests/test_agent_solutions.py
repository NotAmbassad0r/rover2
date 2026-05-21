"""Tests for agent_solutions.build_solutions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))

from agent_solutions import build_solutions  # noqa: E402


def test_hot_cpu_includes_cooldown_steps():
    out = build_solutions(cpu=92, temps={"cpu_thermal": {"current": 86}})
    assert "Suggested fixes:" in out
    assert "tools_only" in out or "FOLLOW" in out


def test_ollama_in_logs_suggests_stop():
    out = build_solutions(log_snippet="Agent: Ollama timed out after 180s")
    assert "ollama" in out.lower()


def test_no_issues_empty_block():
    assert build_solutions() == ""
