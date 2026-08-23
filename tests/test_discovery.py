"""Tests for discovery helpers and entity summarization."""

from __future__ import annotations

from datetime import date
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr

from custom_components.mcp_assist import discovery as discovery_module
from custom_components.mcp_assist.const import DOMAIN
from custom_components.mcp_assist.discovery import SmartDiscovery


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ordinary", "ordinary"),
        ("first\r\nsecond", "first\\r\\nsecond"),
        ("first\nsecond", "first\\nsecond"),
    ],
)
def test_safe_log_value_escapes_line_breaks(value: str, expected: str) -> None:
    """Discovery log values should stay on one physical log line."""
    assert discovery_module._safe_log_value(value) == expected


@pytest.mark.asyncio
async def test_entity_discovery_logs_escape_user_filters(hass, caplog, monkeypatch) -> None:
    """Entity filter and inferred-type logs should escape forged line breaks."""
    discovery = SmartDiscovery(hass)
    malicious_value = "ordinary\r\nFORGED: admin"
    index_manager = SimpleNamespace(
        get_index=AsyncMock(
            return_value={
                "inferred_types": {
                    malicious_value: {"pattern": "sensor.\nFORGED: pattern"},
                }
            }
        )
    )
    monkeypatch.setitem(hass.data, DOMAIN, {"index_manager": index_manager})

    with caplog.at_level(logging.DEBUG, logger=discovery_module._LOGGER.name):
        await discovery.discover_entities_page(
            name_contains=malicious_value,
            limit=5,
        )
        await discovery.discover_entities_page(
            inferred_type=malicious_value,
            limit=5,
        )
        index_manager.get_index.return_value = {"inferred_types": {}}
        await discovery.discover_entities_page(
            inferred_type="missing\r\nFORGED: missing",
            limit=5,
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == discovery_module._LOGGER.name
    ]
    assert all("\r" not in message and "\n" not in message for message in messages)
    assert any("ordinary\\r\\nFORGED: admin" in message for message in messages)
    assert any("sensor.\\nFORGED: pattern" in message for message in messages)
    assert any("missing\\r\\nFORGED: missing" in message for message in messages)


@pytest.mark.asyncio
async def test_device_discovery_logs_escape_all_user_filters(hass, caplog) -> None:
    """Every device discovery filter should be single-line in debug logs."""
    discovery = SmartDiscovery(hass)
    filters = {
        "area": "area\r\nFORGED-area",
        "floor": "floor\r\nFORGED-floor",
        "label": "label\r\nFORGED-label",
        "domain": "domain\r\nFORGED-domain",
        "name_contains": "name\r\nFORGED-name",
        "manufacturer": "manufacturer\r\nFORGED-manufacturer",
        "model": "model\r\nFORGED-model",
    }

    with (
        patch.object(
            discovery,
            "_resolve_area_entry",
            return_value=SimpleNamespace(id="area-id"),
        ),
        patch.object(
            discovery,
            "_resolve_floor_entry",
            return_value=SimpleNamespace(floor_id="floor-id"),
        ),
        patch.object(
            discovery,
            "_resolve_label_entry",
            return_value=SimpleNamespace(label_id="label-id"),
        ),
        caplog.at_level(logging.DEBUG, logger=discovery_module._LOGGER.name),
    ):
        await discovery.discover_devices_page(**filters)

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Device discovery found")
    )
    assert "\r" not in message and "\n" not in message
    for value in filters.values():
        assert discovery_module._safe_log_value(value) in message


def test_entity_match_details_include_alias_and_floor_context(hass) -> None:
    """Search scoring should account for aliases and floor context."""
    discovery = SmartDiscovery(hass)
    state_obj = SimpleNamespace(
        entity_id="lock.basement_door",
        name="Basement Door Lock",
        attributes={"friendly_name": "Basement Door Lock"},
    )
    entity_entry = SimpleNamespace(aliases={"Deadbolt"})
    entity_context = {
        "device": None,
        "device_name": None,
        "device_name_by_user": None,
        "device_aliases": [],
        "area": "Basement",
        "area_aliases": [],
        "floor": "Lower Level",
        "floor_aliases": ["Downstairs"],
        "labels": [],
    }

    score, reasons = discovery._get_entity_match_details(
        "deadbolt",
        state_obj,
        entity_entry,
        entity_context,
    )
    assert score > 0
    assert "entity alias" in reasons

    score, reasons = discovery._get_entity_match_details(
        "downstairs",
        state_obj,
        entity_entry,
        entity_context,
    )
    assert score > 0
    assert "floor alias" in reasons


def test_create_entity_info_includes_weather_forecast_hints(hass) -> None:
    """Weather discovery summaries should expose forecast availability and preview."""
    discovery = SmartDiscovery(hass)
    state_obj = SimpleNamespace(
        entity_id="weather.home",
        name="Home Weather",
        domain="weather",
        state="sunny",
        attributes={
            "friendly_name": "Home Weather",
            "temperature": 54,
            "forecast": [
                {"datetime": date(2026, 4, 12), "condition": "rainy", "temperature": 58},
                {"datetime": date(2026, 4, 13), "condition": "sunny", "temperature": 62},
                {"datetime": date(2026, 4, 14), "condition": "cloudy", "temperature": 60},
            ],
        },
    )

    dummy_registry = SimpleNamespace(async_get=lambda *_args, **_kwargs: None)
    dummy_area_registry = SimpleNamespace(async_get_area=lambda *_args, **_kwargs: None)

    with (
        patch("custom_components.mcp_assist.discovery.er.async_get", return_value=dummy_registry),
        patch("custom_components.mcp_assist.discovery.dr.async_get", return_value=dummy_registry),
        patch(
            "custom_components.mcp_assist.discovery.ar.async_get",
            return_value=dummy_area_registry,
        ),
    ):
        entity_info = discovery._create_entity_info(
            state_obj,
            entity_context={},
        )

    assert entity_info["forecast_available"] is True
    assert entity_info["forecast_entries"] == 3
    assert entity_info["forecast_types"] == ["daily"]
    assert entity_info["forecast_service_supported"] is True
    assert entity_info["forecast_preview"] == [
        {"datetime": "2026-04-12", "condition": "rainy", "temperature": 58},
        {"datetime": "2026-04-13", "condition": "sunny", "temperature": 62},
    ]


