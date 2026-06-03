"""Lightweight async Home Assistant client for ROVER2.

Reads HA_URL and HA_TOKEN from environment (set via /etc/rover2.env).
All methods return empty/False on failure — never raise. 5 s timeout.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Spoken name overrides ─────────────────────────────────────────────────────
# Maps entity_id → short spoken name used in voice replies.
# If an entity_id is not listed, the HA friendly_name is used.
_ENTITY_NAMES: dict[str, str] = {
    "sensor.sonoff_snzb_02d_temperature":            "living room",
    "sensor.sonoff_snzb_02d_temperature_2":          "bedroom",
    "sensor.lumi_lumi_airmonitor_acn01_temperatuur": "office",
    "sensor.samjin_multi_temperatuur":               "hallway",
    "sensor.samjin_multi_temperatuur_2":             "kitchen",
    "sensor.tp357_8805_temperature":                 "bathroom",
    "switch.smart_power_strip_socket_1":             "power strip socket 1",
    "light.office_go":                               "office light",
    "switch.hue_smart_plug_1":                       "hue plug",
    "media_player.tv_one":                           "living room TV",
    "media_player.smart_tv_pro":                     "smart TV",
    "media_player.nesthubmax7edd":                   "Nest Hub",
    "sensor.flippy_6_battery_level":                 "Flip 6 battery",
}


def friendly_name(entity_id: str, ha_name: str | None = None) -> str:
    """Return a short spoken name for an entity."""
    if entity_id in _ENTITY_NAMES:
        return _ENTITY_NAMES[entity_id]
    if ha_name:
        return ha_name
    return entity_id.split(".", 1)[-1].replace("_", " ")


class HAClient:
    """Async HA REST client. All calls are fire-and-forget safe."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── Core calls ────────────────────────────────────────────────────────────

    async def get_states(self) -> list[dict]:
        """Return all entity states from HA."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self._url}/api/states", headers=self._headers)
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            logger.warning("HA get_states failed: %s", exc)
            return []

    async def get_state(self, entity_id: str) -> dict | None:
        """Return a single entity state, or None if not found / unreachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(
                    f"{self._url}/api/states/{entity_id}", headers=self._headers
                )
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            logger.warning("HA get_state(%s) failed: %s", entity_id, exc)
            return None

    async def call_service(
        self, domain: str, service: str, entity_id: str
    ) -> bool:
        """Call a HA service (e.g. light.turn_on)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(
                    f"{self._url}/api/services/{domain}/{service}",
                    headers=self._headers,
                    json={"entity_id": entity_id},
                )
                r.raise_for_status()
                return True
        except Exception as exc:
            logger.warning(
                "HA call_service(%s.%s, %s) failed: %s", domain, service, entity_id, exc
            )
            return False

    async def toggle(self, entity_id: str) -> bool:
        """Toggle an entity. Uses homeassistant.toggle for most; media_play_pause for players."""
        domain = entity_id.split(".")[0]
        if domain == "media_player":
            return await self.call_service("media_player", "media_play_pause", entity_id)
        return await self.call_service("homeassistant", "toggle", entity_id)

    # ── Convenience queries ───────────────────────────────────────────────────

    async def get_temperature_sensors(self) -> list[dict]:
        """Return all temperature sensors with friendly name, value, unit."""
        states = await self.get_states()
        results: list[dict] = []
        for s in states:
            eid = s.get("entity_id", "")
            if not eid.startswith("sensor."):
                continue
            attrs = s.get("attributes", {})
            unit = attrs.get("unit_of_measurement", "")
            if unit not in ("°C", "°F", "C", "F"):
                continue
            try:
                val = float(s.get("state", ""))
            except (ValueError, TypeError):
                continue
            results.append({
                "entity_id": eid,
                "name": friendly_name(eid, attrs.get("friendly_name")),
                "temperature": round(val, 1),
                "unit": unit,
                "humidity": attrs.get("humidity"),
            })
        return results

    async def get_toggleable_entities(self) -> list[dict]:
        """Return all switches, lights, and media_players with their current state."""
        states = await self.get_states()
        results: list[dict] = []
        for s in states:
            eid = s.get("entity_id", "")
            domain = eid.split(".")[0]
            if domain not in ("switch", "light", "media_player"):
                continue
            attrs = s.get("attributes", {})
            results.append({
                "entity_id": eid,
                "name": friendly_name(eid, attrs.get("friendly_name")),
                "state": s.get("state", "unknown"),
                "domain": domain,
            })
        return results

    # ── Connectivity check ────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """Return True if HA API is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self._url}/api/", headers=self._headers)
                return r.status_code == 200
        except Exception:
            return False
