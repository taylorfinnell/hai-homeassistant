"""Tests for Hai diagnostics."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
import pytest

from custom_components.hai.diagnostics import async_get_config_entry_diagnostics

from .bluetooth import inject_hai_advertisement
from .conftest import ADDRESS, make_snapshot, setup_entry

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def test_diagnostics_before_any_poll(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A sleeping, never-polled device still produces diagnostics."""
    entry = await setup_entry(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["snapshot"] is None
    assert data["settings"] is None
    assert data["coordinator"]["wake_generation"] == 0
    assert data["coordinator"]["live_data_fresh"] is False
    assert data["coordinator"]["settings_generation"] is None


async def test_diagnostics_redacts_the_address(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Diagnostics get pasted into issues, so the MAC does not travel."""
    entry = await setup_entry(hass)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["entry"]["address"] == REDACTED
    assert data["snapshot"]["address"] == REDACTED
    assert ADDRESS not in str(data)


async def test_diagnostics_serialize_datetimes_and_frozensets(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """asdict recurses into the nested record, so both need JSON-safe types."""
    entry = await setup_entry(hass)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)

    snapshot = (await async_get_config_entry_diagnostics(hass, entry))["snapshot"]

    assert snapshot["current_start_time"] == "2024-02-10T01:21:40+00:00"
    assert snapshot["last_shower"]["start_time"] == "2024-02-09T08:00:00+00:00"
    assert snapshot["supported_optional_keys"] == sorted(
        snapshot["supported_optional_keys"]
    )
    assert isinstance(snapshot["supported_optional_keys"], list)
    # The bootloader version belongs here and never in hw_version.
    assert snapshot["bootloader_version"] == "3"


async def test_diagnostics_show_both_settings_decodings(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """The settings block is the oracle for the encryption question.

    Read-back verification cannot settle whether the configuration block is
    XOR-encrypted, so diagnostics carry the raw bytes and both candidate
    readings for comparison against the hai app.
    """
    entry = await setup_entry(hass)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)

    settings = (await async_get_config_entry_diagnostics(hass, entry))["settings"]

    assert settings["thresholds_raw"]["third_level_threshold"] == 75708
    assert settings["led_colors"]["temperature_level_color"] == "#00FF00"
    assert settings["writable_keys"] == sorted(settings["writable_keys"])

    candidates = settings["characteristics"]["third_level_threshold"]
    assert candidates["raw"] == "bd250204"
    assert candidates["as_plaintext_int"] == 67249597
    assert candidates["as_xor_int"] == 75708


async def test_diagnostics_settings_none_until_read(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A poll that skipped settings leaves the block absent, not empty."""
    mock_poll.return_value = make_snapshot(settings=None)
    entry = await setup_entry(hass)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1000.0)
    await hass.async_block_till_done(wait_background_tasks=True)

    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["snapshot"] is not None
    assert data["settings"] is None
