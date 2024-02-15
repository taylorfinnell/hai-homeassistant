"""Parser for Hai BLE advertisements."""
from __future__ import annotations

from .parser import HaiBinarySensor, HaiBluetoothDeviceData, HaiSensor

from sensor_state_data import (
    BinarySensorDeviceClass,
    BinarySensorValue,
    DeviceKey,
    SensorDescription,
    SensorDeviceClass,
    SensorDeviceInfo,
    SensorUpdate,
    SensorValue,
    Units,
)

__version__ = "0.5.3"

__all__ = ["HaiBluetoothDeviceData", "HaiDevice"]
