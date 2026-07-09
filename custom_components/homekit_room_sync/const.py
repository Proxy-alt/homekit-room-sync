"""Constants for the HomeKit Room Sync integration."""

from typing import Final

DOMAIN: Final = "homekit_room_sync"

# New schema keys
CONF_BRIDGES: Final = "bridges"
CONF_ENTRY_ID: Final = "entry_id"
CONF_AREAS: Final = "areas"
CONF_INCLUDE_ENTITIES: Final = "include_entities"
CONF_EXCLUDE_ENTITIES: Final = "exclude_entities"
CONF_LINK_RELATED_SENSORS: Final = "link_related_sensors"

# HomeKit Bridge entity_config keys this integration can auto-populate.
# These mirror homeassistant.components.homekit.const so we write values the
# core `homekit` integration already understands without requiring users to
# hand-edit YAML for them.
CONF_LINKED_BATTERY_SENSOR: Final = "linked_battery_sensor"
CONF_LINKED_BATTERY_CHARGING_SENSOR: Final = "linked_battery_charging_sensor"
CONF_LINKED_HUMIDITY_SENSOR: Final = "linked_humidity_sensor"
CONF_LINKED_TEMPERATURE_SENSOR: Final = "linked_temperature_sensor"
CONF_LINKED_PM25_SENSOR: Final = "linked_pm25_sensor"
CONF_LINKED_MOTION_SENSOR: Final = "linked_motion_sensor"
CONF_LINKED_DOORBELL_SENSOR: Final = "linked_doorbell_sensor"
CONF_ENTITY_TYPE: Final = "type"

# Legacy keys retained for migration only
CONF_BRIDGE_NAME: Final = "bridge_name"
CONF_MANAGED_BRIDGES: Final = "managed_bridges"
CONF_BRIDGE_ID: Final = "bridge_id"
CONF_BRIDGE_TITLE: Final = "bridge_title"
CONF_ALLOWED_AREAS: Final = "allowed_areas"
CONF_DEFAULT_ROOM: Final = "default_room"

# Event types to listen for
EVENT_ENTITY_REGISTRY_UPDATED: Final = "entity_registry_updated"
EVENT_AREA_REGISTRY_UPDATED: Final = "area_registry_updated"
EVENT_DEVICE_REGISTRY_UPDATED: Final = "device_registry_updated"

# Debounce delay in seconds
SYNC_DEBOUNCE_DELAY: Final = 0.5

# HomeKit service
HOMEKIT_DOMAIN: Final = "homekit"
SERVICE_RELOAD: Final = "reload"

# Services
SERVICE_SYNC: Final = "sync"

# Service attributes
ATTR_ENTRY_ID: Final = "entry_id"
ATTR_BRIDGE_ID: Final = "bridge_id"
