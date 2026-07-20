"""Diagnostics support for the Hai integration."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import HaiConfigEntry


def _json_safe(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, frozenset):
            result[key] = sorted(value)
        else:
            result[key] = value
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaiConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    snapshot = coordinator.last_snapshot
    return {
        "entry": {
            "title": entry.title,
            "address": entry.unique_id,
            "version": entry.version,
            "minor_version": entry.minor_version,
        },
        "coordinator": {
            "available": coordinator.available,
            "last_poll_successful": coordinator.last_poll_successful,
            "wake_generation": coordinator.tracker.generation,
            "live_data_fresh": coordinator.tracker.live_data_fresh,
        },
        # The bootloader version lives here on purpose: DeviceInfo has no
        # bootloader field and it must not masquerade as hw_version.
        "snapshot": asdict(snapshot, dict_factory=_json_safe) if snapshot else None,
    }
