"""Flash ROVER2 Arduino firmware on the Pi via avrdude."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_HEX = Path("/tmp/rover2_firmware.hex")
DEFAULT_PORT = "/dev/ttyUSB0"
AVRDUDE = Path(os.environ.get("ROVER2_AVRDUDE", "/opt/rover2/tools/avrdude"))
AVRDUDE_CONF = Path(os.environ.get("ROVER2_AVRDUDE_CONF", "/opt/rover2/tools/avrdude.conf"))
SERVICE = os.environ.get("ROVER2_SERVICE", "rover2-api.service")


def tools_available() -> tuple[bool, str]:
    if not AVRDUDE.is_file():
        return False, f"avrdude not found at {AVRDUDE} — run scripts/install_avrdude_on_pi.sh on the Pi"
    if not AVRDUDE_CONF.is_file():
        return False, f"avrdude.conf not found at {AVRDUDE_CONF}"
    return True, "ok"


def flash_firmware(
    hex_path: Path | None = None,
    serial_port: str = DEFAULT_PORT,
    stop_service: bool = True,
) -> dict:
    """Stop rover2-api, flash *hex_path*, restart service. Returns status dict."""
    hex_path = hex_path or DEFAULT_HEX
    if not hex_path.is_file():
        raise FileNotFoundError(f"Firmware hex not found: {hex_path}")

    ok, detail = tools_available()
    if not ok:
        raise RuntimeError(detail)

    steps: list[str] = []
    if stop_service:
        subprocess.run(["sudo", "systemctl", "stop", SERVICE], check=False, timeout=30)
        steps.append("service_stopped")

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(AVRDUDE.parent) + ":" + env.get("LD_LIBRARY_PATH", "")

    cmd = [
        str(AVRDUDE),
        f"-C{AVRDUDE_CONF}",
        "-v",
        "-p",
        "atmega2560",
        "-c",
        "wiring",
        "-P",
        serial_port,
        "-b",
        "115200",
        "-D",
        f"-Uflash:w:{hex_path}:i",
    ]
    logger.info("Running avrdude: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    steps.append("avrdude_ran")

    if proc.returncode != 0:
        raise RuntimeError(
            f"avrdude failed (code {proc.returncode}): {proc.stderr[-500:] or proc.stdout[-500:]}"
        )

    if stop_service:
        subprocess.run(["sudo", "systemctl", "start", SERVICE], check=False, timeout=30)
        steps.append("service_started")

    return {
        "status": "ok",
        "hex": str(hex_path),
        "port": serial_port,
        "steps": steps,
    }


def flash_firmware_async(hex_path: Path | None = None) -> dict:
    """Queue a background flash (API survives because the worker script stops the service)."""
    hex_path = hex_path or DEFAULT_HEX
    if not hex_path.is_file():
        raise FileNotFoundError(f"Firmware hex not found: {hex_path}")

    ok, detail = tools_available()
    if not ok:
        raise RuntimeError(detail)

    script = Path("/opt/rover2/scripts/pi_flash_firmware.sh")
    if not script.is_file():
        script = Path(__file__).resolve().parent.parent / "scripts" / "pi_flash_firmware.sh"
    if not script.is_file():
        raise RuntimeError(f"Flash script not found: {script}")

    env = os.environ.copy()
    env["ROVER2_HEX"] = str(hex_path)
    subprocess.Popen(
        ["/bin/bash", str(script)],
        start_new_session=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "status": "accepted",
        "hex": str(hex_path),
        "log": "/tmp/rover2_flash.log",
        "message": "Flash started in background; rover2-api will restart when done",
    }
