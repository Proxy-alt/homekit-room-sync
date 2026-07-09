"""Tests for the HomeKitBridgeManager."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.homekit_room_sync.bridge_manager import (
    _as_str_set,
    _pick_new_port,
    BridgeConfig,
    HomeKitBridgeManager,
    parse_bridge_configs,
)
from custom_components.homekit_room_sync.const import CONF_BRIDGES, HOMEKIT_DOMAIN


def _mock_entry(
    entity_id: str,
    area_id: str | None = None,
    device_id: str | None = None,
    device_class: str | None = None,
) -> MagicMock:
    entry = MagicMock()
    entry.entity_id = entity_id
    entry.area_id = area_id
    entry.device_id = device_id
    entry.device_class = device_class
    entry.original_device_class = None
    return entry


def _entity_registry(entity_map: dict[str, MagicMock]) -> MagicMock:
    registry = MagicMock()
    registry.entities = entity_map
    registry.async_get = lambda entity_id: entity_map.get(entity_id)
    return registry


@pytest.mark.asyncio
async def test_manager_updates_homekit_entry(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """Manager should update the HomeKit filter based on configured areas.

    Room assignment is NOT part of this: the core `homekit` component has no
    "room" entity_config key at all, so it can never be set by a bridge --
    only a HomeKit controller app (e.g. HomeClaw) can do that. See
    scripts/setup_homekit_rooms_and_zones.py.
    """
    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room", "area_bedroom"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=mock_entity_registry,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    mock_hass.config_entries.async_update_entry.assert_called_once()
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    updated_data = update_kwargs["data"]
    assert updated_data["filter"]["include_entities"] == [
        "light.living_room",
        "switch.bedroom",
    ]
    # No linked sensors/type overrides apply to these fixtures, so nothing
    # needs writing to entity_config at all.
    assert updated_data["entity_config"] == {}
    mock_hass.config_entries.async_reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_respects_manual_overrides(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """Include/exclude overrides should adjust final entity list."""
    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset({"sensor.unknown"}),
        exclude_entities=frozenset({"switch.bedroom"}),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=mock_entity_registry,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    updated_entities = update_kwargs["data"]["filter"]["include_entities"]
    assert updated_entities == ["light.living_room", "sensor.unknown"]


@pytest.mark.asyncio
async def test_manager_preserves_existing_name_override(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """Syncing should not clobber a user's HomeKit name override."""
    mock_homekit_entry.data = {
        "filter": {},
        "entity_config": {
            "light.living_room": {"name": "Custom Lamp Name"},
        },
    }

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=mock_entity_registry,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    entity_config = update_kwargs["data"]["entity_config"]["light.living_room"]
    assert entity_config["name"] == "Custom Lamp Name"


