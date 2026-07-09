"""Bridge management helpers for HomeKit Room Sync."""

from __future__ import annotations

import asyncio
import copy
import logging
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry

from .const import (
    CONF_AREAS,
    CONF_BRIDGES,
    CONF_ENTITY_TYPE,
    CONF_ENTRY_ID,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_ENTITIES,
    CONF_LINK_RELATED_SENSORS,
    CONF_LINKED_BATTERY_CHARGING_SENSOR,
    CONF_LINKED_BATTERY_SENSOR,
    CONF_LINKED_DOORBELL_SENSOR,
    CONF_LINKED_HUMIDITY_SENSOR,
    CONF_LINKED_MOTION_SENSOR,
    CONF_LINKED_PM25_SENSOR,
    CONF_LINKED_TEMPERATURE_SENSOR,
    HOMEKIT_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_PORT_RANGE_START: Final = 20000
_PORT_RANGE_SIZE: Final = 30000

# Sibling-sensor auto-linking rules: a target entity gets `config_key` set to
# a same-device sibling entity when one exists matching (sibling_domain,
# device_class). `target_domains=None` means the rule applies to any target
# domain (e.g. battery level/charging is meaningful for almost any accessory).
# These mirror homeassistant.components.homekit's entity_config options so
# HomeKit surfaces (battery %, filter/AQI cards, motion-triggered doorbell
# notifications) work without the user hand-editing YAML.
_LINKED_SENSOR_RULES: Final[tuple[tuple[frozenset[str] | None, str, str, str], ...]] = (
    (None, "sensor", "battery", CONF_LINKED_BATTERY_SENSOR),
    (None, "binary_sensor", "battery_charging", CONF_LINKED_BATTERY_CHARGING_SENSOR),
    (frozenset({"humidifier"}), "sensor", "humidity", CONF_LINKED_HUMIDITY_SENSOR),
    (frozenset({"fan"}), "sensor", "temperature", CONF_LINKED_TEMPERATURE_SENSOR),
    (frozenset({"fan"}), "sensor", "pm25", CONF_LINKED_PM25_SENSOR),
    (frozenset({"camera", "lock"}), "binary_sensor", "motion", CONF_LINKED_MOTION_SENSOR),
    (frozenset({"camera", "lock"}), "event", "doorbell", CONF_LINKED_DOORBELL_SENSOR),
)


def _preferred_port(entry_id: str) -> int:
    """Return a deterministic preferred port for a HomeKit entry."""
    digest = zlib.adler32(entry_id.encode("utf-8")) & 0xFFFFFFFF
    return _PORT_RANGE_START + (digest % _PORT_RANGE_SIZE)


def _pick_new_port(entry_id: str, used_ports: set[int]) -> int | None:
    """Pick a deterministic port that is not currently reserved."""
    preferred = _preferred_port(entry_id)
    normalized = preferred - _PORT_RANGE_START
    for offset in range(_PORT_RANGE_SIZE):
        candidate = _PORT_RANGE_START + ((normalized + offset) % _PORT_RANGE_SIZE)
        if candidate not in used_ports:
            return candidate
    return None


def _as_str_set(values: object) -> set[str]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return set()

    result: set[str] = set()
    for item in values:
        stringified = str(item).strip()
        if stringified:
            result.add(stringified)
    return result


@dataclass(slots=True)
class BridgeConfig:
    """Normalized configuration for a managed HomeKit bridge."""

    entry_id: str
    areas: frozenset[str]
    include_entities: frozenset[str]
    exclude_entities: frozenset[str]
    link_related_sensors: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> BridgeConfig | None:
        entry_id = str(raw.get(CONF_ENTRY_ID) or "").strip()
        if not entry_id:
            return None

        return cls(
            entry_id=entry_id,
            areas=frozenset(_as_str_set(raw.get(CONF_AREAS))),
            include_entities=frozenset(_as_str_set(raw.get(CONF_INCLUDE_ENTITIES))),
            exclude_entities=frozenset(_as_str_set(raw.get(CONF_EXCLUDE_ENTITIES))),
            link_related_sensors=bool(raw.get(CONF_LINK_RELATED_SENSORS, True)),
        )

    def serialize(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return {
            CONF_ENTRY_ID: self.entry_id,
            CONF_AREAS: sorted(self.areas),
            CONF_INCLUDE_ENTITIES: sorted(self.include_entities),
            CONF_EXCLUDE_ENTITIES: sorted(self.exclude_entities),
            CONF_LINK_RELATED_SENSORS: self.link_related_sensors,
        }


@dataclass(slots=True)
class ManagedBridgeConfig:
    """Compatibility config used by legacy coordinator tests."""

    bridge_id: str
    friendly_name: str
    allowed_areas: set[str]
    include_entities: set[str]
    exclude_entities: set[str]
    default_room: str | None = None


def parse_bridge_configs(entry: ConfigEntry) -> list[BridgeConfig]:
    """Parse bridge configs from the integration entry."""
    configs: list[BridgeConfig] = []
    stored = entry.data.get(CONF_BRIDGES)
    if isinstance(stored, Iterable) and not isinstance(stored, (str, bytes)):
        for raw in stored:
            if isinstance(raw, dict) and (cfg := BridgeConfig.from_dict(raw)):
                configs.append(cfg)
    return configs


class HomeKitBridgeManager:
    """Compute entity filters and push them into HomeKit config entries."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        bridge_configs: list[BridgeConfig],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._configs = {cfg.entry_id: cfg for cfg in bridge_configs}
        self._sync_lock = asyncio.Lock()

    @property
    def bridge_ids(self) -> list[str]:
        """Return managed HomeKit entry_ids."""
        return list(self._configs.keys())

    async def async_sync(self, bridge_entry_id: str | None = None) -> bool:
        """Recompute filters for one or all bridges."""
        async with self._sync_lock:
            return await self._async_sync_unlocked(bridge_entry_id)

    async def _async_sync_unlocked(self, bridge_entry_id: str | None) -> bool:
        if bridge_entry_id and bridge_entry_id not in self._configs:
            _LOGGER.warning(
                "Sync requested for unknown HomeKit entry_id %s",
                bridge_entry_id,
            )
            return False

        targets = (
            [self._configs[bridge_entry_id]] if bridge_entry_id else list(self._configs.values())
        )

        if not targets:
            _LOGGER.debug("No HomeKit bridges configured for %s", self._entry.entry_id)
            return True

        results = await asyncio.gather(
            *(self._async_sync_bridge(cfg) for cfg in targets),
            return_exceptions=True,
        )
        success = True
        for cfg, result in zip(targets, results, strict=False):
            if isinstance(result, Exception):
                success = False
                _LOGGER.error(
                    "Failed to sync HomeKit entry %s: %s",
                    cfg.entry_id,
                    result,
                )
            elif not result:
                success = False
        return success

    async def async_shutdown(self) -> None:
        """Placeholder for future resources."""
        self._configs.clear()

    async def _async_sync_bridge(self, config: BridgeConfig) -> bool:
        homekit_entry = self._hass.config_entries.async_get_entry(config.entry_id)
        if homekit_entry is None:
            _LOGGER.warning(
                "HomeKit entry %s is no longer available; skipping",
                config.entry_id,
            )
            return False

        ent_reg = entity_registry.async_get(self._hass)
        dev_reg = device_registry.async_get(self._hass)

        area_entities = self._entities_in_areas(config, ent_reg, dev_reg)
        allowed_entities = sorted(
            (area_entities | set(config.include_entities)) - set(config.exclude_entities)
        )
        auto_entity_config = self._compute_auto_entity_config(config, allowed_entities, ent_reg)

        updated_data = self._build_updated_data(
            homekit_entry,
            allowed_entities,
            auto_entity_config,
        )
        if updated_data is None:
            # No filter/entity updates, but we still might need to adjust the port.
            updated_data = copy.deepcopy(dict(homekit_entry.data))

        port_changed = self._resolve_port_conflicts(homekit_entry, updated_data)
        if not port_changed and updated_data == homekit_entry.data:
            _LOGGER.debug(
                "No HomeKit data changes detected for entry %s",
                config.entry_id,
            )
            return True

        self._hass.config_entries.async_update_entry(
            homekit_entry,
            data=updated_data,
        )
        await self._hass.config_entries.async_reload(homekit_entry.entry_id)
        _LOGGER.info(
            "Synchronized %s entities for HomeKit entry %s",
            len(allowed_entities),
            homekit_entry.entry_id,
        )
        return True

    def _entities_in_areas(
        self,
        config: BridgeConfig,
        ent_reg,
        dev_reg,
    ) -> set[str]:
        """Return entity_ids that live inside the configured areas."""
        entries = getattr(ent_reg, "entities", {})
        if not entries:
            return set()

        entities: set[str] = set()
        if not config.areas:
            # Do not include anything when no areas are selected; only manual includes apply.
            return set()

        for entry in entries.values():
            area_id = entry.area_id or self._device_area_id(dev_reg, entry.device_id)
            if area_id in config.areas:
                entities.add(entry.entity_id)
        return entities

    @staticmethod
    def _device_area_id(dev_reg, device_id: str | None) -> str | None:
        if not device_id or not hasattr(dev_reg, "async_get"):
            return None
        device = dev_reg.async_get(device_id)
        return device.area_id if device else None

    def _compute_auto_entity_config(
        self,
        config: BridgeConfig,
        allowed_entities: list[str],
        ent_reg,
    ) -> dict[str, dict[str, str]]:
        """Auto-detect linked sensors/type overrides from same-device siblings.

        Only ever *proposes* values; `_build_updated_data` merges them in
        additively so a value the user already set (manually, or via the
        native HomeKit integration UI) always wins.
        """
        if not config.link_related_sensors or not hasattr(ent_reg, "async_get"):
            return {}

        entries = getattr(ent_reg, "entities", {})
        if not entries:
            return {}

        siblings_by_device: dict[str, list] = {}
        for entry in entries.values():
            if entry.device_id:
                siblings_by_device.setdefault(entry.device_id, []).append(entry)

        auto_config: dict[str, dict[str, str]] = {}
        for entity_id in allowed_entities:
            target_entry = ent_reg.async_get(entity_id)
            if target_entry is None or not target_entry.device_id:
                continue

            domain = entity_id.split(".", 1)[0]
            siblings = siblings_by_device.get(target_entry.device_id, [])
            overrides: dict[str, str] = {}

            for target_domains, sibling_domain, device_class, config_key in _LINKED_SENSOR_RULES:
                if target_domains is not None and domain not in target_domains:
                    continue
                match = self._find_sibling(siblings, entity_id, sibling_domain, device_class)
                if match:
                    overrides[config_key] = match

            if domain == "switch" and self._device_class(target_entry) == "outlet":
                overrides[CONF_ENTITY_TYPE] = "outlet"

            if overrides:
                auto_config[entity_id] = overrides

        return auto_config

    @staticmethod
    def _device_class(entry) -> str | None:
        return getattr(entry, "device_class", None) or getattr(entry, "original_device_class", None)

    @classmethod
    def _find_sibling(
        cls,
        siblings: list,
        exclude_entity_id: str,
        sibling_domain: str,
        device_class: str,
    ) -> str | None:
        matches = sorted(
            sibling.entity_id
            for sibling in siblings
            if sibling.entity_id != exclude_entity_id
            and sibling.entity_id.split(".", 1)[0] == sibling_domain
            and cls._device_class(sibling) == device_class
        )
        return matches[0] if matches else None

    @staticmethod
    def _build_updated_data(
        homekit_entry: ConfigEntry,
        allowed_entities: list[str],
        auto_entity_config: dict[str, dict[str, str]],
    ) -> dict[str, object] | None:
        # ConfigEntry.data is a MappingProxyType; convert to a mutable dict before copying.
        new_data = copy.deepcopy(dict(homekit_entry.data))
        new_data["filter"] = {
            "include_entities": allowed_entities,
            "exclude_entities": [],
        }

        existing_entity_config = dict(new_data.get("entity_config") or {})
        for entity_id in allowed_entities:
            existing_entry = dict(existing_entity_config.get(entity_id, {}))
            # Older versions of this integration wrote a "room" key here, but
            # the core `homekit` component never reads it -- HomeKit Room
            # assignment can only be set by a HomeKit controller app (e.g.
            # HomeClaw), not a bridge. Drop any stale leftover value.
            existing_entry.pop("room", None)
            for key, value in auto_entity_config.get(entity_id, {}).items():
                existing_entry.setdefault(key, value)
            if existing_entry:
                existing_entity_config[entity_id] = existing_entry
            else:
                existing_entity_config.pop(entity_id, None)
        new_data["entity_config"] = existing_entity_config

        if new_data == homekit_entry.data:
            return None
        return new_data

    def _current_homekit_ports(self) -> dict[str, int]:
        """Return a map of HomeKit entry_id to the currently configured port."""
        entries = self._hass.config_entries.async_entries(HOMEKIT_DOMAIN)
        result: dict[str, int] = {}
        for entry in entries:
            port = entry.data.get("port")
            if isinstance(port, int):
                result[entry.entry_id] = port
        return result

    def _resolve_port_conflicts(
        self,
        homekit_entry: ConfigEntry,
        new_data: dict[str, object],
    ) -> bool:
        """Ensure the HomeKit entry is not configured to use a duplicate port."""
        current_port = new_data.get("port")
        if not isinstance(current_port, int):
            return False

        port_map = self._current_homekit_ports()
        duplicates = [
            entry_id
            for entry_id, port in port_map.items()
            if entry_id != homekit_entry.entry_id and port == current_port
        ]
        if not duplicates:
            return False

        used_ports = {
            port for entry_id, port in port_map.items() if entry_id != homekit_entry.entry_id
        }
        replacement = _pick_new_port(homekit_entry.entry_id, used_ports)
        if replacement is None:
            _LOGGER.error(
                "Unable to resolve HomeKit port conflict for %s; "
                "multiple bridges are configured to use %s",
                homekit_entry.entry_id,
                current_port,
            )
            return False

        new_data["port"] = replacement
        _LOGGER.warning(
            "HomeKit entry %s shared TCP port %s with %s; reassigned to %s",
            homekit_entry.entry_id,
            current_port,
            ", ".join(sorted(duplicates)),
            replacement,
        )
        return True
