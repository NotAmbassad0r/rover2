"""Fake motor sink — records commands sent to it, never actuates hardware."""
from __future__ import annotations

from typing import Any


class FakeMotorSink:
    """Drop-in replacement for megapi.send / SafetyMonitor._orig_send."""

    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []

    def __call__(self, command: dict[str, Any]) -> None:
        self.commands.append(dict(command))

    def last(self) -> dict[str, Any] | None:
        return self.commands[-1] if self.commands else None

    def clear(self) -> None:
        self.commands.clear()