def test_format_smart_results_page_includes_paging_metadata(hass) -> None:
    """Smart discovery pagination should expose counts and the next offset."""
    discovery = SmartDiscovery(hass)

    page = discovery._format_smart_results_page(
        {
            "query": "alex",
            "query_type": "person",
            "primary_entities": [
                {"entity_id": "person.alex", "name": "Alex", "state": "home"},
            ],
            "related_entities": {
                "presence": [
                    {
                        "entity_id": "binary_sensor.alex_home",
                        "name": "Alex Home",
                        "state": "on",
                    }
                ],
                "room_tracking": [
                    {
                        "entity_id": "input_text.alex_room",
                        "name": "Alex Room",
                        "state": "Office",
                    }
                ],
            },
        },
        limit=2,
        offset=0,
    )

    assert page["total_found"] == 3
    assert page["returned_count"] == 2
    assert page["remaining_count"] == 1
    assert page["next_offset"] == 2
    assert page["items"][0]["entity_id"] == "_summary"
    assert page["items"][0]["next_offset"] == 2
    assert len(page["items"]) == 3


@pytest.mark.asyncio
async def test_area_discovery_honors_entity_type_for_area_and_floor(hass) -> None:
    """Legacy entity_type should still narrow area and floor discovery."""
    floor_registry = fr.async_get(hass)
    upstairs = floor_registry.async_create("Upstairs")
    area_registry = ar.async_get(hass)
    bedroom = area_registry.async_create("Blue Bedroom", floor_id=upstairs.floor_id)
    entity_registry = er.async_get(hass)

    light_entry = entity_registry.async_get_or_create(
        "light",
        "test",
        "upstairs_lamp",
        suggested_object_id="upstairs_lamp",
    )
    binary_sensor_entry = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "upstairs_motion",
        suggested_object_id="upstairs_motion",
    )
    entity_registry.async_update_entity(light_entry.entity_id, area_id=bedroom.id)
    entity_registry.async_update_entity(binary_sensor_entry.entity_id, area_id=bedroom.id)
    hass.states.async_set(
        "light.upstairs_lamp",
        "on",
        {"friendly_name": "Upstairs Lamp"},
    )
    hass.states.async_set(
        "binary_sensor.upstairs_motion",
        "on",
        {"friendly_name": "Upstairs Motion"},
    )

    discovery = SmartDiscovery(hass)

    with patch(
        "custom_components.mcp_assist.discovery.async_should_expose",
        return_value=True,
    ):
        floor_page = await discovery.discover_entities_page(
            entity_type="light",
            area="upstairs",
            limit=20,
        )
        area_page = await discovery.discover_entities_page(
            entity_type="light",
            area="Blue Bedroom",
            limit=20,
        )

    floor_entity_ids = [
        entity["entity_id"]
        for entity in floor_page["items"]
        if entity["entity_id"] != "_summary"
    ]
    area_entity_ids = [
        entity["entity_id"]
        for entity in area_page["items"]
        if entity["entity_id"] != "_summary"
    ]

    assert floor_entity_ids == ["light.upstairs_lamp"]
    assert floor_page["total_found"] == 1
    assert area_entity_ids == ["light.upstairs_lamp"]
    assert area_page["total_found"] == 1


@pytest.mark.asyncio
async def test_get_entity_details_includes_script_fields(hass) -> None:
    """Script entity details should include callable field metadata."""
    hass.states.async_set("script.good_morning", "off")
    discovery = SmartDiscovery(hass)
    hass.data["script"] = SimpleNamespace(
        get_entity=lambda entity_id: SimpleNamespace(
            fields={
                "message": {"description": "Announcement text"},
                "urgent": {},
            }
        )
        if entity_id == "script.good_morning"
        else None
    )

    with patch(
        "custom_components.mcp_assist.discovery.async_should_expose",
        return_value=True,
    ):
        details = await discovery.get_entity_details(["script.good_morning"])

    assert details["script.good_morning"]["fields"] == {
        "message": {"description": "Announcement text"},
        "urgent": {},
    }


@pytest.mark.asyncio
async def test_discovery_handles_regex_metacharacters_in_names(hass) -> None:
    """Model-supplied names with regex metacharacters must not crash discovery."""
    discovery = SmartDiscovery(hass)
    hass.states.async_set("light.kitchen", "on")
    hass.states.async_set("input_text.room_alex", "kitchen")

    # Previously raised re.error (unbalanced parenthesis) from the pattern checks.
    assert discovery._is_likely_person_name("alex (phone") is False

    with patch(
        "custom_components.mcp_assist.discovery.async_should_expose",
        return_value=True,
    ):
        person_page = await discovery._discover_person_entities_page("alex (phone", limit=5)
        pet_page = await discovery._discover_pet_entities_page("rex [cat", limit=5)

    assert person_page["items"] == []
    assert pet_page["items"] == []


@pytest.mark.asyncio
async def test_area_discovery_with_empty_area_list_returns_page(hass) -> None:
    """The defensive empty-list branch must return a page dict, not a bare list."""
    discovery = SmartDiscovery(hass)

    page = await discovery._discover_area_entities_page([], None, None, None, 5, 0)

    assert page["items"] == []
    assert page["total_found"] == 0
