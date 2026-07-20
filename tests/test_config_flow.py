"""Tests for the Hai config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hai.const import DOMAIN

from .bluetooth import inject_hai_advertisement, make_service_info
from .conftest import ADDRESS, DEVICE_NAME

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def test_bluetooth_discovery_flow(hass: HomeAssistant) -> None:
    """A discovered device is confirmed and creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info(ADDRESS, DEVICE_NAME),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == DEVICE_NAME
    entry = result["result"]
    assert entry.unique_id == ADDRESS
    assert entry.version == 1
    assert entry.minor_version == 2
    await hass.async_block_till_done()


async def test_bluetooth_discovery_rejects_other_devices(
    hass: HomeAssistant,
) -> None:
    """A non-Hai local name aborts as not supported."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info("00:11:22:33:44:55", "SomeOtherDevice"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_bluetooth_discovery_aborts_when_configured(
    hass: HomeAssistant,
) -> None:
    """A second discovery of a configured address aborts."""
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info(ADDRESS, DEVICE_NAME),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_with_discovered_device(hass: HomeAssistant) -> None:
    """The user step lists discovered Hai devices and creates an entry."""
    inject_hai_advertisement(hass, ADDRESS)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == DEVICE_NAME
    assert result["result"].unique_id == ADDRESS
    await hass.async_block_till_done()


async def test_user_flow_without_devices_aborts(hass: HomeAssistant) -> None:
    """No advertisements seen: the user step aborts with guidance."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_ignores_non_hai_devices(hass: HomeAssistant) -> None:
    """Foreign advertisements never reach the pick list."""
    inject_hai_advertisement(hass, "00:11:22:33:44:55", name="NotAShower")
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
