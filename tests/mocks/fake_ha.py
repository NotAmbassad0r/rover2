"""Fake HA client — returns synthetic entity states, no network."""
from __future__ import annotations

from typing import Any


class FakeHAClient:
    """Drop-in replacement for ha_client.HAClient."""

    def __init__(self, states: list[dict[str, Any]] | None = None) -> None:
        self._states = states or []
        self.calls: list[tuple[str, Any]] = []

    async def get_states(self) -> list[dict]:
        self.calls.append(("get_states", None))
        return list(self._states)

    async def get_state(self, entity_id: str) -> dict | None:
        self.calls.append(("get_state", entity_id))
        for s in self._states:
            if s.get("entity_id") == entity_id:
                return s
        return None

    async def call_service(self, domain: str, service: str, entity_id: str) -> bool:
        self.calls.append(("call_service", (domain, service, entity_id)))
        return True

    async def toggle(self, entity_id: str) -> bool:
        self.calls.append(("toggle", entity_id))
        return True

    async def ping(self) -> bool:
        self.calls.append(("ping", None))
        return True