@pytest.mark.asyncio
async def test_manager_strips_stale_room_key(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """A "room" key left over from before this was fixed should be dropped.

    The core `homekit` component has no "room" entity_config key, so this
    was always a silent no-op; leaving it in place is actively misleading.
    """
    mock_homekit_entry.data = {
        "filter": {},
        "entity_config": {
            "light.living_room": {"room": "Living Room"},
        },
    }

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=mock_entity_registry,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    entity_config = update_kwargs["data"]["entity_config"]
    # The whole entry is dropped since "room" was its only key.
    assert "light.living_room" not in entity_config


@pytest.mark.asyncio
async def test_manager_auto_links_battery_sensor(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """A same-device battery sensor should be auto-linked in entity_config."""
    ent_reg = _entity_registry(
        {
            "light.living_room": _mock_entry(
                "light.living_room", area_id="area_living_room", device_id="device_light"
            ),
            "sensor.living_room_battery": _mock_entry(
                "sensor.living_room_battery", device_id="device_light", device_class="battery"
            ),
        }
    )

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=ent_reg,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    entity_config = update_kwargs["data"]["entity_config"]["light.living_room"]
    assert entity_config["linked_battery_sensor"] == "sensor.living_room_battery"
    # The sibling battery sensor itself is a diagnostic entity, not exposed.
    assert "sensor.living_room_battery" not in update_kwargs["data"]["filter"]["include_entities"]


@pytest.mark.asyncio
async def test_manager_auto_link_does_not_override_manual_value(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """A manually-configured linked sensor should win over auto-detection."""
    mock_homekit_entry.data = {
        "filter": {},
        "entity_config": {
            "light.living_room": {"linked_battery_sensor": "sensor.custom_battery"},
        },
    }
    ent_reg = _entity_registry(
        {
            "light.living_room": _mock_entry(
                "light.living_room", area_id="area_living_room", device_id="device_light"
            ),
            "sensor.living_room_battery": _mock_entry(
                "sensor.living_room_battery", device_id="device_light", device_class="battery"
            ),
        }
    )

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=ent_reg,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    entity_config = update_kwargs["data"]["entity_config"]["light.living_room"]
    assert entity_config["linked_battery_sensor"] == "sensor.custom_battery"


@pytest.mark.asyncio
async def test_manager_auto_link_disabled(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """link_related_sensors=False should skip auto-linking entirely."""
    ent_reg = _entity_registry(
        {
            "light.living_room": _mock_entry(
                "light.living_room", area_id="area_living_room", device_id="device_light"
            ),
            "sensor.living_room_battery": _mock_entry(
                "sensor.living_room_battery", device_id="device_light", device_class="battery"
            ),
        }
    )

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
        link_related_sensors=False,
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=ent_reg,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    entity_config = update_kwargs["data"]["entity_config"]
    assert "light.living_room" not in entity_config


@pytest.mark.asyncio
async def test_manager_auto_detects_outlet_switch_type(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """A switch entity with device_class 'outlet' should get type: outlet."""
    ent_reg = _entity_registry(
        {
            "switch.plug": _mock_entry(
                "switch.plug",
                area_id="area_living_room",
                device_id="device_plug",
                device_class="outlet",
            ),
        }
    )

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=ent_reg,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    entity_config = update_kwargs["data"]["entity_config"]["switch.plug"]
    assert entity_config["type"] == "outlet"


def test_as_str_set_converts_non_strings() -> None:
    """_as_str_set should include non-string iterables as strings."""
    data = {1, "two", 3}
    assert _as_str_set(data) == {"1", "two", "3"}

    # Strings/bytes are treated as scalar, not iterable for our purposes
    assert _as_str_set("abc") == set()
    assert _as_str_set(b"bytes") == set()


def test_parse_bridge_configs_ignores_string_conf() -> None:
    """parse_bridge_configs should not iterate over string/bytes configs."""
    entry = MagicMock()
    entry.data = {CONF_BRIDGES: "not-a-list"}
    assert parse_bridge_configs(entry) == []


@pytest.mark.asyncio
async def test_manager_resolves_duplicate_port(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """A duplicate port should be reassigned before reloading HomeKit."""
    mock_homekit_entry.data = {
        "filter": {},
        "entity_config": {},
        "port": 21064,
    }

    other_entry = MagicMock()
    other_entry.entry_id = "other_entry"
    other_entry.data = {"port": 21064}

    def _async_entries(domain: str | None = None):
        if domain == HOMEKIT_DOMAIN:
            return [mock_homekit_entry, other_entry]
        return []

    mock_hass.config_entries.async_entries = MagicMock(side_effect=_async_entries)

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=mock_entity_registry,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    expected_port = _pick_new_port(mock_homekit_entry.entry_id, {21064})
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    assert update_kwargs["data"]["port"] == expected_port
    mock_hass.config_entries.async_reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_port_conflict_triggers_update_without_filter_change(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """Port conflicts are resolved even when no filter changes are detected."""
    mock_homekit_entry.data = {
        "filter": {},
        "entity_config": {},
        "port": 21064,
    }

    other_entry = MagicMock()
    other_entry.entry_id = "other_entry"
    other_entry.data = {"port": 21064}

    def _async_entries(domain: str | None = None):
        if domain == HOMEKIT_DOMAIN:
            return [mock_homekit_entry, other_entry]
        return []

    mock_hass.config_entries.async_entries = MagicMock(side_effect=_async_entries)

    config = BridgeConfig(
        entry_id=mock_homekit_entry.entry_id,
        areas=frozenset({"area_living_room"}),
        include_entities=frozenset(),
        exclude_entities=frozenset(),
    )
    manager = HomeKitBridgeManager(mock_hass, mock_config_entry, [config])

    with (
        patch(
            "custom_components.homekit_room_sync.bridge_manager.entity_registry.async_get",
            return_value=mock_entity_registry,
        ),
        patch(
            "custom_components.homekit_room_sync.bridge_manager.device_registry.async_get",
            return_value=mock_device_registry,
        ),
        patch.object(
            HomeKitBridgeManager,
            "_build_updated_data",
            return_value=None,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    expected_port = _pick_new_port(mock_homekit_entry.entry_id, {21064})
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    assert update_kwargs["data"]["port"] == expected_port
    mock_hass.config_entries.async_reload.assert_awaited_once()
