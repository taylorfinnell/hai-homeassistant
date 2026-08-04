"""Tests for the Hai writable LED colours."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth.passive_update_processor import (
    deserialize_entity_description,
    serialize_entity_description,
)
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
    TextEntityDescription,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hai.protocol import HaiWriteVerificationError
from custom_components.hai.text import COLOR_KEYS, TEXT_DESCRIPTIONS

from .bluetooth import generate_ble_device, inject_hai_advertisement
from .conftest import (
    ADDRESS,
    DEVICE_NAME,
    DOMAIN,
    entity_id_for,
    make_settings,
    make_snapshot,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def wake_and_poll(
    hass: HomeAssistant, advertisement_time: float = 1000.0
) -> None:
    """Inject one wake advertisement and let the immediate poll settle."""
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=advertisement_time)
    await hass.async_block_till_done(wait_background_tasks=True)


def color_id(hass: HomeAssistant, key: str) -> str:
    """Look up a colour entity ID."""
    return entity_id_for(hass, "text", key)


def connectable_device():
    """Make the device reachable for a write.

    Every poll ends by clearing the advertisement history so the next identical
    wake packet is delivered, which also makes the address unresolvable
    afterwards. A real write happens while the shower is still running, so the
    lookup is stubbed rather than the behaviour changed.
    """
    return patch(
        "custom_components.hai.coordinator.bluetooth.async_ble_device_from_address",
        return_value=generate_ble_device(
            address=ADDRESS, name=DEVICE_NAME, details={}
        ),
    )


async def test_colors_created_from_settings(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A settings read publishes each writable colour as a text entity."""
    await setup_entry(hass)
    await wake_and_poll(hass)

    assert hass.states.get(color_id(hass, "temperature_level_color")).state == (
        "#00FF00"
    )
    assert hass.states.get(color_id(hass, "fourth_level_color")).state == "#FF2000"
    assert hass.states.get(color_id(hass, "first_level_color")).state == "#000000"

    state = hass.states.get(color_id(hass, "fourth_level_color"))
    assert state.attributes["pattern"] == r"^#[0-9A-Fa-f]{6}$"
    assert state.attributes["min"] == 7
    assert state.attributes["max"] == 7

    registry_entry = er.async_get(hass).async_get(
        color_id(hass, "fourth_level_color")
    )
    assert registry_entry.entity_category == "config"
    assert registry_entry.disabled_by is None


async def test_no_color_entities_before_settings_read(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A poll that did not read settings creates nothing."""
    mock_poll.return_value = make_snapshot(settings=None)
    await setup_entry(hass)
    await wake_and_poll(hass)

    assert not er.async_get(hass).async_get_entity_id(
        "text", DOMAIN, f"{ADDRESS}-first_level_color"
    )


async def test_read_only_color_creates_no_entity(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A colour the user cannot set is not offered as a text entity."""
    mock_poll.return_value = make_snapshot(
        settings=make_settings(
            writable_keys=frozenset({"temperature_level_color"})
        )
    )
    await setup_entry(hass)
    await wake_and_poll(hass)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "text", DOMAIN, f"{ADDRESS}-temperature_level_color"
    )
    assert not registry.async_get_entity_id(
        "text", DOMAIN, f"{ADDRESS}-first_level_color"
    )


