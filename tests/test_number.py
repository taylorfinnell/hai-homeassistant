"""Tests for the Hai writable consumption thresholds."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth.passive_update_processor import (
    deserialize_entity_description,
    serialize_entity_description,
)
from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hai.coordinator import HaiCoordinator
from custom_components.hai.number import NUMBER_DESCRIPTIONS, THRESHOLD_KEYS
from custom_components.hai.protocol import HaiWriteVerificationError

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


def threshold_id(hass: HomeAssistant, key: str) -> str:
    """Look up a threshold entity ID."""
    return entity_id_for(hass, "number", key)


async def setup_with_thresholds(hass: HomeAssistant):
    """Set up and poll once so the settings read creates the thresholds."""
    entry = await setup_entry(hass)
    await wake_and_poll(hass, advertisement_time=1000.0)
    return entry


async def test_thresholds_created_after_settings_poll(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A settings read creates one config number per writable threshold."""
    await setup_with_thresholds(hass)

    state = hass.states.get(threshold_id(hass, "first_level_threshold"))
    assert state is not None
    # 20000 raw units -> 20000 mL -> 20.0 L
    assert float(state.state) == pytest.approx(20.0)
    assert state.attributes["unit_of_measurement"] == "L"
    assert state.attributes["device_class"] == "water"
    assert state.attributes[ATTR_MIN] == 0
    assert state.attributes[ATTR_MAX] == 200
    assert state.attributes["mode"] == NumberMode.BOX

    entry = er.async_get(hass).async_get(
        threshold_id(hass, "third_level_threshold")
    )
    assert entry.entity_category == "config"


