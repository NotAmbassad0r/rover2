"""pytest configuration: install hardware-boundary stubs and add pi/ to sys.path.

These stubs MUST be installed before any pi/ module is imported, because
conftest.py executes at collection time before test-file top-level imports.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ── 1. Hardware-boundary module stubs ───────────────────────────────────────
# Add to sys.modules so that pi/ imports see mocks instead of real hardware.

def _stub(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


# hailo_platform and sub-packages (body_tracker, vlm_engine)
_hailo = _stub("hailo_platform")
_hailo.VDevice = MagicMock  # type: ignore[attr-defined]
_hailo_genai = _stub("hailo_platform.genai")
_hailo_genai.LLM = MagicMock  # type: ignore[attr-defined]
_hailo_genai.VLM = MagicMock  # type: ignore[attr-defined]
_stub("hailo_platform.pyhailort")

# turbojpeg (body_tracker, camera_server)
_turbo = _stub("turbojpeg")
_turbo.TurboJPEG = MagicMock  # type: ignore[attr-defined]

# pyserial (megapi)
_serial = _stub("serial")
_serial.Serial = MagicMock  # type: ignore[attr-defined]
_serial.SerialException = Exception  # type: ignore[attr-defined]

# bleak (ble_tracker)
_bleak = _stub("bleak")
_bleak.BleakScanner = MagicMock  # type: ignore[attr-defined]

# megapi stub — safety.py does `from megapi import MegaPiBridge` at module level.
# If we let Python find pi/megapi.py it will import serial → need real pyserial.
# Stub the entire module so the import resolves without touching the real file.
_megapi_stub = _stub("megapi")
_megapi_stub.MegaPiBridge = MagicMock  # type: ignore[attr-defined]

# cv2 (camera_server)
_cv2 = _stub("cv2")
_cv2.VideoCapture = MagicMock  # type: ignore[attr-defined]

# ── 2. Add pi/ to sys.path ──────────────────────────────────────────────────
_PI_DIR = Path(__file__).parent.parent / "pi"
if str(_PI_DIR) not in sys.path:
    sys.path.insert(0, str(_PI_DIR))