async def test_unsupported_color_creates_no_entity(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Firmware that does not expose a colour gets no entity."""
    mock_poll.return_value = make_snapshot(
        settings=make_settings(led_colors={"temperature_level_color": "#00FF00"})
    )
    await setup_entry(hass)
    await wake_and_poll(hass)

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "text", DOMAIN, f"{ADDRESS}-temperature_level_color"
    )
    assert not registry.async_get_entity_id(
        "text", DOMAIN, f"{ADDRESS}-third_level_color"
    )


async def test_colors_retained_when_later_polls_omit_settings(
    hass: HomeAssistant, mock_poll: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """Omitting settings keeps the cached colour instead of blanking it.

    This is the guarantee the once-per-wake-generation refresh depends on: the
    processor merges per key, so a key absent from entity_data keeps its
    previous value. Publishing None instead would clear it on every poll after
    the first.
    """
    await setup_entry(hass)
    await wake_and_poll(hass)
    entity_id = color_id(hass, "temperature_level_color")
    assert hass.states.get(entity_id).state == "#00FF00"

    mock_poll.return_value = make_snapshot(settings=None)
    inject_hai_advertisement(hass, ADDRESS, advertisement_time=1020.0)
    await hass.async_block_till_done(wait_background_tasks=True)
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_poll.await_count == 2
    assert hass.states.get(entity_id).state == "#00FF00"


async def test_colors_stay_available_while_asleep(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Colours are configuration: they survive the device going away.

    Availability is also what makes the "device is asleep" error reachable at
    all, because Home Assistant drops unavailable entities before the platform
    is called.
    """
    entry = await setup_entry(hass)
    await wake_and_poll(hass)
    entity_id = color_id(hass, "temperature_level_color")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert mock_poll.await_count == 1
    assert hass.states.get(entity_id).state == "#00FF00"


async def test_set_value_writes_and_publishes(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A verified write updates state without waiting for the next poll."""
    await setup_entry(hass)
    await wake_and_poll(hass)
    entity_id = color_id(hass, "first_level_color")
    polls_before = mock_poll.await_count

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient.async_write_color",
            new_callable=AsyncMock,
            return_value="#3366FF",
        ) as write,
    ):
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: "#3366FF"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert write.await_args.args[2] == "#3366FF"
    assert hass.states.get(entity_id).state == "#3366FF"
    # No extra poll was provoked by the write.
    assert mock_poll.await_count == polls_before


async def test_set_value_survives_reload(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """The verified colour reaches processor storage, not just entity state."""
    entry = await setup_entry(hass)
    await wake_and_poll(hass)
    entity_id = color_id(hass, "first_level_color")

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient.async_write_color",
            new_callable=AsyncMock,
            return_value="#3366FF",
        ),
    ):
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: "#3366FF"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "#3366FF"


async def test_write_while_asleep_raises(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A sleeping device fails fast; the write is never queued."""
    await setup_entry(hass)
    await wake_and_poll(hass)
    entity_id = color_id(hass, "first_level_color")

    with (
        patch(
            "custom_components.hai.coordinator.bluetooth."
            "async_ble_device_from_address",
            return_value=None,
        ),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient.async_write_color",
            new_callable=AsyncMock,
        ) as write,
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: "#3366FF"},
            blocking=True,
        )

    assert err.value.translation_key == "device_asleep"
    write.assert_not_awaited()
    assert hass.states.get(entity_id).state == "#000000"


async def test_write_verification_failure_keeps_old_state(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """An unverified write never shows an optimistic colour."""
    await setup_entry(hass)
    await wake_and_poll(hass)
    entity_id = color_id(hass, "first_level_color")

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient.async_write_color",
            new_callable=AsyncMock,
            side_effect=HaiWriteVerificationError("mismatch"),
        ),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: "#3366FF"},
            blocking=True,
        )

    assert err.value.translation_key == "write_not_verified"
    assert hass.states.get(entity_id).state == "#000000"


@pytest.mark.parametrize("value", ["not a colour", "#GGGGGG", "#FFF"])
async def test_malformed_color_is_rejected(
    hass: HomeAssistant, mock_poll: AsyncMock, value: str
) -> None:
    """Bad input never reaches the radio.

    The declared length and pattern make Home Assistant's own text platform
    reject the call with a ValueError before the entity is touched, which is
    why the description carries native_min/native_max/pattern rather than
    relying only on the guard inside async_set_value.
    """
    await setup_entry(hass)
    await wake_and_poll(hass)
    entity_id = color_id(hass, "first_level_color")

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient.async_write_color",
            new_callable=AsyncMock,
        ) as write,
        pytest.raises((ValueError, ServiceValidationError)),
    ):
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
            blocking=True,
        )

    write.assert_not_awaited()
    assert hass.states.get(entity_id).state == "#000000"


def test_text_description_storage_round_trip() -> None:
    """Restored descriptions keep every constraint.

    Entities are built once from whatever description exists at creation and
    keep it for the entry's lifetime, so a field that fails to round-trip is
    permanently degraded after any restart.
    """
    assert set(TEXT_DESCRIPTIONS) == set(COLOR_KEYS)
    for description in TEXT_DESCRIPTIONS.values():
        restored = deserialize_entity_description(
            TextEntityDescription, serialize_entity_description(description)
        )
        assert restored.key == description.key
        assert restored.translation_key == description.translation_key
        assert restored.entity_category == description.entity_category
        assert restored.native_min == description.native_min
        assert restored.native_max == description.native_max
        assert restored.pattern == description.pattern
        # TextMode is a StrEnum, so this restores as a plain string and still
        # compares equal to the enum member.
        assert restored.mode == description.mode
