"""Parser for Hai BLE devices"""

from __future__ import annotations

import struct
import asyncio
import dataclasses
import struct
from collections import namedtuple
from datetime import datetime
import logging

from bleak import BleakError
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bluetooth_sensor_state_data import BluetoothData

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class HaiDevice:
    """Response data with information about the Hai device"""

    hw_version: str = ""
    sw_version: str = ""
    name: str = ""
    identifier: str = ""
    address: str = ""
    sensors: dict[str, str | float | None] = dataclasses.field(
        default_factory=lambda: {}
    )

class HaiGattReader:
    XOR_DECRYPTION_KEY = [1, 2, 3, 4, 5, 6]  # Yes, for real

    def __init__(self, client: BleakClientWithServiceCache):
        self._client = client

    def decrypt(self, data, key):
        return bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])

    async def read_raw(self, characteristic_id: str):
        data = await self._client.read_gatt_char(characteristic_id)

        _LOGGER.debug("Reading raw characteristic for Hai %s=%s", characteristic_id, data.hex())

        return data

    async def read(self, characteristic_id: str, byte_layout: str, encrypted: bool):
        data = await self._client.read_gatt_char(characteristic_id)

        _LOGGER.debug("Reading packed characteristic for Hai %s=%s", characteristic_id, data.hex())

        if encrypted:
            data = self.decrypt(data, HaiGattReader.XOR_DECRYPTION_KEY)

        return struct.unpack(byte_layout, data)


# pylint: disable=too-many-locals
# pylint: disable=too-many-branches
class HaiBluetoothDeviceData(BluetoothData):
    """Data for Hai BLE sensors."""

    # habluetooth.models.BluetoothServiceInfoBleak
    def supported(self, service_info):
        advert = service_info.advertisement

        if not advert or not advert.local_name:
            return False

        _LOGGER.debug("Checking if %s is supported", advert.local_name)

        return "hai" in advert.local_name

    async def _get_status(self, client: BleakClientWithServiceCache, device: HaiDevice) -> HaiDevice:
        reader = HaiGattReader(client)

        # Current session
        (session_id,) = await reader.read("e6221401-e12f-40f2-b0f5-aaa011c0aa8d", "<I", encrypted=False)
        _LOGGER.debug("Got Hai session %s", session_id)

        # Software version
        (software_version,) = await reader.read("e622150b-e12f-40f2-b0f5-aaa011c0aa8d", "<H", encrypted=False)
        device.sw_version = str(float(software_version) / 100.0)
        _LOGGER.debug("Got Hai sw_version %s", device.sw_version)

        (hardware_version,) = await reader.read("e622150c-e12f-40f2-b0f5-aaa011c0aa8d", "<B", encrypted=False)
        device.hw_version = str(hardware_version).upper()
        _LOGGER.debug("Got Hai hw_version %s", device.hw_version)

        product_id = await reader.read_raw("e622140b-e12f-40f2-b0f5-aaa011c0aa8d")
        device.identifier = str(product_id.hex()).upper()
        _LOGGER.debug("Got Hai product_id %s", device.identifier)

        if session_id != 0:
            # Lifetime consumption
            (lifetime_consumption_data,) = await reader.read(
                "e6221408-e12f-40f2-b0f5-aaa011c0aa8d",
                "<I",
                encrypted=True
            )
            device.sensors["total_volume"] = lifetime_consumption_data

            # Current Temp
            (current_temp_data,) = await reader.read(
                "e6221402-e12f-40f2-b0f5-aaa011c0aa8d",
                "<H",
                encrypted=False
            )
            device.sensors["current_temperature"] = float(current_temp_data) / 100.0

            # Current Consumption/Volume
            (current_consumption_data,) = await reader.read(
                "e6221404-e12f-40f2-b0f5-aaa011c0aa8d",
                "<I",
                encrypted=True
            )
            device.sensors["current_volume"] = float(current_consumption_data)

            # Current duration
            (current_duration_data,) = await reader.read(
                "e6221406-e12f-40f2-b0f5-aaa011c0aa8d",
                "<H",
                encrypted=True
            )
            device.sensors["current_duration"] = float(current_duration_data)

            # Avg Temp
            (average_temp_data,) = await reader.read(
                "e6221403-e12f-40f2-b0f5-aaa011c0aa8d",
                "<H",
                encrypted=False
            )
            device.sensors["average_temperature"] = float(average_temp_data) / 100.0

        # Now parse the last shower.
        (
            session,
            temp_celcius,
            duration_seconds,
            volume_ml,
            start_ts,
            initial_temp,
        ) = await reader.read("e622140a-e12f-40f2-b0f5-aaa011c0aa8d", "<IHHIIH", encrypted=True)
        device.sensors["last_shower_duration"] = duration_seconds
        device.sensors["last_shower_temperature"] = temp_celcius / 100.0
        device.sensors["last_shower_volume"] = volume_ml

        _LOGGER.debug("Got Status")

        return device

    async def poll_ble_device(self, ble_device: BLEDevice) -> HaiDevice:
        """Connects to the device through BLE and retrieves relevant data"""

        client = await establish_connection(
            BleakClientWithServiceCache, ble_device, ble_device.address, max_attempts=1
        )

        device = HaiDevice()
        try:
            device = await self._get_status(client, device)
            device.name = ble_device.name
            device.address = ble_device.address
        except BleakError as be:
            _LOGGER.error("BLE error fetching data from ble device. Disconnecting...", be)
        except Exception as e:
            _LOGGER.error("Unknown error fetching data from ble device. Disconnecting...", e)

        await client.disconnect()

        return device
