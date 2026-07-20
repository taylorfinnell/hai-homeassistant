"""Tests for Hai sensor behavior: freshness, retention, and metadata."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import AsyncMock, patch

from bleak.exc import BleakError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth.passive_update_processor import (
    deserialize_entity_description,
    serialize_entity_description,
)
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.hai.const import DOMAIN
from custom_components.hai.coordinator import HaiCoordinator
from custom_components.hai.sensor import SENSOR_DESCRIPTIONS

from .bluetooth import inject_hai_advertisement, make_service_info
from .conftest import ADDRESS, DEVICE_NAME, make_snapshot

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

LIVE_KEYS = ("current_temperature", "average_temperature", "current_volume",
             "current_duration")
RETAINED_KEYS = (
    "lifetime_volume",
    "last_shower_temperature",
    "last_shower_duration",
    "last_shower_volume",
)


async def setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the standard Hai config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title=DEVICE_NAME,
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id_for(hass: HomeAssistant, key: str) -> str:
    """Look up the entity ID for a processor key."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{ADDRESS}-{key}"
    )
    assert entity_id is not None, f"no entity for key {key}"
    return entity_id


async def wake_and_poll(
    hass: HomeAssistant, advertisement_time: float
) -> None:
    """Inject one wake advertisement and let the immediate poll settle."""
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=advertisement_time)
    await hass.async_block_till_done(wait_background_tasks=True)


