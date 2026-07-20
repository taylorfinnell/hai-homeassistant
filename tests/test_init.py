"""Tests for Hai setup, unload, and config entry migration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hai.const import DOMAIN

from .conftest import ADDRESS, DEVICE_NAME

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

LEGACY_PREFIX = f"{DEVICE_NAME} 0A1B2C"

LEGACY_TO_NEW = {
    "current_volume": "current_volume",
    "total_volume": "lifetime_volume",
    "current_temperature": "current_temperature",
    "average_temperature": "average_temperature",
    "current_duration": "current_duration",
    "last_shower_duration": "last_shower_duration",
    "last_shower_temperature": "last_shower_temperature",
    "last_shower_volume": "last_shower_volume",
}


def make_entry(**kwargs: object) -> MockConfigEntry:
    """Build the standard Hai config entry."""
    defaults: dict = {
        "domain": DOMAIN,
        "unique_id": ADDRESS,
        "title": DEVICE_NAME,
        "version": 1,
        "minor_version": 2,
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


async def test_migration_moves_legacy_unique_ids(hass: HomeAssistant) -> None:
    """v1 name-based unique IDs migrate to the address-based format."""
    entry = make_entry(minor_version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)

    old_entity_ids: dict[str, str] = {}
    for legacy_key in LEGACY_TO_NEW:
        registry_entry = entity_registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{LEGACY_PREFIX}_{legacy_key}",
            config_entry=entry,
        )
        old_entity_ids[legacy_key] = registry_entry.entity_id

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 1
    assert entry.minor_version == 2
    for legacy_key, new_key in LEGACY_TO_NEW.items():
        migrated = entity_registry.async_get(old_entity_ids[legacy_key])
        assert migrated is not None, legacy_key
        assert migrated.unique_id == f"{ADDRESS}-{new_key}"


async def test_migration_is_idempotent(hass: HomeAssistant) -> None:
    """Already-migrated unique IDs are untouched by a rerun."""
    entry = make_entry(minor_version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    registry_entry = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{ADDRESS}-lifetime_volume",
        config_entry=entry,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    migrated = entity_registry.async_get(registry_entry.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{ADDRESS}-lifetime_volume"
    assert entry.minor_version == 2


async def test_migration_collision_keeps_both_entities(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """When old and new unique IDs coexist, neither is destroyed."""
    entry = make_entry(minor_version=1)
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    legacy = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{LEGACY_PREFIX}_total_volume",
        config_entry=entry,
    )
    modern = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{ADDRESS}-lifetime_volume",
        config_entry=entry,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    legacy_after = entity_registry.async_get(legacy.entity_id)
    modern_after = entity_registry.async_get(modern.entity_id)
    assert legacy_after is not None
    assert legacy_after.unique_id == f"{LEGACY_PREFIX}_total_volume"
    assert modern_after is not None
    assert modern_after.unique_id == f"{ADDRESS}-lifetime_volume"
    assert "Not migrating" in caplog.text
    assert entry.minor_version == 2


async def test_migration_refuses_future_major_version(hass: HomeAssistant) -> None:
    """A config entry from a newer major schema fails migration."""
    entry = make_entry(version=2)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
