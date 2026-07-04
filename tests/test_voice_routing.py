"""Tests for pi/voice_engine.py — pure logic parts only (TTS pre-processing, session flag).

Hardware-dependent parts (faster-whisper, piper subprocess) are NOT tested here.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import voice_engine as _ve


class TestPreprocessForTts:
    def test_temperature_notation_expanded(self):
        result = _ve._preprocess_for_tts("Temperature is 42.3°C")
        assert "42.3 degrees Celsius" in result
        assert "°C" not in result

    def test_temperature_without_decimal(self):
        result = _ve._preprocess_for_tts("Temp: 64°C")
        assert "64 degrees Celsius" in result

    def test_percentage_expanded(self):
        result = _ve._preprocess_for_tts("CPU at 75%")
        assert "75 percent" in result
        assert "%" not in result

    def test_thousands_separator_removed(self):
        result = _ve._preprocess_for_tts("Used 1,234,567 bytes")
        assert "1234567" in result
        assert "1,234,567" not in result

    def test_rssi_expanded(self):
        result = _ve._preprocess_for_tts("RSSI is -65")
        assert "signal strength" in result

    def test_ble_expanded(self):
        result = _ve._preprocess_for_tts("BLE beacon lost")
        assert "Bluetooth" in result

    def test_llm_expanded(self):
        result = _ve._preprocess_for_tts("The LLM is running")
        assert "language model" in result

    def test_markdown_bold_stripped(self):
        result = _ve._preprocess_for_tts("**important** text")
        assert "**" not in result
        assert "important" in result

    def test_markdown_code_stripped(self):
        result = _ve._preprocess_for_tts("`code here`")
        assert "`" not in result
        assert "code here" in result

    def test_extra_whitespace_collapsed(self):
        result = _ve._preprocess_for_tts("hello   world")
        assert "  " not in result

    def test_empty_string_returns_empty(self):
        assert _ve._preprocess_for_tts("") == ""

    def test_temperature_before_percent_in_pipeline(self):
        # "42.3°C" should not hit the percent replacement before the temp one
        result = _ve._preprocess_for_tts("42.3°C and 80%")
        assert "42.3 degrees Celsius" in result
        assert "80 percent" in result


class TestSessionFlag:
    def test_is_active_false_initially(self):
        _ve.set_active(False)
        assert _ve.is_active() is False

    def test_set_active_true(self):
        _ve.set_active(True)
        assert _ve.is_active() is True
        _ve.set_active(False)  # reset

    def test_round_trip(self):
        _ve.set_active(False)
        assert _ve.is_active() is False
        _ve.set_active(True)
        assert _ve.is_active() is True
        _ve.set_active(False)
        assert _ve.is_active() is False


class TestSpeakEventDebounce:
    def setup_method(self):
        # Reset debounce state before each test
        _ve._last_spoken.clear()

    def test_first_call_fires(self):
        fake_router = MagicMock()
        fake_router.get_mode.return_value = "ROVER_A32"

        calls = []
        with patch("voice_engine.asyncio") as mock_asyncio:
            mock_asyncio.get_event_loop.return_value.is_running.return_value = False
            _ve.speak_event("PERSON_FOUND", audio_router=fake_router)
            # Just check no exception is raised and debounce key is set
        assert "PERSON_FOUND" in _ve._last_spoken

    def test_second_call_within_debounce_is_silent(self):
        _ve._last_spoken["PERSON_FOUND"] = time.monotonic()  # mark as just-spoken
        fake_router = MagicMock()
        fake_router.get_mode.return_value = "ROVER_A32"

        dispatch_called = []
        with patch("voice_engine.asyncio") as mock_asyncio:
            loop = MagicMock()
            loop.is_running.return_value = False
            mock_asyncio.get_event_loop.return_value = loop
            _ve.speak_event("PERSON_FOUND", audio_router=fake_router)
            dispatch_called.append(mock_asyncio.ensure_future.called)

        # ensure_future should NOT have been called (debounce active)
        assert not any(dispatch_called), "speak_event should be silent within debounce window"

    def test_none_audio_router_is_silent(self):
        _ve._last_spoken.clear()
        # Should not raise and should not set debounce key
        _ve.speak_event("PERSON_FOUND", audio_router=None)
        assert "PERSON_FOUND" not in _ve._last_spoken

    def test_wrong_mode_is_silent(self):
        _ve._last_spoken.clear()
        fake_router = MagicMock()
        fake_router.get_mode.return_value = "BUDS"  # not ROVER_A32
        _ve.speak_event("PERSON_FOUND", audio_router=fake_router)
        assert "PERSON_FOUND" not in _ve._last_spoken

    def test_unknown_event_key_is_silent(self):
        _ve._last_spoken.clear()
        fake_router = MagicMock()
        fake_router.get_mode.return_value = "ROVER_A32"
        # No exception should be raised for unknown event key
        _ve.speak_event("NO_SUCH_EVENT", audio_router=fake_router)

    def test_whisper_loaded_false_initially(self):
        # faster-whisper is never loaded in tests — model should be None
        assert _ve.whisper_loaded() is False

    def test_whisper_idle_s_none_when_never_used(self):
        _ve._last_voice_activity_ts = 0.0
        assert _ve.whisper_idle_s() is None
