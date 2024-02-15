"""Support for Hai BLE sensors."""
from __future__ import annotations

from .hai_ble import HaiSensor, SensorUpdate

from homeassistant import config_entries
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothProcessorCoordinator,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfVolume,
    PERCENTAGE,
    EntityCategory,
    UnitOfTime,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info

from .const import DOMAIN
from .device import device_key_to_bluetooth_entity_key

SENSOR_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    ###
    # Diag
    ###

    HaiSensor.BATTERY_VOLTAGE: SensorEntityDescription(
        key=HaiSensor.BATTERY_VOLTAGE,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        name="Battery Voltage",
    ),

    ###
    # Current Shower
    ###

    HaiSensor.CURRENT_SHOWER_DURATION: SensorEntityDescription(
        key=HaiSensor.CURRENT_SHOWER_DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        name="Current Shower Duration",
    ),

    HaiSensor.CURRENT_SHOWER_TEMP: SensorEntityDescription(
        key=HaiSensor.CURRENT_SHOWER_TEMP,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        name="Current Temperature",
    ),

    HaiSensor.CURRENT_SHOWER_VOLUME: SensorEntityDescription(
        key=HaiSensor.CURRENT_SHOWER_VOLUME,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLUME,
        name="Current Shower Volume",
    ),

    ###
    # Last Shower
    ###

    HaiSensor.LAST_SHOWER_VOLUME: SensorEntityDescription(
        key=HaiSensor.LAST_SHOWER_VOLUME,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.VOLUME,
        name="Last Shower Volume",
    ),

    HaiSensor.LAST_SHOWER_AVG_TEMP: SensorEntityDescription(
        key=HaiSensor.LAST_SHOWER_AVG_TEMP,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        name="Last Shower Avg Temperature",
    ),

    HaiSensor.LAST_SHOWER_DURATION: SensorEntityDescription(
        key=HaiSensor.LAST_SHOWER_DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        name="Last Shower Duration",
    ),
}


def sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate:
    """Convert a sensor update to a bluetooth data update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: sensor_device_info_to_hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): SENSOR_DESCRIPTIONS[
                device_key.key
            ]
            for device_key in sensor_update.entity_descriptions
        },
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
        entity_names={},
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Hai BLE sensors."""
    coordinator: PassiveBluetoothProcessorCoordinator = hass.data[DOMAIN][
        entry.entry_id
    ]
    processor = PassiveBluetoothDataProcessor(sensor_update_to_bluetooth_data_update)
    entry.async_on_unload(
        processor.async_add_entities_listener(
            HaiBluetoothSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )


class HaiBluetoothSensorEntity(
    PassiveBluetoothProcessorEntity[PassiveBluetoothDataProcessor[str | int | None]],
    SensorEntity,
):
    """Representation of a Hai sensor."""

    @property
    def native_value(self) -> str | int | None:
        """Return the native value."""
        return self.processor.entity_data.get(self.entity_key)

    @property
    def available(self) -> bool:
        """Return True if entity is available.

        The sensor is only created when the device is seen.

        Since these are sleepy devices which stop broadcasting
        when not in use, we can't rely on the last update time
        so once we have seen the device we always return True.
        """
        return True

    @property
    def assumed_state(self) -> bool:
        """Return True if the device is no longer broadcasting."""
        return not self.processor.available