async def test_thresholds_are_enabled_by_default(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Thresholds are usable out of the box.

    Merely having the entity writes nothing; a value only reaches the device
    when the user sets one, and the guards on that path do the protecting.
    """
    await setup_entry(hass)
    await wake_and_poll(hass)

    for key in THRESHOLD_KEYS:
        registry_entry = er.async_get(hass).async_get(threshold_id(hass, key))
        assert registry_entry.disabled_by is None, key
        assert hass.states.get(threshold_id(hass, key)) is not None, key


async def test_threshold_entity_data_is_raw_device_units(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Stored values are raw units, so a scale fix cannot corrupt them."""
    entry = await setup_with_thresholds(hass)
    coordinator: HaiCoordinator = entry.runtime_data

    processor = next(
        processor
        for processor in coordinator._processors
        if processor.restore_key == "number"
    )
    key = next(
        entity_key
        for entity_key in processor.entity_data
        if entity_key.key == "third_level_threshold"
    )
    assert processor.entity_data[key] == 75708


async def test_no_number_entities_before_settings_read(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A poll that did not read settings creates nothing."""
    mock_poll.return_value = make_snapshot(settings=None)
    await setup_entry(hass)
    await wake_and_poll(hass)

    assert not er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{ADDRESS}-first_level_threshold"
    )


async def test_read_only_threshold_creates_no_entity(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A number the user cannot set is worse than no number at all."""
    mock_poll.return_value = make_snapshot(
        settings=make_settings(
            writable_keys=frozenset(
                {"second_level_threshold", "third_level_threshold"}
            )
        )
    )
    await setup_entry(hass)
    await wake_and_poll(hass)

    registry = er.async_get(hass)
    assert not registry.async_get_entity_id(
        "number", DOMAIN, f"{ADDRESS}-first_level_threshold"
    )
    assert registry.async_get_entity_id(
        "number", DOMAIN, f"{ADDRESS}-second_level_threshold"
    )


async def test_unsupported_threshold_creates_no_entity(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Firmware that does not expose a threshold gets no entity."""
    mock_poll.return_value = make_snapshot(
        settings=make_settings(
            thresholds_raw={"third_level_threshold": 75708},
        )
    )
    await setup_entry(hass)
    await wake_and_poll(hass)

    registry = er.async_get(hass)
    assert not registry.async_get_entity_id(
        "number", DOMAIN, f"{ADDRESS}-first_level_threshold"
    )
    assert registry.async_get_entity_id(
        "number", DOMAIN, f"{ADDRESS}-third_level_threshold"
    )


async def test_implausible_threshold_is_unavailable(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A value that cannot be a shower blocks the entity, and so blocks writes.

    67,249,597 is what the observed third-threshold bytes decode to if the
    configuration block turns out to be plaintext after all.
    """
    mock_poll.return_value = make_snapshot(
        settings=make_settings(
            thresholds_raw={
                "first_level_threshold": 20000,
                "second_level_threshold": 40000,
                "third_level_threshold": 67249597,
            }
        )
    )
    await setup_with_thresholds(hass)

    assert hass.states.get(threshold_id(hass, "third_level_threshold")).state == (
        "unavailable"
    )
    assert hass.states.get(threshold_id(hass, "first_level_threshold")).state != (
        "unavailable"
    )


async def test_negative_threshold_is_unavailable(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """The characteristic is signed, so -1 is a sentinel or a bad decoding."""
    mock_poll.return_value = make_snapshot(
        settings=make_settings(
            thresholds_raw={
                "first_level_threshold": -1,
                "second_level_threshold": 40000,
                "third_level_threshold": 75708,
            }
        )
    )
    await setup_with_thresholds(hass)

    assert hass.states.get(threshold_id(hass, "first_level_threshold")).state == (
        "unavailable"
    )


async def test_set_value_writes_and_publishes(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A verified write updates state without waiting for the next poll."""
    await setup_with_thresholds(hass)
    entity_id = threshold_id(hass, "first_level_threshold")
    polls_before = mock_poll.await_count

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient."
            "async_write_threshold",
            new_callable=AsyncMock,
        ) as write,
    ):
        write.return_value = 45000
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 45},
            blocking=True,
        )
        await hass.async_block_till_done()

    # Called with raw device units, not litres.
    assert write.await_args.args[2] == 45000
    assert float(hass.states.get(entity_id).state) == pytest.approx(45.0)
    # No extra poll was provoked by the write.
    assert mock_poll.await_count == polls_before


async def test_set_value_survives_reload(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """The verified value reaches processor storage, not just entity state."""
    entry = await setup_with_thresholds(hass)
    entity_id = threshold_id(hass, "first_level_threshold")
    polls_before = mock_poll.await_count

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient."
            "async_write_threshold",
            new_callable=AsyncMock,
        ) as write,
    ):
        write.return_value = 45000
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 45},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Restored with no new advertisement and therefore no new poll.
    assert mock_poll.await_count == polls_before
    assert float(hass.states.get(entity_id).state) == pytest.approx(45.0)


async def test_write_while_asleep_raises(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """A sleeping device fails fast; the write is never queued."""
    await setup_with_thresholds(hass)
    entity_id = threshold_id(hass, "first_level_threshold")

    with (
        patch(
            "custom_components.hai.coordinator.bluetooth."
            "async_ble_device_from_address",
            return_value=None,
        ),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient."
            "async_write_threshold",
            new_callable=AsyncMock,
        ) as write,
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 45},
            blocking=True,
        )

    assert err.value.translation_key == "device_asleep"
    write.assert_not_awaited()
    assert float(hass.states.get(entity_id).state) == pytest.approx(20.0)


async def test_write_verification_failure_keeps_old_state(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """An unverified write never shows an optimistic value."""
    await setup_with_thresholds(hass)
    entity_id = threshold_id(hass, "first_level_threshold")

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient."
            "async_write_threshold",
            new_callable=AsyncMock,
            side_effect=HaiWriteVerificationError("mismatch"),
        ),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 45},
            blocking=True,
        )

    assert err.value.translation_key == "write_not_verified"
    assert float(hass.states.get(entity_id).state) == pytest.approx(20.0)


async def test_out_of_range_value_rejected(
    hass: HomeAssistant, mock_poll: AsyncMock
) -> None:
    """Values past the entity range are user errors, not device errors."""
    await setup_with_thresholds(hass)
    entity_id = threshold_id(hass, "first_level_threshold")

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient."
            "async_write_threshold",
            new_callable=AsyncMock,
        ) as write,
        pytest.raises(ServiceValidationError),
    ):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 5000},
            blocking=True,
        )

    write.assert_not_awaited()


async def test_settings_read_once_per_wake_generation(
    hass: HomeAssistant, mock_poll: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """Configuration is read once per wake, not on every 10-second poll."""
    entry = await setup_entry(hass)
    coordinator: HaiCoordinator = entry.runtime_data

    await wake_and_poll(hass, advertisement_time=1000.0)
    assert mock_poll.await_args_list[0].kwargs["read_settings"] is True

    # Same shower, next debounced poll: the nine configuration reads that
    # would otherwise repeat every ten seconds are skipped.
    await next_debounced_poll(hass, freezer, advertisement_time=1020.0)
    assert coordinator.tracker.generation == 1
    assert mock_poll.await_count == 2
    assert mock_poll.await_args_list[1].kwargs["read_settings"] is False

    # A new wake generation asks again.
    await next_debounced_poll(hass, freezer, advertisement_time=2000.0)
    assert coordinator.tracker.generation == 2
    assert mock_poll.await_count == 3
    assert mock_poll.await_args_list[2].kwargs["read_settings"] is True


async def test_settings_reread_after_write(
    hass: HomeAssistant, mock_poll: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A write invalidates the cache so device-side clamping is picked up."""
    entry = await setup_with_thresholds(hass)
    coordinator: HaiCoordinator = entry.runtime_data
    assert coordinator.settings_generation is not None

    with (
        connectable_device(),
        patch(
            "custom_components.hai.coordinator.HaiProtocolClient."
            "async_write_threshold",
            new_callable=AsyncMock,
            return_value=45000,
        ),
    ):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {
                ATTR_ENTITY_ID: threshold_id(hass, "first_level_threshold"),
                ATTR_VALUE: 45,
            },
            blocking=True,
        )
        await hass.async_block_till_done()

    assert coordinator.settings_generation is None

    await next_debounced_poll(hass, freezer, advertisement_time=1020.0)
    assert mock_poll.await_args_list[-1].kwargs["read_settings"] is True


def test_number_description_storage_round_trip() -> None:
    """Restored descriptions keep their type and every constraint.

    Entities are built once from whatever description exists at creation and
    keep it for the entry's lifetime, so a field that fails to round-trip is
    permanently degraded after any restart.
    """
    for description in NUMBER_DESCRIPTIONS.values():
        restored = deserialize_entity_description(
            NumberEntityDescription, serialize_entity_description(description)
        )
        # Restore swaps in the frozen-dataclass compat shadow rather than the
        # real class. That is why these descriptions must stay stock: a custom
        # subclass would come back as this shadow with its extra fields gone.
        assert type(restored) is not NumberEntityDescription
        assert type(restored).__name__ == "NumberEntityDescription"
        assert restored.key == description.key
        assert restored.translation_key == description.translation_key
        assert restored.device_class == description.device_class
        assert (
            restored.native_unit_of_measurement
            == description.native_unit_of_measurement
        )
        assert restored.native_min_value == description.native_min_value
        assert restored.native_max_value == description.native_max_value
        assert restored.native_step == description.native_step
        assert restored.entity_category == description.entity_category
        assert (
            restored.entity_registry_enabled_default
            == description.entity_registry_enabled_default
        )
        # NumberMode is a StrEnum, so this restores as a plain string and
        # still compares (and hashes) equal to the enum member.
        assert restored.mode == description.mode
