"""Tests for pi/emotion_state.py and its wiring into RoverAgent tool dispatch."""
from __future__ import annotations

from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "pi"))

import emotion_state
from agent import RoverAgent


class TestEmotionStateModule:
    def setup_method(self):
        emotion_state.reset()

    def test_initial_state_idle(self):
        assert emotion_state.current() == emotion_state.IDLE

    def test_person_detected_sets_attentive(self):
        emotion_state.on_person_detected()
        assert emotion_state.current() == emotion_state.ATTENTIVE

    def test_person_lost_sets_idle(self):
        emotion_state.on_person_detected()
        emotion_state.on_person_lost()
        assert emotion_state.current() == emotion_state.IDLE

    def test_wake_word_sets_attentive(self):
        emotion_state.on_wake_word()
        assert emotion_state.current() == emotion_state.ATTENTIVE

    def test_tool_acknowledging_then_working_then_done_reverts_to_ambient(self):
        emotion_state.on_person_detected()  # ambient = attentive
        emotion_state.on_tool_acknowledging()
        assert emotion_state.current() == emotion_state.ACKNOWLEDGING
        emotion_state.on_tool_working()
        assert emotion_state.current() == emotion_state.WORKING
        emotion_state.on_tool_done()
        assert emotion_state.current() == emotion_state.ATTENTIVE

    def test_tool_done_without_person_reverts_to_idle(self):
        emotion_state.on_tool_acknowledging()
        emotion_state.on_tool_working()
        emotion_state.on_tool_done()
        assert emotion_state.current() == emotion_state.IDLE

    def test_watchdog_alert_overrides_transient_and_ambient(self):
        emotion_state.on_person_detected()
        emotion_state.on_tool_working()
        emotion_state.on_watchdog_alert()
        assert emotion_state.current() == emotion_state.ALERT

    def test_watchdog_concern_overrides_ambient(self):
        emotion_state.on_person_detected()
        emotion_state.on_watchdog_concern()
        assert emotion_state.current() == emotion_state.CONCERNED

    def test_watchdog_alert_beats_concern(self):
        emotion_state.on_watchdog_concern()
        emotion_state.on_watchdog_alert()
        assert emotion_state.current() == emotion_state.ALERT

    def test_watchdog_clear_restores_previous_layer(self):
        emotion_state.on_person_detected()
        emotion_state.on_watchdog_alert()
        emotion_state.on_watchdog_clear()
        assert emotion_state.current() == emotion_state.ATTENTIVE

    def test_is_valid_accepts_all_known_states(self):
        for s in emotion_state.VALID_STATES:
            assert emotion_state.is_valid(s)

    def test_is_valid_rejects_unknown_states(self):
        assert not emotion_state.is_valid("bogus")
        assert not emotion_state.is_valid("")
        assert not emotion_state.is_valid("IDLE")   # case-sensitive, not the canonical form
        assert not emotion_state.is_valid(None)

    def test_current_ignores_corrupted_ambient(self):
        """A bug writing an unrecognised value into internal state must not leak out."""
        emotion_state._ambient = "not-a-real-state"
        assert emotion_state.current() == emotion_state.IDLE

    def test_current_ignores_corrupted_transient(self):
        emotion_state._transient = "bogus"
        assert emotion_state.current() == emotion_state.IDLE

    def test_current_ignores_corrupted_concern(self):
        emotion_state._concern = "bogus"
        assert emotion_state.current() == emotion_state.IDLE

    def test_reset_restores_defaults(self):
        emotion_state.on_person_detected()
        emotion_state.on_tool_working()
        emotion_state.on_watchdog_alert()
        emotion_state.reset()
        assert emotion_state.current() == emotion_state.IDLE


class TestToolDispatchEmitsStateSequence:
    """Verifies RoverAgent._run_tool_with_reaction emits the expected
    acknowledging -> working -> (tool runs) -> done sequence, and that the
    underlying tool logic/result is completely untouched."""

    def setup_method(self):
        emotion_state.reset()

    async def test_expected_state_sequence_on_success(self, monkeypatch):
        import agent as agent_module

        calls: list[str] = []
        monkeypatch.setattr(agent_module.emotion_state, "on_tool_acknowledging",
                             lambda: calls.append("acknowledging"))
        monkeypatch.setattr(agent_module.emotion_state, "on_tool_working",
                             lambda: calls.append("working"))
        monkeypatch.setattr(agent_module.emotion_state, "on_tool_done",
                             lambda: calls.append("done"))

        rover_agent = RoverAgent()

        async def fake_run_tool(name, args):
            calls.append(f"tool_ran:{name}:{args}")
            return {"ok": True}

        rover_agent._run_tool = fake_run_tool

        result = await rover_agent._run_tool_with_reaction("wave_arm", {"speed": 1})

        assert result == {"ok": True}, "tool result must be passed through unchanged"
        assert calls == ["acknowledging", "working", "tool_ran:wave_arm:{'speed': 1}", "done"], (
            f"unexpected sequence: {calls}"
        )

    async def test_state_cleared_even_when_tool_raises(self, monkeypatch):
        import agent as agent_module
        import pytest

        calls: list[str] = []
        monkeypatch.setattr(agent_module.emotion_state, "on_tool_acknowledging",
                             lambda: calls.append("acknowledging"))
        monkeypatch.setattr(agent_module.emotion_state, "on_tool_working",
                             lambda: calls.append("working"))
        monkeypatch.setattr(agent_module.emotion_state, "on_tool_done",
                             lambda: calls.append("done"))

        rover_agent = RoverAgent()

        async def failing_run_tool(name, args):
            calls.append("tool_ran")
            raise RuntimeError("boom")

        rover_agent._run_tool = failing_run_tool

        with pytest.raises(RuntimeError):
            await rover_agent._run_tool_with_reaction("suggest_fix", {})

        assert calls == ["acknowledging", "working", "tool_ran", "done"], (
            "on_tool_done must fire even when the tool raises"
        )

    async def test_real_emotion_state_reflects_dispatch(self):
        """End-to-end (no mocking of emotion_state itself): current() shows
        WORKING while the tool runs, and reverts to ambient afterwards."""
        rover_agent = RoverAgent()
        observed_during: list[str] = []

        async def fake_run_tool(name, args):
            observed_during.append(emotion_state.current())
            return {"ok": True}

        rover_agent._run_tool = fake_run_tool

        assert emotion_state.current() == emotion_state.IDLE
        await rover_agent._run_tool_with_reaction("control_follow_mode", {"action": "enable"})

        assert observed_during == [emotion_state.WORKING]
        assert emotion_state.current() == emotion_state.IDLE
