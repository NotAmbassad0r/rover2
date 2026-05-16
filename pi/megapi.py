"""USB serial bridge to the ROVER2 MegaPi (Arduino) firmware."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import serial
from serial import SerialException

logger = logging.getLogger(__name__)


class MegaPiBridge:
    """Threaded JSON-line serial client for MegaPi firmware."""

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        timeout_s: float = 1.0,
        reconnect_interval_s: float = 5.0,
        on_message: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.reconnect_interval_s = reconnect_interval_s
        self.on_message = on_message

        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._last_read_time: float | None = None
        self._last_ultrasonic_cm: int | None = None
        self._last_ultrasonic_at: float | None = None
        self._ultrasonic_event = threading.Event()
        self._firmware_version: str | None = None
        self._motors_ready: bool = False
        self._poll_thread: threading.Thread | None = None
        self._poll_interval_s = 0.5

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def last_ultrasonic_cm(self) -> int | None:
        return self._last_ultrasonic_cm

    @property
    def last_ultrasonic_age_s(self) -> float | None:
        if self._last_ultrasonic_at is None:
            return None
        return round(time.monotonic() - self._last_ultrasonic_at, 2)

    @property
    def firmware_version(self) -> str | None:
        return self._firmware_version

    @property
    def motors_ready(self) -> bool:
        return self._motors_ready

    def set_poll_interval(self, interval_s: float) -> None:
        self._poll_interval_s = max(0.2, float(interval_s))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="megapi-bridge", daemon=True)
        self._thread.start()
        if not self._poll_thread or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._poll_ultrasonic_loop, name="megapi-ultra-poll", daemon=True
            )
            self._poll_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
        self._close()

    def send(self, command: dict[str, Any]) -> None:
        if not self.connected:
            raise RuntimeError("MegaPi serial not connected")
        payload = json.dumps(command, separators=(",", ":")) + "\n"
        with self._write_lock:
            assert self._serial is not None
            self._serial.write(payload.encode("utf-8"))
            self._serial.flush()

    def request_ultrasonic(self, wait_s: float = 0.55) -> int | None:
        """Request a fresh reading; wait up to *wait_s* for the sensor response."""
        if not self.connected:
            raise RuntimeError("MegaPi serial not connected")
        self._ultrasonic_event.clear()
        self.send({"cmd": "sensor_req", "sensor": "ultrasonic"})
        self._ultrasonic_event.wait(timeout=wait_s)
        return self._last_ultrasonic_cm

    def _poll_ultrasonic_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.connected:
                try:
                    self.request_ultrasonic(wait_s=self._poll_interval_s + 0.2)
                except Exception as exc:
                    logger.debug("Ultrasonic poll: %s", exc)
            self._stop_event.wait(self._poll_interval_s)

    def drive(self, left: int, right: int) -> None:
        self.send({"cmd": "drive", "l": int(left), "r": int(right)})

    def stop_motors(self) -> None:
        self.send({"cmd": "stop"})

    def grip(self, action: str) -> None:
        self.send({"cmd": "grip", "action": action})

    def arm(self, speed: int = 0, *, action: str | None = None) -> None:
        """Run arm lift motor: continuous *speed* (−255…255) or timed *action* up/down."""
        if action is not None:
            self.send({"cmd": "arm", "action": action})
        else:
            self.send({"cmd": "arm", "speed": int(speed)})

    def detect_hardware(self) -> None:
        self.send({"cmd": "detect"})

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.connected:
                self._connect()
                continue
            try:
                self._read_line()
            except SerialException as exc:
                logger.warning("Serial error: %s", exc)
                self._close()
            time.sleep(0.01)

    def _connect(self) -> None:
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout_s,
            )
            self._last_read_time = time.monotonic()
            logger.info("MegaPi connected on %s", self.port)
            delay = 2.5
            for _ in range(int(delay / 0.1)):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)
            try:
                self.send({"cmd": "ping"})
            except RuntimeError:
                pass
        except SerialException as exc:
            self._serial = None
            logger.warning("MegaPi connect failed (%s), retry in %.1fs", exc, self.reconnect_interval_s)
            time.sleep(self.reconnect_interval_s)

    def _close(self) -> None:
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None

    def _read_line(self) -> None:
        assert self._serial is not None
        raw = self._serial.readline()
        if not raw:
            return
        self._last_read_time = time.monotonic()
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        if text.startswith("ROVER"):
            self._firmware_version = text.split()[-1]
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("Non-JSON line: %s", text)
            return
        if not isinstance(msg, dict):
            return
        if msg.get("evt") == "sensor" or "ultrasonic_cm" in msg:
            raw_cm = msg.get("ultrasonic_cm")
            if raw_cm is None:
                self._last_ultrasonic_cm = -1
            else:
                try:
                    self._last_ultrasonic_cm = int(raw_cm)
                except (TypeError, ValueError):
                    self._last_ultrasonic_cm = -1
            self._last_ultrasonic_at = time.monotonic()
            self._ultrasonic_event.set()
        if msg.get("evt") in ("ready", "detected"):
            motors = int(msg.get("motors", 0))
            self._motors_ready = motors >= 2 if motors else True
            fw = msg.get("fw") or msg.get("version")
            if fw:
                self._firmware_version = str(fw)
            logger.info("MegaPi ready: %s", msg)
        if msg.get("evt") == "version":
            self._firmware_version = str(msg.get("fw", self._firmware_version))
        if msg.get("evt") == "info":
            logger.info("MegaPi: %s", msg.get("msg", msg))
        if self.on_message:
            self.on_message(msg)
