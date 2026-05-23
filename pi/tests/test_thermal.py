"""Unit tests for thermal.py — no Pi hardware required."""

from __future__ import annotations

import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from thermal import _level, ThermalMonitor, _set_cpu_max_freq, read_thermal, read_cpu_freq_mhz


class TestLevel(unittest.TestCase):
    def test_cool_below_75(self):
        self.assertEqual(_level(40.0), "cool")
        self.assertEqual(_level(74.9), "cool")

    def test_boundary_cool_is_74_9(self):
        self.assertEqual(_level(74.999), "cool")

    def test_warm_75_to_79(self):
        self.assertEqual(_level(75.0), "warm")
        self.assertEqual(_level(79.9), "warm")

    def test_hot_80_to_84(self):
        self.assertEqual(_level(80.0), "hot")
        self.assertEqual(_level(84.9), "hot")

    def test_critical_85_plus(self):
        self.assertEqual(_level(85.0), "critical")
        self.assertEqual(_level(100.0), "critical")


class TestReadThermal(unittest.TestCase):
    def _mock_vcgencmd(self, temp_line="temp=54.3'C", throttle_line="throttled=0x0"):
        def _run(cmd, **kwargs):
            r = MagicMock()
            if "measure_temp" in cmd:
                r.stdout = temp_line + "\n"
            elif "get_throttled" in cmd:
                r.stdout = throttle_line + "\n"
            else:
                r.stdout = ""
            return r
        return _run

    def test_parses_temp(self):
        with patch("thermal.subprocess.run", side_effect=self._mock_vcgencmd()):
            result = read_thermal()
        self.assertAlmostEqual(result["temp_c"], 54.3)
        self.assertEqual(result["level"], "cool")

    def test_parses_no_throttle(self):
        with patch("thermal.subprocess.run", side_effect=self._mock_vcgencmd(throttle_line="throttled=0x0")):
            result = read_thermal()
        self.assertFalse(result.get("throttle_current"))
        self.assertFalse(result.get("throttle_ever"))

    def test_parses_current_throttle(self):
        # 0x5 = bits 0+2 set → current throttling
        with patch("thermal.subprocess.run", side_effect=self._mock_vcgencmd(throttle_line="throttled=0x5")):
            result = read_thermal()
        self.assertTrue(result.get("throttle_current"))
        self.assertFalse(result.get("throttle_ever"))

    def test_parses_ever_throttled(self):
        # 0x50000 = bits 16+18 set → ever throttled since boot
        with patch("thermal.subprocess.run", side_effect=self._mock_vcgencmd(throttle_line="throttled=0x50000")):
            result = read_thermal()
        self.assertFalse(result.get("throttle_current"))
        self.assertTrue(result.get("throttle_ever"))

    def test_parses_both_flags(self):
        with patch("thermal.subprocess.run", side_effect=self._mock_vcgencmd(throttle_line="throttled=0x50005")):
            result = read_thermal()
        self.assertTrue(result.get("throttle_current"))
        self.assertTrue(result.get("throttle_ever"))

    def test_hot_level(self):
        with patch("thermal.subprocess.run", side_effect=self._mock_vcgencmd(temp_line="temp=82.0'C")):
            result = read_thermal()
        self.assertEqual(result["level"], "hot")

    def test_vcgencmd_failure_returns_empty(self):
        with patch("thermal.subprocess.run", side_effect=FileNotFoundError("vcgencmd not found")):
            with patch("thermal.Path") as mock_path:
                mock_path.return_value.read_text.side_effect = OSError("no sysfs")
                result = read_thermal()
        self.assertNotIn("temp_c", result)
        self.assertNotIn("throttle_current", result)

    def test_sysfs_fallback_used_when_vcgencmd_missing(self):
        def fail_vcgencmd(cmd, **kwargs):
            raise FileNotFoundError

        real_path = os.path.join(os.path.dirname(__file__), "..")

        with patch("thermal.subprocess.run", side_effect=fail_vcgencmd):
            with patch("thermal.Path") as mock_path_cls:
                instance = MagicMock()
                instance.read_text.return_value = "56700"
                mock_path_cls.return_value = instance
                result = read_thermal()
        # If sysfs read succeeds, we get a temperature
        if "temp_c" in result:
            self.assertAlmostEqual(result["temp_c"], 56.7)


