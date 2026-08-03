"""The Hai integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import HaiConfigEntry, HaiCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SENSOR]

# There is deliberately no async_migrate_entry: v2 is a rewrite with new unique
# IDs and no upgrade path from v1, so a v1 entry is removed and re-added by hand.
# A MINOR_VERSION bump never needs a migrate handler, but raising VERSION does --
# add one in the same commit if that ever happens.


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
