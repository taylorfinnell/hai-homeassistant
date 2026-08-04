"""Support for the Hai shower activity binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HaiConfigEntry, HaiCoordinator, HaiUpdate, HaiUpdateSource

PARALLEL_UPDATES = 0

SHOWER_ACTIVE_KEY = PassiveBluetoothEntityKey(key="shower_active", device_id=None)

SHOWER_ACTIVE_DESCRIPTION = BinarySensorEntityDescription(
    key="shower_active",
    translation_key="shower_active",
    device_class=BinarySensorDeviceClass.RUNNING,
)


def binary_sensor_update_to_bluetooth_data_update(
    coordinator: HaiCoordinator, update: HaiUpdate
) -> PassiveBluetoothDataUpdate[bool]:
    """Convert a HaiUpdate into a processor update for this platform.

    An advertisement only says the device is awake, which normally means water
    is running. A successful poll knows better: session 0 means no shower is in
    progress, and that ends the activity immediately instead of waiting out the
    multi-minute advertisement timeout.

    The running flag is published as entity data rather than derived in the
    entity so that a change actually reaches the per-key dispatch, which only
    fires for values that changed.
    """
    data: dict[PassiveBluetoothEntityKey, bool] = {}
    if update.source is HaiUpdateSource.ADVERTISEMENT:
        data[SHOWER_ACTIVE_KEY] = True
    elif (snapshot := update.snapshot) is not None:
        data[SHOWER_ACTIVE_KEY] = snapshot.session_id != 0

    return PassiveBluetoothDataUpdate(
        devices={None: coordinator.device_info()},
        entity_descriptions={SHOWER_ACTIVE_KEY: SHOWER_ACTIVE_DESCRIPTION},
        entity_data=data,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Hai binary sensors."""
    coordinator = entry.runtime_data
    processor: PassiveBluetoothDataProcessor[bool, HaiUpdate] = (
        PassiveBluetoothDataProcessor(
            lambda update: binary_sensor_update_to_bluetooth_data_update(
                coordinator, update
            )
        )
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(
            HaiShowerActiveEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(
            processor, BinarySensorEntityDescription
        )
    )


class HaiShowerActiveEntity(
    PassiveBluetoothProcessorEntity[PassiveBluetoothDataProcessor[bool, HaiUpdate]],
    BinarySensorEntity,
):
    """Shower activity from advertisement presence, corrected by polls.

    On while the device is advertising, unless a poll has reported that no
    session is in progress. Off once Home Assistant declares the broadcaster
    unavailable, which can take several minutes -- so this is still a delayed
    convenience trigger, not a safety signal.
    """

    @property
    def is_on(self) -> bool:
        """Return True while a shower is believed to be running."""
        if not self.processor.coordinator.available:
            return False
        # Absent until the first update; presence alone implies running.
        return self.processor.entity_data.get(self.entity_key, True)

    @property
    def available(self) -> bool:
        """Stay available: advertising stopping means off, not broken."""
        return True