class TestReadCpuFreqMhz(unittest.TestCase):
    def test_converts_hz_to_mhz(self):
        with patch("thermal.Path") as mock_path_cls:
            instance = MagicMock()
            instance.read_text.return_value = "1800000"
            mock_path_cls.return_value = instance
            result = read_cpu_freq_mhz()
        self.assertEqual(result, 1800)

    def test_returns_none_on_error(self):
        with patch("thermal.Path") as mock_path_cls:
            instance = MagicMock()
            instance.read_text.side_effect = OSError("no cpufreq")
            mock_path_cls.return_value = instance
            result = read_cpu_freq_mhz()
        self.assertIsNone(result)


class TestThermalMonitor(unittest.TestCase):
    def _thermal(self, temp_c: float) -> dict:
        return {"temp_c": temp_c, "level": _level(temp_c)}

    def test_cool_no_alert(self):
        m = ThermalMonitor()
        self.assertIsNone(m.evaluate(self._thermal(40.0)))

    def test_warm_emits_alert_first_time(self):
        m = ThermalMonitor()
        result = m.evaluate(self._thermal(77.0))
        self.assertIsNotNone(result)
        self.assertEqual(result["severity"], "warn")
        self.assertIn("77", result["msg"])

    def test_warm_deduped_within_window(self):
        m = ThermalMonitor()
        m.evaluate(self._thermal(77.0))   # first → emits
        result = m.evaluate(self._thermal(77.0))  # within window → None
        self.assertIsNone(result)

    def test_warm_re_emits_after_window(self):
        m = ThermalMonitor()
        m.evaluate(self._thermal(77.0))
        m._last_warm_ts = time.monotonic() - 601  # expire window
        result = m.evaluate(self._thermal(77.0))
        self.assertIsNotNone(result)

    def test_hot_emits_warn(self):
        m = ThermalMonitor()
        result = m.evaluate(self._thermal(82.0))
        self.assertIsNotNone(result)
        self.assertEqual(result["severity"], "warn")

    def test_critical_emits_crit(self):
        m = ThermalMonitor()
        result = m.evaluate(self._thermal(87.0))
        self.assertIsNotNone(result)
        self.assertEqual(result["severity"], "crit")

    def test_no_temp_returns_none(self):
        m = ThermalMonitor()
        self.assertIsNone(m.evaluate({}))
        self.assertIsNone(m.evaluate({"level": "hot"}))

    def test_hot_triggers_throttle_when_sudo_available(self):
        m = ThermalMonitor()
        m._sudo_available = True
        with patch("thermal._set_cpu_max_freq", return_value=True) as mock_freq:
            m.evaluate(self._thermal(82.0))
        mock_freq.assert_called_once_with(1_600_000)
        self.assertTrue(m._throttled)

    def test_hot_no_double_throttle(self):
        m = ThermalMonitor()
        m._sudo_available = True
        m._throttled = True  # already throttled
        with patch("thermal._set_cpu_max_freq", return_value=True) as mock_freq:
            m.evaluate(self._thermal(82.0))
        mock_freq.assert_not_called()

    def test_cool_restores_freq_when_throttled(self):
        m = ThermalMonitor()
        m._sudo_available = True
        m._throttled = True
        with patch("thermal._set_cpu_max_freq", return_value=True) as mock_freq:
            m.evaluate(self._thermal(40.0))
        mock_freq.assert_called_once_with(1_800_000)
        self.assertFalse(m._throttled)

    def test_no_throttle_without_sudo(self):
        m = ThermalMonitor()
        m._sudo_available = False
        with patch("thermal._set_cpu_max_freq", return_value=True) as mock_freq:
            m.evaluate(self._thermal(82.0))
        mock_freq.assert_not_called()
        self.assertFalse(m._throttled)

    def test_cool_no_restore_if_not_throttled(self):
        m = ThermalMonitor()
        m._sudo_available = True
        m._throttled = False
        with patch("thermal._set_cpu_max_freq", return_value=True) as mock_freq:
            m.evaluate(self._thermal(40.0))
        mock_freq.assert_not_called()


if __name__ == "__main__":
    unittest.main()
