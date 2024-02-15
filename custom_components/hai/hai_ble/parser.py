"""Parser for BLE BLE advertisements.
"""
from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass
from enum import Enum, auto

from bleak import BleakError, BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
    retry_bluetooth_connection_error,
)
from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data import SensorDeviceClass, SensorUpdate, Units
from sensor_state_data.enum import StrEnum


_LOGGER = logging.getLogger(__name__)


class HaiSensor(StrEnum):
    BATTERY_VOLTAGE = "battery_voltage"
    CURRENT_SHOWER_DURATION = "current_session_duration"
    CURRENT_SHOWER_TEMP = "current_session_temp"
    CURRENT_SHOWER_VOLUME = "current_shower_volume"
    LAST_SHOWER_VOLUME = "last_shower_volume"
    LAST_SHOWER_AVG_TEMP = "last_shower_avg_temp"
    LAST_SHOWER_DURATION = "last_shower_duration"


class HaiBinarySensor(StrEnum):
    SHOWERING = "showering"


class Models(Enum):
    SpaRegular = auto()


@dataclass
class ModelDescription:
    device_type: str


DEVICE_TYPES = {
    Models.SpaRegular: ModelDescription("Spa Regular"),
}

class HaiGattReader:
    XOR_DECRYPTION_KEY = [1, 2, 3, 4, 5, 6]  # Yes, for real

    def __init__(self, client: BleakClientWithServiceCache):
        self._client = client

    def decrypt(self, data, key):
        return bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])

    async def read(self, characteristic_id: str, byte_layout: str, encrypted: bool):
        data = await self._client.read_gatt_char(characteristic_id)

        _LOGGER.debug("Read characteristic for Hai %s=%s", characteristic_id, data.hex())

        if encrypted:
            data = self.decrypt(data, HaiGattReader.XOR_DECRYPTION_KEY)

        return struct.unpack(byte_layout, data)


class HaiBluetoothDeviceData(BluetoothData):
    """Data for Hai BLE shower heads."""

    def supported(self, d):
        return True

    def poll_needed(
        self, service_info: BluetoothServiceInfo, last_poll: float | None
    ) -> bool:
        """
        This is called every time we get a service_info for a device. It means the
        device is working and online.
        """
        return True

    @retry_bluetooth_connection_error()
    async def _get_payload(self, client: BleakClientWithServiceCache) -> None:
        """Get the payload from the shower head using its gatt_characteristics."""
        reader = HaiGattReader(client)


        ###
        # Current shower
        ###
        vals = await reader.read("e6221401-e12f-40f2-b0f5-aaa011c0aa8d", "<I", encrypted=False)
        session_id = vals[0]

        current_shower_duration_seconds, current_shower_temp_celcius, current_shower_volume_ml = 0, 0, 0;

        if session_id:
            vals = await reader.read("e6221406-e12f-40f2-b0f5-aaa011c0aa8d", "<H", encrypted=True)
            current_shower_duration_seconds = vals[0]

            vals = await reader.read("e6221402-e12f-40f2-b0f5-aaa011c0aa8d", "<H", encrypted=False)
            current_shower_temp_celcius = vals[0]

            vals = await reader.read("e6221404-e12f-40f2-b0f5-aaa011c0aa8d", "<I", encrypted=True)
            current_shower_volume_ml = vals[0]

        self.update_sensor(
            str(HaiSensor.CURRENT_SHOWER_DURATION),
            Units.TIME_SECONDS,
            current_shower_duration_seconds,
            SensorDeviceClass.DURATION,
        )

        self.update_sensor(
            str(HaiSensor.CURRENT_SHOWER_TEMP),
            Units.TEMP_CELSIUS,
            current_shower_temp_celcius / 100.0,
            SensorDeviceClass.TEMPERATURE,
        )

        self.update_sensor(
            str(HaiSensor.CURRENT_SHOWER_VOLUME),
            Units.VOLUME_MILLILITERS,
            current_shower_volume_ml,
            SensorDeviceClass.VOLUME,
        )

        ###
        # Last Shower
        ###
        (
            last_shower_session,
            last_shower_temp_celcius,
            last_shower_duration_seconds,
            last_shower_volume_ml,
            last_shower_start_ts,
            last_shower_initial_temp,
        ) = await reader.read("e622140a-e12f-40f2-b0f5-aaa011c0aa8d", "<IHHIIH", encrypted=True)

        self.update_sensor(
            str(HaiSensor.LAST_SHOWER_VOLUME),
            Units.VOLUME_MILLILITERS,
            last_shower_volume_ml,
            SensorDeviceClass.VOLUME,
        )

        self.update_sensor(
            str(HaiSensor.LAST_SHOWER_AVG_TEMP),
            Units.TEMP_CELSIUS,
            last_shower_temp_celcius / 100.0,
            SensorDeviceClass.TEMPERATURE,
        )

        self.update_sensor(
            str(HaiSensor.LAST_SHOWER_DURATION),
            Units.TIME_SECONDS,
            last_shower_duration_seconds,
            SensorDeviceClass.DURATION,
        )

        _LOGGER.debug("Successfully read Hai active gatt characters")

    async def async_poll(self, ble_device: BLEDevice) -> SensorUpdate:
        """
        Poll the device to retrieve any values we can't get from passive listening.
        """
        _LOGGER.debug("Polling Hai device: %s", ble_device.address)
        client = await establish_connection(
            BleakClientWithServiceCache, ble_device, ble_device.address
        )
        try:
            await self._get_payload(client)
        except BleakError as err:
            _LOGGER.warning(f"Reading gatt characters from Hai failed with err: {err}")
        finally:
            await client.disconnect()
            _LOGGER.debug("Disconnected from Hai active bluetooth client")
        return self._finish_update()
