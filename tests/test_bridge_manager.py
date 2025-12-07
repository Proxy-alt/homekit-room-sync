"""Tests for the HomeKitBridgeManager."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.homekit_room_sync.bridge_manager import (
    BridgeConfig,
    HomeKitBridgeManager,
)


@pytest.mark.asyncio
async def test_manager_updates_homekit_entry(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_area_registry: MagicMock,
    mock_homekit_entry: MagicMock,
) -> None:
    """Manager should update filter + entity_config based on areas."""
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
        patch(
            "custom_components.homekit_room_sync.bridge_manager.area_registry.async_get",
            return_value=mock_area_registry,
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
    entity_config = updated_data["entity_config"]
    assert entity_config["light.living_room"]["room"] == "Living Room"
    assert entity_config["switch.bedroom"]["room"] == "Bedroom"
    mock_hass.config_entries.async_reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_respects_manual_overrides(
    mock_hass: MagicMock,
    mock_config_entry: MagicMock,
    mock_entity_registry: MagicMock,
    mock_device_registry: MagicMock,
    mock_area_registry: MagicMock,
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
        patch(
            "custom_components.homekit_room_sync.bridge_manager.area_registry.async_get",
            return_value=mock_area_registry,
        ),
    ):
        result = await manager.async_sync()

    assert result is True
    update_kwargs = mock_hass.config_entries.async_update_entry.call_args[1]
    updated_entities = update_kwargs["data"]["filter"]["include_entities"]
    assert updated_entities == ["light.living_room", "sensor.unknown"]
    entity_config = update_kwargs["data"]["entity_config"]
    assert entity_config["sensor.unknown"]["room"] is None
