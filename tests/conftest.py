"""Fixtures for the Hai integration tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.async_ import get_scheduled_timer_handles
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hai.const import DOMAIN
from custom_components.hai.protocol import HaiLastShower, HaiSettings, HaiSnapshot

ADDRESS = "AA:BB:CC:DD:EE:FF"
DEVICE_NAME = "haiS0123456"

ALL_OPTIONAL_KEYS = frozenset(
    {
        "flow_rate",
        "current_start_time",
        "lifetime_average_temperature",
        "battery_voltage",
    }
)

THRESHOLD_KEYS = (
    "first_level_threshold",
    "second_level_threshold",
    "third_level_threshold",
)

COLOR_KEYS = (
    "first_level_color",
    "second_level_color",
    "third_level_color",
    "fourth_level_color",
    "temperature_level_color",
)

ALL_SETTINGS_KEYS = frozenset({*THRESHOLD_KEYS, *COLOR_KEYS, "level_configuration"})


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading custom integrations in every test."""
    yield


@pytest.fixture(autouse=True)
def cancel_leaked_bluetooth_expire_timers(
    request: pytest.FixtureRequest,
) -> Generator[None]:
    """Cancel the device-expiry timer the bluetooth entry leaks on unload.

    HaScanner.async_setup() schedules a repeating 30 s device-expiry timer
    and returns its canceller, which homeassistant.components.bluetooth
    discards; unloading the bluetooth config entry never cancels it. Without
    this, the shared lingering-timer teardown check fails every test that
    used the enable_bluetooth fixture.
    """
    yield
    if "hass" not in request.fixturenames:
        return
    hass = request.getfixturevalue("hass")
    for handle in get_scheduled_timer_handles(hass.loop):
        if not handle.cancelled() and "_async_expire_devices" in repr(handle):
            handle.cancel()


def make_last_shower(**overrides: object) -> HaiLastShower:
    """Build a decoded last-shower record with defaults."""
    values: dict = {
        "session_id": 29,
        "temperature_c": 30.15,
        "initial_temperature_c": 25.0,
        "duration_s": 600,
        "volume_ml": 90000,
        "start_time": datetime(2024, 2, 9, 8, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return HaiLastShower(**values)


def make_settings(**overrides: object) -> HaiSettings:
    """Build a settings read with defaults; override any field.

    Threshold values are raw device units, matching what the protocol layer
    stores. The colour defaults are the observed hardware samples XOR-decoded.
    """
    values: dict = {
        "thresholds_raw": {
            "first_level_threshold": 20000,
            "second_level_threshold": 40000,
            "third_level_threshold": 75708,
        },
        "led_colors": {
            "first_level_color": "#000000",
            "second_level_color": "#000000",
            "third_level_color": "#000000",
            "fourth_level_color": "#FF2000",
            "temperature_level_color": "#00FF00",
        },
        "raw_hex": {"third_level_threshold": "bd250204"},
        "supported_keys": ALL_SETTINGS_KEYS,
        "writable_keys": frozenset(THRESHOLD_KEYS),
    }
    values.update(overrides)
    return HaiSettings(**values)


def make_snapshot(**overrides: object) -> HaiSnapshot:
    """Build a full snapshot with defaults; override any field."""
    values: dict = {
        "address": ADDRESS,
        "name": DEVICE_NAME,
        "product_id": "0A1B2C",
        "app_version": "1.10",
        "bootloader_version": "3",
        "session_id": 30,
        "current_temperature_c": 31.73,
        "current_average_temperature_c": 31.5,
        "current_volume_ml": 12345,
        "current_duration_s": 120,
        "current_flow_rate_lpm": 6.0,
        "current_start_time": datetime(2024, 2, 10, 1, 21, 40, tzinfo=UTC),
        "lifetime_volume_ml": 106348,
        "lifetime_average_temperature_c": 30.9,
        "battery_voltage_v": 3.537,
        "last_shower": make_last_shower(),
        "supported_optional_keys": ALL_OPTIONAL_KEYS,
        "settings": make_settings(),
    }
    values.update(overrides)
    return HaiSnapshot(**values)


def make_idle_snapshot(**overrides: object) -> HaiSnapshot:
    """Build a snapshot for a device with no active session."""
    values: dict = {
        "session_id": 0,
        "current_temperature_c": None,
        "current_average_temperature_c": None,
        "current_volume_ml": None,
        "current_duration_s": None,
        "current_flow_rate_lpm": None,
        "current_start_time": None,
    }
    values.update(overrides)
    return make_snapshot(**values)


async def setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the standard Hai config entry."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=DEVICE_NAME)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id_for(hass: HomeAssistant, platform: str, key: str) -> str:
    """Look up the entity ID for a processor key on a platform."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{ADDRESS}-{key}"
    )
    assert entity_id is not None, f"no {platform} entity for key {key}"
    return entity_id


@pytest.fixture
def mock_poll() -> Generator[AsyncMock]:
    """Patch the protocol client's poll with a controllable AsyncMock."""
    with patch(
        "custom_components.hai.coordinator.HaiProtocolClient.async_poll",
        new_callable=AsyncMock,
    ) as poll:
        poll.return_value = make_snapshot()
        yield poll
