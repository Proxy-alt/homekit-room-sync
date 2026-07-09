"""Mock subset of homeassistant.helpers.config_validation."""

from __future__ import annotations

from typing import Iterable

import voluptuous as vol


def multi_select(_options: dict[str, str] | None = None):  # noqa: D401
    """Return a validator that coerces to a list of strings."""

    def validate(value: object) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, Iterable):
            return [str(item) for item in value]
        raise vol.Invalid("Invalid multi_select value")

    return validate


def boolean(value: object) -> bool:
    """Mimic homeassistant.helpers.config_validation.boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("1", "true", "yes", "on", "enable"):
            return True
        if value.lower() in ("0", "false", "no", "off", "disable"):
            return False
        raise vol.Invalid(f"invalid boolean value {value!r}")
    raise vol.Invalid(f"invalid boolean value {value!r}")
