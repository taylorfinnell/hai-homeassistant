"""Tests for the Hai shower activity binary sensor."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from bleak.exc import BleakError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hai.coordinator import HaiCoordinator

from .bluetooth import inject_hai_advertisement, make_service_info
from .conftest import (
    ADDRESS,
    DEVICE_NAME,
    entity_id_for,
    make_idle_snapshot,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


def shower_active_id(hass: HomeAssistant) -> str:
    """Look up the shower_active entity ID."""
    return entity_id_for(hass, "binary_sensor", "shower_active")


async def test_advertisement_turns_shower_on_even_if_poll_fails(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Activity tracks advertising presence, not poll success."""
    mock_poll.side_effect = BleakError("no GATT for you")
    await setup_entry(hass)

    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(shower_active_id(hass))
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "running"


async def test_advertisement_timeout_turns_shower_off_not_unavailable(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Losing advertisements means off, never unavailable."""
    entry = await setup_entry(hass)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(shower_active_id(hass)).state == STATE_ON

    coordinator: HaiCoordinator = entry.runtime_data
    coordinator._async_handle_unavailable(
        make_service_info(ADDRESS, DEVICE_NAME, advertisement_time=1400.0)
    )
    await hass.async_block_till_done()

    assert hass.states.get(shower_active_id(hass)).state == STATE_OFF

    # The next shower flips it straight back on.
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=2000.0)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(shower_active_id(hass)).state == STATE_ON


async def test_session_zero_poll_ends_activity_while_still_advertising(
    hass: HomeAssistant, mock_poll: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A poll reporting no session ends the shower without waiting.

    Advertising alone only means the head is awake. Waiting for the
    advertisement timeout costs minutes; a poll that reports session 0 is
    definitive and takes effect at once.
    """
    await setup_entry(hass)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(shower_active_id(hass)).state == STATE_ON

    mock_poll.return_value = make_idle_snapshot()
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1020.0)
    await hass.async_block_till_done(wait_background_tasks=True)
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    # Still advertising, still available -- but off, and not unavailable.
    entry_coordinator: HaiCoordinator = hass.config_entries.async_entries(
        "hai"
    )[0].runtime_data
    assert entry_coordinator.available is True
    assert hass.states.get(shower_active_id(hass)).state == STATE_OFF


async def test_restored_shower_active_starts_off(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """After a reload while asleep, activity restores as off."""
    entry = await setup_entry(hass)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(shower_active_id(hass))
    assert state is not None
    assert state.state == STATE_OFF
