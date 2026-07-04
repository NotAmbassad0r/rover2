"""Tests for pi/ha_client.py — HA client with mocked HTTP, no network."""
from __future__ import annotations

import pytest
import httpx
import respx

from ha_client import HAClient, friendly_name

_BASE = "http://homeassistant.local:8123"


@pytest.fixture
def client() -> HAClient:
    return HAClient(url=_BASE, token="test-token")


# ── friendly_name (pure logic — class A) ────────────────────────────────────

class TestFriendlyName:
    def test_known_entity_returns_override(self):
        name = friendly_name("sensor.sonoff_snzb_02d_temperature")
        assert name == "living room"

    def test_unknown_entity_with_ha_name(self):
        name = friendly_name("sensor.unknown_xyz", ha_name="My Sensor")
        assert name == "My Sensor"

    def test_unknown_entity_no_ha_name_formats_entity_id(self):
        name = friendly_name("sensor.kitchen_temperature")
        assert name == "kitchen temperature"

    def test_entity_with_domain_prefix_stripped(self):
        name = friendly_name("switch.my_switch")
        assert name == "my switch"


# ── HAClient (mocked HTTP) ───────────────────────────────────────────────────

class TestGetStates:
    @respx.mock
    async def test_200_returns_list(self, client: HAClient):
        respx.get(f"{_BASE}/api/states").mock(
            return_value=httpx.Response(200, json=[
                {"entity_id": "light.test", "state": "on", "attributes": {}},
            ])
        )
        states = await client.get_states()
        assert len(states) == 1
        assert states[0]["entity_id"] == "light.test"

    @respx.mock
    async def test_network_error_returns_empty_list(self, client: HAClient):
        respx.get(f"{_BASE}/api/states").mock(side_effect=httpx.ConnectError("refused"))
        states = await client.get_states()
        assert states == []


class TestGetState:
    @respx.mock
    async def test_200_returns_dict(self, client: HAClient):
        entity = {"entity_id": "light.office_go", "state": "off", "attributes": {}}
        respx.get(f"{_BASE}/api/states/light.office_go").mock(
            return_value=httpx.Response(200, json=entity)
        )
        result = await client.get_state("light.office_go")
        assert result is not None
        assert result["state"] == "off"

    @respx.mock
    async def test_404_returns_none(self, client: HAClient):
        respx.get(f"{_BASE}/api/states/sensor.missing").mock(
            return_value=httpx.Response(404)
        )
        result = await client.get_state("sensor.missing")
        assert result is None

    @respx.mock
    async def test_network_error_returns_none(self, client: HAClient):
        respx.get(f"{_BASE}/api/states/sensor.x").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = await client.get_state("sensor.x")
        assert result is None


class TestToggle:
    @respx.mock
    async def test_media_player_calls_play_pause(self, client: HAClient):
        route = respx.post(f"{_BASE}/api/services/media_player/media_play_pause").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await client.toggle("media_player.tv_one")
        assert result is True
        assert route.called

    @respx.mock
    async def test_light_calls_homeassistant_toggle(self, client: HAClient):
        route = respx.post(f"{_BASE}/api/services/homeassistant/toggle").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await client.toggle("light.office_go")
        assert result is True
        assert route.called

    @respx.mock
    async def test_network_error_returns_false(self, client: HAClient):
        respx.post(f"{_BASE}/api/services/homeassistant/toggle").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = await client.toggle("light.office_go")
        assert result is False


class TestGetTemperatureSensors:
    @respx.mock
    async def test_filters_temperature_sensors(self, client: HAClient):
        states = [
            {
                "entity_id": "sensor.living_room_temp",
                "state": "21.5",
                "attributes": {"unit_of_measurement": "°C", "friendly_name": "Living Room"},
            },
            {
                "entity_id": "sensor.humidity",
                "state": "55",
                "attributes": {"unit_of_measurement": "%"},
            },
            {
                "entity_id": "light.office_go",
                "state": "on",
                "attributes": {},
            },
        ]
        respx.get(f"{_BASE}/api/states").mock(
            return_value=httpx.Response(200, json=states)
        )
        result = await client.get_temperature_sensors()
        assert len(result) == 1
        assert result[0]["entity_id"] == "sensor.living_room_temp"
        assert result[0]["temperature"] == 21.5

    @respx.mock
    async def test_invalid_state_value_skipped(self, client: HAClient):
        states = [{
            "entity_id": "sensor.broken",
            "state": "unavailable",
            "attributes": {"unit_of_measurement": "°C"},
        }]
        respx.get(f"{_BASE}/api/states").mock(
            return_value=httpx.Response(200, json=states)
        )
        result = await client.get_temperature_sensors()
        assert result == []


class TestGetToggleableEntities:
    @respx.mock
    async def test_filters_domains(self, client: HAClient):
        states = [
            {"entity_id": "light.office_go", "state": "on", "attributes": {"friendly_name": "Office"}},
            {"entity_id": "switch.strip_1", "state": "off", "attributes": {}},
            {"entity_id": "media_player.tv", "state": "idle", "attributes": {}},
            {"entity_id": "sensor.temp", "state": "21", "attributes": {}},
        ]
        respx.get(f"{_BASE}/api/states").mock(
            return_value=httpx.Response(200, json=states)
        )
        result = await client.get_toggleable_entities()
        entity_ids = [e["entity_id"] for e in result]
        assert "light.office_go" in entity_ids
        assert "switch.strip_1" in entity_ids
        assert "media_player.tv" in entity_ids
        assert "sensor.temp" not in entity_ids


class TestPing:
    @respx.mock
    async def test_200_returns_true(self, client: HAClient):
        respx.get(f"{_BASE}/api/").mock(return_value=httpx.Response(200))
        assert await client.ping() is True

    @respx.mock
    async def test_non_200_returns_false(self, client: HAClient):
        respx.get(f"{_BASE}/api/").mock(return_value=httpx.Response(401))
        assert await client.ping() is False

    @respx.mock
    async def test_connection_error_returns_false(self, client: HAClient):
        respx.get(f"{_BASE}/api/").mock(side_effect=httpx.ConnectError("refused"))
        assert await client.ping() is False
