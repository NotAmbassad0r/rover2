"""ROVER2 entrypoint — minimal Pi API for drive, gripper, ultrasonic."""

from __future__ import annotations

import logging
import pathlib
import sys

import uvicorn
import yaml

from body_tracker import BodyTracker
from megapi import MegaPiBridge
from safety import SafetyMonitor
from server import create_app, _directions_from_config

CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"


def main() -> None:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    serial_cfg = config.get("serial", {})
    megapi = MegaPiBridge(
        port=serial_cfg.get("port", "/dev/ttyUSB0"),
        baudrate=int(serial_cfg.get("baudrate", 115200)),
        timeout_s=float(serial_cfg.get("timeout_s", 1.0)),
        reconnect_interval_s=float(serial_cfg.get("reconnect_interval_s", 5.0)),
    )
    ultra_cfg = config.get("ultrasonic", {})
    megapi.set_poll_interval(float(ultra_cfg.get("poll_interval_s", 0.4)))
    megapi.start()

    safety_monitor: SafetyMonitor | None = None
    safety_cfg = config.get("safety", {})
    if safety_cfg.get("enabled", True):
        directions = _directions_from_config(config)
        safety_monitor = SafetyMonitor(
            megapi=megapi,
            forward_signs=directions["forward"],
            safe_distance_cm=int(safety_cfg.get("safe_distance_cm", 40)),
            poll_interval_s=float(safety_cfg.get("poll_interval_s", 0.5)),
            initial_poll_delay_s=float(safety_cfg.get("initial_poll_delay_s", 1.0)),
        )
        safety_monitor.start()

    directions = _directions_from_config(config)
    body_tracker: BodyTracker | None = None
    tracker_cfg = config.get("body_tracker", {})
    if tracker_cfg.get("enabled", False):
        body_tracker = BodyTracker(
            drive=megapi.drive,
            stop=megapi.stop_motors,
            directions=directions,
            config=config,
        )
        body_tracker.start()

    server_cfg = config.get("server", {})
    app = create_app(
        megapi,
        config,
        safety_monitor=safety_monitor,
        body_tracker=body_tracker,
    )

    try:
        uvicorn.run(
            app,
            host=server_cfg.get("host", "0.0.0.0"),
            port=int(server_cfg.get("port", 8080)),
            log_level="info",
        )
    finally:
        if body_tracker is not None:
            body_tracker.stop()
        if safety_monitor is not None:
            safety_monitor.stop()
        megapi.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
