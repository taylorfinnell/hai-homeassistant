"""Parser for Hai BLE devices"""

from __future__ import annotations

import struct
import asyncio
import dataclasses
import struct
from collections import namedtuple
from datetime import datetime
import logging

# from logging import Logger
from math import exp
from typing import Any, Callable, Tuple

from bleak import BleakClient, BleakError
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

READ_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

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


XOR_DECRYPTION_KEY = [1, 2, 3, 4, 5, 6]  # Yes, for real


# pylint: disable=too-many-locals
# pylint: disable=too-many-branches
class HaiBluetoothDeviceData:
    """Data for Hai BLE sensors."""

    _event: asyncio.Event | None
    _command_data: bytearray | None

    def __init__(
        self,
        logger: logging.Logger,
    ):
        super().__init__()
        self.logger = logger
        self.logger.debug("In Device Data")

    def decrypt(self, data, key=XOR_DECRYPTION_KEY):
        return bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])

    async def _get_status(self, client: BleakClient, device: HaiDevice) -> HaiDevice:
        _LOGGER.debug("Getting Status")

        # Current session
        current_session_data = await client.read_gatt_char(
            "e6221401-e12f-40f2-b0f5-aaa011c0aa8d"
        )
        session_id = struct.unpack("<I", current_session_data)[0]

        if session_id == 0:
            _LOGGER.debug("No hai session")
        else:
            _LOGGER.debug("Hai session %x", session_id)

            # Lifetime consumption (XORd)
            lifetime_consumption_data = await client.read_gatt_char(
                "e6221408-e12f-40f2-b0f5-aaa011c0aa8d"
            )
            lifetime_consumption_data = self.decrypt(lifetime_consumption_data)
            device.sensors["total_volume"] = float(
                struct.unpack("<I", lifetime_consumption_data)[0]
            )

            # Current Temp
            current_temp_data = await client.read_gatt_char(
                "e6221402-e12f-40f2-b0f5-aaa011c0aa8d"
            )
            device.sensors["current_temperature"] = (
                struct.unpack("<H", current_temp_data)[0] / 100
            )

            # Current Consumption/Volume (XORed)
            current_consumption_data = await client.read_gatt_char(
                "e6221404-e12f-40f2-b0f5-aaa011c0aa8d"
            )
            current_consumption_data = self.decrypt(current_consumption_data)
            device.sensors["current_volume"] = float(
                struct.unpack("<I", current_consumption_data)[0]
            )

            # Current duration
            current_duration_data = await client.read_gatt_char(
                "e6221406-e12f-40f2-b0f5-aaa011c0aa8d",
            )
            current_duration_data = self.decrypt(current_duration_data)
            device.sensors["current_duration"] = float(
                struct.unpack("<h", current_duration_data)[0]
            )

            # Avg Temp
            average_temp_data = await client.read_gatt_char(
                "e6221403-e12f-40f2-b0f5-aaa011c0aa8d"
            )
            device.sensors["average_temperature"] = float(
                struct.unpack("<h", average_temp_data)[0] / 100
            )

        # Now parse the last shower.
        last_shower_data = await client.read_gatt_char(
            "e622140a-e12f-40f2-b0f5-aaa011c0aa8d"
        )
        last_shower_data = self.decrypt(last_shower_data)
        (
            session,
            temp_celcius,
            duration_seconds,
            volume_ml,
            start_ts,
            initial_temp,
        ) = struct.unpack("<IHHIIH", last_shower_data)
        device.sensors["last_shower_duration"] = duration_seconds
        device.sensors["last_shower_temperature"] = temp_celcius / 100
        device.sensors["last_shower_volume"] = volume_ml

        _LOGGER.debug("Got Status")

        return device

    async def update_device(self, ble_device: BLEDevice) -> HaiDevice:
        """Connects to the device through BLE and retrieves relevant data"""
        _LOGGER.debug("Update Device")
        client = await establish_connection(
            BleakClient, ble_device, ble_device.address, max_attempts=1
        )
        _LOGGER.debug("Got Client")
        # await client.pair()
        device = HaiDevice()
        _LOGGER.debug("Made Device")
        try:
            device = await self._get_status(client, device)
            device.name = ble_device.address  # ble_device.name
            device.address = ble_device.address
            _LOGGER.debug("device.name: %s", device.name)
            _LOGGER.debug("device.address: %s", device.address)
        except:
            _LOGGER.debug("Disconnect")

        await client.disconnect()

        return device
