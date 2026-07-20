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

from .coordinator import HaiConfigEntry, HaiCoordinator, HaiUpdate

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

    Any update (advertisement or poll) means the device is awake right now.
    """
    return PassiveBluetoothDataUpdate(
        devices={None: coordinator.device_info()},
        entity_descriptions={SHOWER_ACTIVE_KEY: SHOWER_ACTIVE_DESCRIPTION},
        entity_data={SHOWER_ACTIVE_KEY: True},
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
    """Shower activity derived from advertisement presence.

    On while the device is advertising; off once Home Assistant declares the
    broadcaster unavailable, which can take several minutes. A delayed
    convenience trigger, not a safety signal.
    """

    @property
    def is_on(self) -> bool:
        """Return True while the shower head is advertising."""
        return self.processor.coordinator.available

    @property
    def available(self) -> bool:
        """Stay available: advertising stopping means off, not broken."""
        return True
