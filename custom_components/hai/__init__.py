"""The Hai integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import HaiConfigEntry, HaiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# v1 unique IDs were "{ble_name} {product_id}_{key}"; v2 uses the processor
# entity format "{address}-{key}". Longest suffixes first so no legacy key can
# shadow another.
_LEGACY_KEY_MAP: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("current_volume", "current_volume"),
            ("total_volume", "lifetime_volume"),
            ("current_temperature", "current_temperature"),
            ("average_temperature", "average_temperature"),
            ("current_duration", "current_duration"),
            ("last_shower_duration", "last_shower_duration"),
            ("last_shower_temperature", "last_shower_temperature"),
            ("last_shower_volume", "last_shower_volume"),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


async def async_setup_entry(hass: HomeAssistant, entry: HaiConfigEntry) -> bool:
    """Set up Hai from a config entry.

    Setup must succeed with no advertisement present: a sleeping shower head
    is normal. Entities restore from processor storage and polling starts on
    the next wake advertisement, so there is no first refresh here.
    """
    address = entry.unique_id
    assert address is not None

    coordinator = HaiCoordinator(hass, entry, address)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start only after every platform has registered its processor so the
    # first replayed advertisement reaches all of them.
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HaiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: HaiConfigEntry) -> bool:
    """Migrate old config entries to the current schema."""
    if entry.version > 1:
        # Downgrade from a future major version: refuse to guess.
        return False

    if entry.version == 1 and entry.minor_version < 2:
        await _async_migrate_unique_ids(hass, entry)
        hass.config_entries.async_update_entry(
            entry, version=1, minor_version=2
        )

    return True


async def _async_migrate_unique_ids(
    hass: HomeAssistant, entry: HaiConfigEntry
) -> None:
    """Move v1 name-based sensor unique IDs to the processor format."""
    address = entry.unique_id
    assert address is not None
    entity_registry = er.async_get(hass)

    @callback
    def _migrate(registry_entry: er.RegistryEntry) -> dict[str, Any] | None:
        if (
            registry_entry.platform != DOMAIN
            or registry_entry.domain != Platform.SENSOR
        ):
            return None
        for legacy_key, new_key in _LEGACY_KEY_MAP:
            if not registry_entry.unique_id.endswith(f"_{legacy_key}"):
                continue
            new_unique_id = f"{address}-{new_key}"
            if registry_entry.unique_id == new_unique_id:
                return None
            existing = entity_registry.async_get_entity_id(
                Platform.SENSOR, DOMAIN, new_unique_id
            )
            if existing is not None and existing != registry_entry.entity_id:
                _LOGGER.warning(
                    (
                        "Not migrating %s: unique ID %s is already used by %s;"
                        " remove the stale duplicate entity manually"
                    ),
                    registry_entry.entity_id,
                    new_unique_id,
                    existing,
                )
                return None
            _LOGGER.debug(
                "Migrating %s unique ID %s -> %s",
                registry_entry.entity_id,
                registry_entry.unique_id,
                new_unique_id,
            )
            return {"new_unique_id": new_unique_id}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)
