"""Tests for pi/megapi.py — serial port exclusivity (A3).

conftest.py stubs sys.modules["megapi"] with a bare MagicMock (safety.py
only needs `from megapi import MegaPiBridge` to resolve at import time, and
avoiding the real file means pi/safety.py's tests don't need real pyserial
behaviour). That means a plain `import megapi` here would get the stub, not
the real module under test — so this file loads pi/megapi.py directly from
its file path instead, under a distinct module name that never touches
sys.modules["megapi"] and therefore can't affect any other test file.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_MEGAPI_PATH = Path(__file__).parent.parent / "pi" / "megapi.py"
_spec = importlib.util.spec_from_file_location("megapi_real", _MEGAPI_PATH)
assert _spec is not None and _spec.loader is not None
megapi_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(megapi_real)

MegaPiBridge = megapi_real.MegaPiBridge


class TestSerialExclusivity:
    def test_connect_opens_serial_with_exclusive_true(self, monkeypatch):
        # A3: without exclusive=True, a second process (e.g. ROVER v1) can
        # open the same /dev/ttyUSB0 and interleave writes on the wire.
        fake_serial_cls = MagicMock()
        monkeypatch.setattr(megapi_real.serial, "Serial", fake_serial_cls)

        bridge = MegaPiBridge(port="/dev/ttyUSB0", baudrate=115200, timeout_s=1.0)
        bridge._stop_event.set()  # skip the post-connect ping delay loop
        bridge._connect()

        fake_serial_cls.assert_called_once_with(
            port="/dev/ttyUSB0",
            baudrate=115200,
            timeout=1.0,
            exclusive=True,
        )

    def test_connect_handles_port_already_held_by_another_process(self, monkeypatch):
        def _raise_busy(*args, **kwargs):
            raise megapi_real.SerialException(
                "could not open port /dev/ttyUSB0: [Errno 16] Device or resource busy"
            )

        monkeypatch.setattr(megapi_real.serial, "Serial", _raise_busy)

        bridge = MegaPiBridge(port="/dev/ttyUSB0", reconnect_interval_s=0.01)
        bridge._stop_event.set()

        bridge._connect()  # must not raise

        assert bridge._serial is None
        assert bridge.connected is False
