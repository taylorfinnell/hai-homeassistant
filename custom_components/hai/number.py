"""Writable consumption thresholds for the Hai integration.

Everything about these is deliberately conservative: the characteristics are
capability-probed for writability, values are verified by read-back, a
threshold that decodes to an impossible volume makes its entity unavailable
and so cannot be written at all, and a value that has never been read is
refused because an unobserved encoding cannot be verified.
"""

from __future__ import annotations

import logging
from typing import cast

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import HaiConfigEntry, HaiCoordinator, HaiUpdate
from .protocol import (
    THRESHOLD_MAX_PLAUSIBLE_ML,
    decode_threshold,
    encode_threshold,
)

_LOGGER = logging.getLogger(__name__)

# Writes are user-initiated and hit the radio. Unlike the read-only platforms,
# this must not be 0: 0 removes the platform semaphore entirely, so an
# automation setting all three thresholds would fire three concurrent BLE
# connections.
PARALLEL_UPDATES = 1

THRESHOLD_KEYS: tuple[str, ...] = (
    "first_level_threshold",
    "second_level_threshold",
    "third_level_threshold",
)

_MAX_LITERS = THRESHOLD_MAX_PLAUSIBLE_ML / 1000

NUMBER_DESCRIPTIONS: dict[str, NumberEntityDescription] = {
    key: NumberEntityDescription(
        key=key,
        translation_key=key,
        device_class=NumberDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        native_min_value=0,
        native_max_value=_MAX_LITERS,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    )
    for key in THRESHOLD_KEYS
}


def _entity_key(key: str) -> PassiveBluetoothEntityKey:
    return PassiveBluetoothEntityKey(key=key, device_id=None)


def number_update_to_bluetooth_data_update(
    coordinator: HaiCoordinator, update: HaiUpdate
) -> PassiveBluetoothDataUpdate[int | None]:
    """Convert a HaiUpdate into a processor update for the number platform.

    Values are raw device units. Settings are omitted entirely (rather than
    published as None) whenever they were not read, so the processor's per-key
    merge retains the cached value.
    """
    devices = {None: coordinator.device_info()}
    descriptions: dict[PassiveBluetoothEntityKey, NumberEntityDescription] = {}
    data: dict[PassiveBluetoothEntityKey, int | None] = {}

    if (settings := update.settings) is not None:
        for key in THRESHOLD_KEYS:
            # Present but read-only firmware gets no entity: a number the user
            # cannot set is worse than no number at all.
            if key in settings.thresholds_raw and key in settings.writable_keys:
                descriptions[_entity_key(key)] = NUMBER_DESCRIPTIONS[key]
                data[_entity_key(key)] = settings.thresholds_raw[key]

    return PassiveBluetoothDataUpdate(
        devices=devices,
        entity_descriptions=descriptions,
        entity_data=data,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Hai threshold numbers."""
    coordinator = entry.runtime_data
    processor: PassiveBluetoothDataProcessor[int | None, HaiUpdate] = (
        PassiveBluetoothDataProcessor(
            lambda update: number_update_to_bluetooth_data_update(coordinator, update)
        )
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(HaiThresholdNumber, async_add_entities)
    )
    # Registration dispatches restored data immediately, so the entities
    # listener above must already be attached.
    entry.async_on_unload(
        coordinator.async_register_processor(processor, NumberEntityDescription)
    )


class HaiThresholdNumber(
    PassiveBluetoothProcessorEntity[
        PassiveBluetoothDataProcessor[int | None, HaiUpdate]
    ],
    NumberEntity,
):
    """One writable consumption threshold."""

    @property
    def _coordinator(self) -> HaiCoordinator:
        return cast(HaiCoordinator, self.processor.coordinator)

    @property
    def _raw_value(self) -> int | None:
        return self.processor.entity_data.get(self.entity_key)

    @property
    def native_value(self) -> float | None:
        """Return the threshold in litres."""
        if (raw := self._raw_value) is None:
            return None
        return decode_threshold(raw) / 1000

    @property
    def available(self) -> bool:
        """Available once a plausible value is known, awake or asleep.

        Deliberately not gated on Bluetooth presence. Home Assistant's entity
        service dispatch drops unavailable entities before the platform is
        called, so a presence-gated number would make number.set_value a silent
        no-op for the ~99% of the time the shower head is asleep, instead of
        raising the "device is asleep" error.

        The plausibility check is a decoding smoke test, not proof that the
        encoding is right: it only says the bytes produce a shower-sized
        number. Blocking here also blocks writes, which is the point. A
        negative value fails it too: the characteristic is signed, so a
        negative reading is either a wrong decoding or a sentinel this
        integration does not yet understand.
        """
        value = self.native_value
        return value is not None and 0 <= value <= _MAX_LITERS

    async def async_set_native_value(self, value: float) -> None:
        """Write the threshold and publish only the verified read-back."""
        milliliters = round(value * 1000)
        if not 0 <= milliliters <= THRESHOLD_MAX_PLAUSIBLE_ML:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="threshold_out_of_range",
                translation_placeholders={
                    "value": str(value),
                    "maximum": str(_MAX_LITERS),
                },
            )
        if self._raw_value is None:
            # Never verify an encoding that has not been observed first.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="setting_never_read",
                translation_placeholders={"setting": self.entity_key.key},
            )

        update = await self._coordinator.async_write_threshold(
            self.entity_key.key, encode_threshold(milliliters)
        )
        # Publish through the processor so the verified value reaches restore
        # storage, not just this entity's state.
        self.processor.async_handle_update(update)
        # Per-key dispatch is filtered to changed values, so a write that
        # confirms the existing value would otherwise never write state.
        self.async_write_ha_state()
