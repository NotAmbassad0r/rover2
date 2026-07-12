"""Tests for the advisory LLM diagnosis layer and self-test hook in pi/watchdog.py.

All hardware/Hailo is mocked — these run entirely offline. The deterministic
30s watchdog and its existing auto-remediation must never depend on any of
this; every test here proves the floor holds regardless of diagnosis outcome.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import emotion_state
import hailo_session
import watchdog as _wd_module
from watchdog import Watchdog


@pytest.fixture(autouse=True)
def _patch_log_path(tmp_path, monkeypatch):
    """Redirect watchdog log to tmp_path — avoids /opt/rover2 permission errors."""
    monkeypatch.setattr(_wd_module, "_LOG_PATH", tmp_path / "watchdog.log")


@pytest.fixture(autouse=True)
def _reset_emotion_state():
    emotion_state.reset()
    yield
    emotion_state.reset()


def _make_watchdog(rover_agent=None, **llm_diagnosis_overrides) -> Watchdog:
    llm_cfg = {
        "enabled": True,
        "cooldown_s": 300,
        "allow_llm_remediation": False,
        **llm_diagnosis_overrides,
    }
    config = {
        "watchdog": {
            "enabled": True,
            "interval_s": 30,
            "memory_warn_mb": 420,
            "memory_critical_mb": 500,
            "temp_warn_c": 78,
            "temp_critical_c": 85,
            "action_window_s": 3600,
            "max_actions": 3,
            "ollama_idle_stop_minutes": 2,
            "cpu_sustained_percent": 90,
            "cpu_sustained_seconds": 60,
            "disk_warn_gb": 2.0,
            "disk_critical_gb": 0.5,
            "cert_warn_days": 30,
            "cert_critical_days": 7,
            "temp_follow_disable_c": 85.0,
            "llm_diagnosis": llm_cfg,
        }
    }
    return Watchdog(config, rover_agent=rover_agent)


async def _drain(wd: Watchdog) -> None:
    """Let any fire-and-forget diagnosis/remediation tasks finish."""
    await asyncio.sleep(0)
    for t in list(wd._diagnosis_tasks.values()) + list(wd._remediation_tasks.values()):
        if not t.done():
            await t


class TestFloorHoldsWithDiagnosisUnavailable:
    """The deterministic watchdog must keep monitoring/remediating even when
    diagnosis is completely unavailable."""

    async def test_memory_expanded_check_still_remediates(self, monkeypatch):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value=None)  # LLM unavailable
        wd = _make_watchdog(rover_agent=agent)

        monkeypatch.setattr(wd, "_get_self_rss_mb", AsyncMock(return_value=420.0))
        monkeypatch.setattr(wd, "_top5_rss", AsyncMock(return_value="python3 100MB"))
        run_fix = AsyncMock(return_value=True)
        monkeypatch.setattr(wd, "_run_fix", run_fix)

        await wd._check_memory_expanded()

        run_fix.assert_awaited_once_with(["sudo", "systemctl", "stop", "ollama"])
        assert wd._ollama_stopped_for_mem is True
        alerts = wd.get_persistent_alerts()
        assert any(a["metric"] == "watchdog_mem_high_stop_ollama" for a in alerts), (
            "the real alert must still be raised even though diagnosis is unavailable"
        )
        await _drain(wd)

    async def test_emit_alert_never_raises_when_diagnose_itself_raises(self):
        """A crashing diagnosis task must never surface out of _emit_alert."""
        agent = MagicMock()
        agent.diagnose = AsyncMock(side_effect=RuntimeError("boom"))
        wd = _make_watchdog(rover_agent=agent)

        wd._emit_alert("some_check", "warning", "trouble")  # must not raise
        assert wd.get_persistent_alerts()
        await _drain(wd)  # the task's own exception is swallowed internally

    async def test_no_agent_wired_up_never_blocks(self):
        """rover_agent=None (agent disabled) must behave exactly like today."""
        wd = _make_watchdog(rover_agent=None)
        wd._emit_alert("cpu", "critical", "hot")
        assert wd.get_persistent_alerts()
        assert wd._diagnosis_tasks == {}


class TestDebounce:
    async def test_one_diagnosis_per_alert_instance(self):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={"text": "looks fine", "action_proposal": None})
        wd = _make_watchdog(rover_agent=agent, cooldown_s=300)

        wd._emit_alert("cpu_high", "warning", "cpu is high")
        await _drain(wd)

        wd._emit_alert("cpu_high", "warning", "cpu is high (still)")
        await _drain(wd)

        assert agent.diagnose.await_count == 1, "must not re-diagnose the same alert instance"

    async def test_cooldown_blocks_flapping_re_emit(self):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={"text": "ok", "action_proposal": None})
        wd = _make_watchdog(rover_agent=agent, cooldown_s=300)

        wd._emit_alert("cpu_high", "warning", "first")
        await _drain(wd)

        wd._clear_alert("cpu_high")
        wd._emit_alert("cpu_high", "warning", "flapped back")
        await _drain(wd)

        assert agent.diagnose.await_count == 1, "cooldown must block a flapping re-emit of the same key"

    async def test_severity_escalation_bypasses_cooldown(self):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={"text": "ok", "action_proposal": None})
        wd = _make_watchdog(rover_agent=agent, cooldown_s=300)

        wd._emit_alert("cpu_high", "warning", "warning level")
        await _drain(wd)

        wd._emit_alert("cpu_high", "critical", "escalated!")
        await _drain(wd)

        assert agent.diagnose.await_count == 2, "an escalation to critical must bypass the cooldown"

    async def test_concurrent_emits_for_same_key_do_not_double_diagnose(self):
        agent = MagicMock()

        async def _slow_diagnose(prompt):
            await asyncio.sleep(0.02)
            return {"text": "ok", "action_proposal": None}

        agent.diagnose = AsyncMock(side_effect=_slow_diagnose)
        wd = _make_watchdog(rover_agent=agent, cooldown_s=300)

        wd._emit_alert("thermal", "warning", "hot")
        wd._emit_alert("thermal", "warning", "still hot")  # fires before the first task finishes
        await _drain(wd)

        assert agent.diagnose.await_count == 1, "an in-flight diagnosis must not be duplicated"


class TestSkipsWhenHailoSessionBusy:
    async def test_diagnosis_skipped_when_session_held(self, monkeypatch):
        monkeypatch.setattr(hailo_session, "session_holder", lambda: "voice-conversation")

        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={"text": "should never be called"})
        wd = _make_watchdog(rover_agent=agent)

        wd._emit_alert("thermal", "warning", "hot")
        await _drain(wd)

        agent.diagnose.assert_not_awaited()
        assert "thermal" not in wd._diagnosis_tasks, "no task should even be created"
        assert "thermal" not in wd._diagnosed_at, "a skip must not burn the cooldown"

    async def test_retries_next_cycle_after_session_frees_up(self, monkeypatch):
        holder = {"value": "voice"}
        monkeypatch.setattr(hailo_session, "session_holder", lambda: holder["value"])

        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={"text": "ok", "action_proposal": None})
        wd = _make_watchdog(rover_agent=agent)

        wd._emit_alert("thermal", "warning", "hot")
        await _drain(wd)
        agent.diagnose.assert_not_awaited()

        holder["value"] = None  # session freed up
        wd._emit_alert("thermal", "warning", "still hot")
        await _drain(wd)
        agent.diagnose.assert_awaited_once()


class TestLlmRemediationWhitelist:
    async def test_not_executed_when_flag_off(self, monkeypatch):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={
            "text": "ollama seems stuck",
            "action_proposal": {"name": "restart_service", "args": {"service": "ollama"}},
        })
        wd = _make_watchdog(rover_agent=agent, allow_llm_remediation=False)
        run_fix = AsyncMock(return_value=True)
        monkeypatch.setattr(wd, "_run_fix", run_fix)

        wd._emit_alert("ollama_stuck", "warning", "ollama looks stuck")
        await _drain(wd)

        run_fix.assert_not_awaited()
        assert wd.actions_used == 0

    async def test_executes_when_enabled_and_whitelisted(self, monkeypatch):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={
            "text": "ollama seems stuck",
            "action_proposal": {"name": "restart_service", "args": {"service": "ollama"}},
        })
        wd = _make_watchdog(rover_agent=agent, allow_llm_remediation=True)
        run_fix = AsyncMock(return_value=True)
        monkeypatch.setattr(wd, "_run_fix", run_fix)

        wd._emit_alert("ollama_stuck", "warning", "ollama looks stuck")
        await _drain(wd)

        run_fix.assert_awaited_once_with(["sudo", "systemctl", "restart", "ollama"])
        assert wd.actions_used == 1

    async def test_rejects_non_whitelisted_action_even_when_enabled(self, monkeypatch):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={
            "text": "just reboot it",
            "action_proposal": {"name": "reboot_pi", "args": {"reason": "why not"}},
        })
        wd = _make_watchdog(rover_agent=agent, allow_llm_remediation=True)
        run_fix = AsyncMock(return_value=True)
        monkeypatch.setattr(wd, "_run_fix", run_fix)

        wd._emit_alert("something", "warning", "trouble")
        await _drain(wd)

        run_fix.assert_not_awaited()
        assert wd.actions_used == 0

    async def test_rejects_non_whitelisted_service_even_when_enabled(self, monkeypatch):
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={
            "text": "restart something else",
            "action_proposal": {"name": "restart_service", "args": {"service": "rover2-api"}},
        })
        wd = _make_watchdog(rover_agent=agent, allow_llm_remediation=True)
        run_fix = AsyncMock(return_value=True)
        monkeypatch.setattr(wd, "_run_fix", run_fix)

        wd._emit_alert("something", "warning", "trouble")
        await _drain(wd)

        run_fix.assert_not_awaited()
        assert wd.actions_used == 0


class TestSelfTestHook:
    async def test_emits_alert_mapping_to_concerned(self):
        wd = _make_watchdog()
        result = wd.trigger_selftest()

        assert result["emotion_state"] == emotion_state.CONCERNED
        assert emotion_state.current() == emotion_state.CONCERNED
        alerts = wd.get_persistent_alerts()
        assert any(
            a["metric"] == "watchdog_selftest" and "[SELF-TEST]" in a["msg"]
            for a in alerts
        )
        wd._selftest_clear_task.cancel()

    async def test_critical_severity_maps_to_alert(self):
        wd = _make_watchdog()
        result = wd.trigger_selftest(severity="critical")

        assert result["emotion_state"] == emotion_state.ALERT
        assert emotion_state.current() == emotion_state.ALERT
        wd._selftest_clear_task.cancel()

    async def test_invalid_severity_falls_back_to_warning(self):
        wd = _make_watchdog()
        result = wd.trigger_selftest(severity="nonsense")
        assert result["severity"] == "warning"
        assert result["emotion_state"] == emotion_state.CONCERNED
        wd._selftest_clear_task.cancel()

    async def test_auto_clears_after_ttl(self, monkeypatch):
        monkeypatch.setattr(_wd_module, "_SELFTEST_TTL_S", 0.01)
        wd = _make_watchdog()

        wd.trigger_selftest()
        assert emotion_state.current() == emotion_state.CONCERNED

        await asyncio.sleep(0.05)

        assert emotion_state.current() == emotion_state.IDLE
        assert not any(a["metric"] == "watchdog_selftest" for a in wd.get_persistent_alerts())

    async def test_invokes_no_real_remediation(self, monkeypatch):
        wd = _make_watchdog()
        run_fix = AsyncMock(return_value=True)
        monkeypatch.setattr(wd, "_run_fix", run_fix)

        wd.trigger_selftest()
        await asyncio.sleep(0)

        run_fix.assert_not_awaited()
        assert wd.actions_used == 0
        wd._selftest_clear_task.cancel()

    async def test_can_be_diagnosed_like_a_real_alert(self):
        """Diagnosis runs for the self-test alert too (it's the documented way
        to exercise the whole path on demand) — but see the next test: even if
        the LLM proposes a remediation for it, it must never execute."""
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={"text": "this is a test, no action needed",
                                                  "action_proposal": None})
        wd = _make_watchdog(rover_agent=agent)

        wd.trigger_selftest()
        await _drain(wd)

        agent.diagnose.assert_awaited_once()
        wd._selftest_clear_task.cancel()

    async def test_never_executes_remediation_even_if_llm_proposes_one(self, monkeypatch):
        """The one hard rule for self-test: no real remediation, ever — even
        with allow_llm_remediation on and a whitelisted-looking proposal."""
        agent = MagicMock()
        agent.diagnose = AsyncMock(return_value={
            "text": "just restart ollama to be safe",
            "action_proposal": {"name": "restart_service", "args": {"service": "ollama"}},
        })
        wd = _make_watchdog(rover_agent=agent, allow_llm_remediation=True)
        run_fix = AsyncMock(return_value=True)
        monkeypatch.setattr(wd, "_run_fix", run_fix)

        wd.trigger_selftest()
        await _drain(wd)

        run_fix.assert_not_awaited()
        assert wd.actions_used == 0
        wd._selftest_clear_task.cancel()

    async def test_idempotent_repeated_calls(self):
        wd = _make_watchdog()
        wd.trigger_selftest()
        first_task = wd._selftest_clear_task

        wd.trigger_selftest()  # fire again before the first TTL elapses
        second_task = wd._selftest_clear_task
        await asyncio.sleep(0)  # let the requested cancellation of first_task finalize

        assert first_task is not second_task
        assert first_task.cancelled() or first_task.done()
        assert not second_task.done()
        alerts = [a for a in wd.get_persistent_alerts() if a["metric"] == "watchdog_selftest"]
        assert len(alerts) == 1, "repeated self-test calls must not duplicate the alert"
        second_task.cancel()
