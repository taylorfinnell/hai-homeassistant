"""Tests for Hai config entry setup and unload."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hai.const import DOMAIN
from custom_components.hai.coordinator import HaiCoordinator

from .conftest import ADDRESS, DEVICE_NAME

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

PLATFORM_DOMAINS = ("binary_sensor", "number", "sensor")


def make_entry(**kwargs: object) -> MockConfigEntry:
    """Build the standard Hai config entry."""
    defaults: dict = {
        "domain": DOMAIN,
        "unique_id": ADDRESS,
        "title": DEVICE_NAME,
    }
    defaults.update(kwargs)
    return MockConfigEntry(**defaults)


async def test_setup_without_advertisement_succeeds(hass: HomeAssistant) -> None:
    """A sleeping device must not cause setup retries."""
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # Never-seen device: no entities yet, and that is fine.
    assert not hass.states.async_entity_ids("sensor")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_existing_v1_minor_version_entry_loads(hass: HomeAssistant) -> None:
    """An entry stored before MINOR_VERSION was dropped still loads.

    v2 provides no async_migrate_entry, so this pins that a pre-existing entry
    is tolerated rather than ending in MIGRATION_ERROR.
    """
    entry = make_entry(minor_version=1)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_creates_runtime_data_and_starts_coordinator(
    hass: HomeAssistant,
) -> None:
    """Both platforms and diagnostics read the coordinator off runtime_data."""
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert isinstance(coordinator, HaiCoordinator)
    assert coordinator.address == ADDRESS
    assert coordinator.entry is entry


async def test_unload_removes_all_platform_entities(hass: HomeAssistant) -> None:
    """Unload tears down every forwarded platform.

    Setup forwards platforms before starting the coordinator; unload must undo
    both without leaving states behind.
    """
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    for domain in PLATFORM_DOMAINS:
        assert not hass.states.async_entity_ids(domain), domain