async def next_debounced_poll(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    advertisement_time: float,
) -> None:
    """Inject a follow-up advertisement and run the debounced poll."""
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=advertisement_time)
    await hass.async_block_till_done(wait_background_tasks=True)
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_successful_poll_populates_entities(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A wake advertisement plus poll produces the full entity set."""
    await setup_entry(hass)
    assert not hass.states.async_entity_ids("sensor")

    await wake_and_poll(hass, advertisement_time=1000.0)
    assert mock_poll.await_count == 1

    current_temperature = hass.states.get(
        entity_id_for(hass, "current_temperature")
    )
    assert current_temperature.state == "31.73"
    assert current_temperature.attributes[ATTR_DEVICE_CLASS] == "temperature"
    assert current_temperature.attributes[ATTR_UNIT_OF_MEASUREMENT] == "°C"
    assert current_temperature.attributes["state_class"] == "measurement"

    average_temperature = hass.states.get(
        entity_id_for(hass, "average_temperature")
    )
    assert average_temperature.state == "31.5"
    assert "state_class" not in average_temperature.attributes

    current_volume = hass.states.get(entity_id_for(hass, "current_volume"))
    assert current_volume.state == "12345"
    assert current_volume.attributes[ATTR_UNIT_OF_MEASUREMENT] == "mL"
    assert current_volume.attributes["state_class"] == "total_increasing"

    current_duration = hass.states.get(entity_id_for(hass, "current_duration"))
    assert current_duration.state == "120"
    assert current_duration.attributes[ATTR_UNIT_OF_MEASUREMENT] == "s"

    flow_rate = hass.states.get(entity_id_for(hass, "flow_rate"))
    assert flow_rate.state == "6.0"
    assert flow_rate.attributes[ATTR_UNIT_OF_MEASUREMENT] == "L/min"

    lifetime_volume = hass.states.get(entity_id_for(hass, "lifetime_volume"))
    assert lifetime_volume.state == "106.348"
    assert lifetime_volume.attributes[ATTR_DEVICE_CLASS] == "water"
    assert lifetime_volume.attributes[ATTR_UNIT_OF_MEASUREMENT] == "L"
    assert "state_class" not in lifetime_volume.attributes

    last_temperature = hass.states.get(
        entity_id_for(hass, "last_shower_temperature")
    )
    assert last_temperature.state == "30.15"
    assert "state_class" not in last_temperature.attributes

    last_volume = hass.states.get(entity_id_for(hass, "last_shower_volume"))
    assert last_volume.state == "90000"
    assert ATTR_DEVICE_CLASS not in last_volume.attributes

    lifetime_average = hass.states.get(
        entity_id_for(hass, "lifetime_average_temperature")
    )
    assert lifetime_average.state == "30.9"

    # Battery is a disabled-by-default diagnostic: registered, no state.
    entity_registry = er.async_get(hass)
    battery_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{ADDRESS}-battery_voltage"
    )
    assert battery_id is not None
    battery_entry = entity_registry.async_get(battery_id)
    assert battery_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert battery_entry.entity_category == "diagnostic"
    assert hass.states.get(battery_id) is None


async def test_failed_first_poll_keeps_live_unavailable(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Entities exist from the advertisement but stay unavailable."""
    mock_poll.side_effect = BleakError("connection refused")
    entry = await setup_entry(hass)

    await wake_and_poll(hass, advertisement_time=1000.0)

    coordinator: HaiCoordinator = entry.runtime_data
    assert coordinator.last_poll_successful is False
    assert coordinator.tracker.live_data_fresh is False
    for key in LIVE_KEYS + RETAINED_KEYS:
        state = hass.states.get(entity_id_for(hass, key))
        assert state.state == STATE_UNAVAILABLE, key


async def test_repeated_advertisements_keep_live_values(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Same-shower advertisements never clear or drop live values."""
    entry = await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)
    coordinator: HaiCoordinator = entry.runtime_data
    assert coordinator.tracker.generation == 1

    # An identical advertisement re-delivered 20 s later (post cache clear).
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1020.0)
    await hass.async_block_till_done()

    assert coordinator.tracker.generation == 1
    assert coordinator.tracker.live_data_fresh is True
    assert hass.states.get(
        entity_id_for(hass, "current_temperature")
    ).state == "31.73"


async def test_new_wake_generation_requires_fresh_poll(
    hass: HomeAssistant, mock_poll: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A new shower drops live values until its own poll succeeds."""
    entry = await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)
    coordinator: HaiCoordinator = entry.runtime_data

    # Next shower: advertisement far beyond the wake gap, poll failing.
    mock_poll.side_effect = BleakError("device busy")
    await next_debounced_poll(hass, freezer, advertisement_time=2000.0)

    assert coordinator.tracker.generation == 2
    assert coordinator.tracker.live_data_fresh is False
    for key in LIVE_KEYS:
        assert (
            hass.states.get(entity_id_for(hass, key)).state == STATE_UNAVAILABLE
        ), key
    # Retained values from the previous shower stay available.
    assert hass.states.get(
        entity_id_for(hass, "lifetime_volume")
    ).state == "106.348"
    assert hass.states.get(
        entity_id_for(hass, "last_shower_temperature")
    ).state == "30.15"

    # Recovery: the next poll of the same generation succeeds.
    mock_poll.side_effect = None
    mock_poll.return_value = make_snapshot(current_temperature_c=35.0)
    await next_debounced_poll(hass, freezer, advertisement_time=2030.0)

    assert coordinator.tracker.generation == 2
    assert coordinator.tracker.live_data_fresh is True
    assert hass.states.get(
        entity_id_for(hass, "current_temperature")
    ).state == "35.0"


async def test_poll_failure_after_success_notifies_live_entities(
    hass: HomeAssistant, mock_poll: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A failed poll drops live entities even with no processor dispatch."""
    entry = await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)
    assert hass.states.get(
        entity_id_for(hass, "current_temperature")
    ).state == "31.73"

    mock_poll.side_effect = BleakError("gone mid-shower")
    await next_debounced_poll(hass, freezer, advertisement_time=1030.0)

    coordinator: HaiCoordinator = entry.runtime_data
    assert coordinator.tracker.generation == 1
    assert coordinator.tracker.live_data_fresh is False
    for key in LIVE_KEYS:
        assert (
            hass.states.get(entity_id_for(hass, key)).state == STATE_UNAVAILABLE
        ), key
    assert hass.states.get(
        entity_id_for(hass, "lifetime_volume")
    ).state == "106.348"


async def test_unavailable_resets_freshness_and_keeps_retained(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Advertisement timeout drops live values and keeps retained ones."""
    entry = await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)
    coordinator: HaiCoordinator = entry.runtime_data

    coordinator._async_handle_unavailable(
        make_service_info(ADDRESS, DEVICE_NAME, advertisement_time=1400.0)
    )
    await hass.async_block_till_done()

    assert coordinator.tracker.live_data_fresh is False
    for key in LIVE_KEYS:
        assert (
            hass.states.get(entity_id_for(hass, key)).state == STATE_UNAVAILABLE
        ), key
    for key in RETAINED_KEYS:
        assert (
            hass.states.get(entity_id_for(hass, key)).state != STATE_UNAVAILABLE
        ), key


async def test_session_zero_poll_publishes_explicit_none(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """An idle-device poll keeps live entities unavailable, not stale."""
    from .conftest import make_idle_snapshot

    mock_poll.return_value = make_idle_snapshot()
    await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)

    for key in LIVE_KEYS:
        assert (
            hass.states.get(entity_id_for(hass, key)).state == STATE_UNAVAILABLE
        ), key
    # Retained data still delivered by the same poll.
    assert hass.states.get(
        entity_id_for(hass, "lifetime_volume")
    ).state == "106.348"


async def test_unsupported_optional_keys_create_no_entities(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Optional entities appear only after capability confirmation."""
    mock_poll.return_value = make_snapshot(
        supported_optional_keys=frozenset(),
        current_flow_rate_lpm=None,
        current_start_time=None,
        lifetime_average_temperature_c=None,
        battery_voltage_v=None,
    )
    await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)

    entity_registry = er.async_get(hass)
    for key in ("flow_rate", "lifetime_average_temperature", "battery_voltage"):
        assert (
            entity_registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}-{key}")
            is None
        ), key


async def test_reload_restores_retained_and_gates_live(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Processor storage restores entities while the device sleeps."""
    entry = await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)
    assert mock_poll.await_count == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # No advertisement after the reload: no new poll happened.
    assert mock_poll.await_count == 1
    for key in RETAINED_KEYS:
        state = hass.states.get(entity_id_for(hass, key))
        assert state.state != STATE_UNAVAILABLE, key
    assert hass.states.get(
        entity_id_for(hass, "lifetime_volume")
    ).state == "106.348"
    # Live values from the pre-reload shower must not resurrect as live.
    for key in LIVE_KEYS:
        assert (
            hass.states.get(entity_id_for(hass, key)).state == STATE_UNAVAILABLE
        ), key


async def test_device_registry_gets_polled_metadata(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Polled metadata lands on the device entry created by entities."""
    await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)

    device = dr.async_get(hass).async_get_device(
        connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)}
    )
    assert device is not None
    assert device.name == DEVICE_NAME
    assert device.manufacturer == "hai"
    assert device.model == "Smart Showerhead"
    assert device.sw_version == "1.10"
    assert device.model_id == "0A1B2C"
    assert device.hw_version is None


async def test_clear_advertisement_history_after_every_poll(
    hass: HomeAssistant, mock_poll: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The advertisement cache is cleared after success and failure.

    With the real clearing mocked away, identical follow-up advertisements
    would be swallowed by the manager's change-detection guard, so each
    injected advertisement here varies its manufacturer data.
    """
    with patch(
        "custom_components.hai.coordinator.bluetooth.async_clear_advertisement_history"
    ) as clear_history:
        await setup_entry(hass)
        inject_hai_advertisement(
            hass,
            ADDRESS,
            advertisement_time=1000.0,
            manufacturer_data={0x1234: b"\x01"},
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert clear_history.call_count == 1

        mock_poll.side_effect = BleakError("boom")
        inject_hai_advertisement(
            hass,
            ADDRESS,
            advertisement_time=1030.0,
            manufacturer_data={0x1234: b"\x02"},
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert clear_history.call_count == 2
        clear_history.assert_called_with(hass, ADDRESS)


def test_description_storage_round_trip() -> None:
    """Every description survives the JSON restore cycle unchanged."""
    for key, description in SENSOR_DESCRIPTIONS.items():
        stored = json.loads(json.dumps(serialize_entity_description(description)))
        restored = deserialize_entity_description(SensorEntityDescription, stored)
        assert restored.key == key
        assert restored.device_class == description.device_class
        assert (
            restored.native_unit_of_measurement
            == description.native_unit_of_measurement
        )
        assert restored.state_class == description.state_class
        assert restored.entity_category == description.entity_category
        assert (
            restored.entity_registry_enabled_default
            == description.entity_registry_enabled_default
        )
