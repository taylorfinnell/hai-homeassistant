"""Support for Hai sensors."""

from __future__ import annotations

from typing import cast

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HaiConfigEntry, HaiCoordinator, HaiUpdate, HaiUpdateSource

# Entities are pushed by the processor; polls are serialized by the
# coordinator's debouncer and the protocol client's lock.
PARALLEL_UPDATES = 0

SENSOR_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    "current_temperature": SensorEntityDescription(
        key="current_temperature",
        translation_key="current_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    "average_temperature": SensorEntityDescription(
        key="average_temperature",
        translation_key="average_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # No state class: an aggregate, not a point-in-time measurement.
        suggested_display_precision=1,
    ),
    "current_volume": SensorEntityDescription(
        key="current_volume",
        translation_key="current_volume",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:shower-head",
    ),
    "current_duration": SensorEntityDescription(
        key="current_duration",
        translation_key="current_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "flow_rate": SensorEntityDescription(
        key="flow_rate",
        translation_key="flow_rate",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    "lifetime_volume": SensorEntityDescription(
        key="lifetime_volume",
        translation_key="lifetime_volume",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        # No state class until the counter's rollover/factory-reset behavior
        # is hardware-verified; enabling statistics on a counter that resets
        # would corrupt long-term data.
        suggested_display_precision=1,
    ),
    "lifetime_average_temperature": SensorEntityDescription(
        key="lifetime_average_temperature",
        translation_key="lifetime_average_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    "last_shower_temperature": SensorEntityDescription(
        key="last_shower_temperature",
        translation_key="last_shower_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    "last_shower_duration": SensorEntityDescription(
        key="last_shower_duration",
        translation_key="last_shower_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    "last_shower_volume": SensorEntityDescription(
        key="last_shower_volume",
        translation_key="last_shower_volume",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        icon="mdi:shower-head",
    ),
    "battery_voltage": SensorEntityDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        suggested_display_precision=2,
    ),
}

# Live values are only meaningful during the wake generation they were read
# in; everything else is retained from cache/restore. Derived from the entity
# key (not stored on the description) so processor restore storage cannot
# lose the policy.
LIVE_SENSOR_KEYS: frozenset[str] = frozenset(
    {
        "current_temperature",
        "average_temperature",
        "current_volume",
        "current_duration",
        "flow_rate",
    }
)

# Keys backed by required characteristics; published with every
# advertisement so replayed discovery recreates entities before the first
# successful poll. Optional keys join only after capability confirmation.
CORE_SENSOR_KEYS: tuple[str, ...] = (
    "current_temperature",
    "average_temperature",
    "current_volume",
    "current_duration",
    "lifetime_volume",
    "last_shower_temperature",
    "last_shower_duration",
    "last_shower_volume",
)

_OPTIONAL_SENSOR_KEYS: tuple[str, ...] = (
    "flow_rate",
    "lifetime_average_temperature",
    "battery_voltage",
)


def _entity_key(key: str) -> PassiveBluetoothEntityKey:
    return PassiveBluetoothEntityKey(key=key, device_id=None)


def sensor_update_to_bluetooth_data_update(
    coordinator: HaiCoordinator, update: HaiUpdate
) -> PassiveBluetoothDataUpdate[float | int | None]:
    """Convert a HaiUpdate into a processor update for the sensor platform.

    Advertisements carry device metadata and the core descriptions but no
    entity data, so cached values are never cleared by a wake signal. A
    successful poll publishes every described live key with a fresh value or
    an explicit None, plus retained values.
    """
    devices = {None: coordinator.device_info()}
    descriptions = {
        _entity_key(key): SENSOR_DESCRIPTIONS[key] for key in CORE_SENSOR_KEYS
    }

    if update.source is HaiUpdateSource.ADVERTISEMENT or update.snapshot is None:
        return PassiveBluetoothDataUpdate(
            devices=devices,
            entity_descriptions=descriptions,
            entity_data={},
        )

    snapshot = update.snapshot
    data: dict[PassiveBluetoothEntityKey, float | int | None] = {
        _entity_key("current_temperature"): snapshot.current_temperature_c,
        _entity_key("average_temperature"): snapshot.current_average_temperature_c,
        _entity_key("current_volume"): snapshot.current_volume_ml,
        _entity_key("current_duration"): snapshot.current_duration_s,
        _entity_key("lifetime_volume"): round(snapshot.lifetime_volume_ml / 1000, 3),
        _entity_key("last_shower_temperature"): snapshot.last_shower.temperature_c,
        _entity_key("last_shower_duration"): snapshot.last_shower.duration_s,
        _entity_key("last_shower_volume"): snapshot.last_shower.volume_ml,
    }
    optional_values: dict[str, float | int | None] = {
        "flow_rate": snapshot.current_flow_rate_lpm,
        "lifetime_average_temperature": snapshot.lifetime_average_temperature_c,
        "battery_voltage": snapshot.battery_voltage_v,
    }
    for key in _OPTIONAL_SENSOR_KEYS:
        if key in snapshot.supported_optional_keys:
            descriptions[_entity_key(key)] = SENSOR_DESCRIPTIONS[key]
            data[_entity_key(key)] = optional_values[key]

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
    """Set up the Hai sensors."""
    coordinator = entry.runtime_data
    processor: PassiveBluetoothDataProcessor[float | int | None, HaiUpdate] = (
        PassiveBluetoothDataProcessor(
            lambda update: sensor_update_to_bluetooth_data_update(coordinator, update)
        )
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(HaiSensorEntity, async_add_entities)
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )


class HaiSensorEntity(
    PassiveBluetoothProcessorEntity[
        PassiveBluetoothDataProcessor[float | int | None, HaiUpdate]
    ],
    SensorEntity,
):
    """A Hai sensor with a live or retained availability policy."""

    @property
    def _coordinator(self) -> HaiCoordinator:
        return cast(HaiCoordinator, self.processor.coordinator)

    @property
    def _is_live(self) -> bool:
        return self.entity_key.key in LIVE_SENSOR_KEYS

    @property
    def native_value(self) -> float | int | None:
        """Return the latest cached value for this key."""
        return self.processor.entity_data.get(self.entity_key)

    @property
    def available(self) -> bool:
        """Apply the live or retained availability policy.

        Live values require current Bluetooth presence, a successful poll for
        the current wake generation, and a non-None value. Retained values
        stay available from cache/restore even while the device sleeps.
        """
        if self._is_live:
            return (
                super().available
                and self._coordinator.tracker.live_data_fresh
                and self.native_value is not None
            )
        return self.native_value is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe live entities beyond the per-key processor dispatch.

        Per-key dispatch only fires when this key's value changes, but live
        availability also changes on new wake generations (advertisement
        updates carry no entity data) and on failed polls (which dispatch
        nothing at all). The unfiltered processor listener covers the former,
        the freshness tracker the latter.
        """
        await super().async_added_to_hass()
        if self._is_live:
            self.async_on_remove(
                self.processor.async_add_listener(self._handle_processor_update)
            )
            self.async_on_remove(
                self._coordinator.tracker.add_listener(self._handle_tracker_reset)
            )

    @callback
    def _handle_tracker_reset(self) -> None:
        self.async_write_ha_state()
