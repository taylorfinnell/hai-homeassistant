"""Bluetooth test helpers.

Trimmed copies of helpers from Home Assistant Core's
``tests/components/bluetooth/__init__.py`` (Apache-2.0), which
pytest-homeassistant-custom-component does not package.
"""

from __future__ import annotations

import time
from typing import Any

from bleak.backends.scanner import AdvertisementData, BLEDevice
from homeassistant.components.bluetooth import (
    SOURCE_LOCAL,
    BluetoothServiceInfoBleak,
    async_get_advertisement_callback,
)
from homeassistant.core import HomeAssistant

ADVERTISEMENT_DATA_DEFAULTS = {
    "local_name": "",
    "manufacturer_data": {},
    "service_data": {},
    "service_uuids": [],
    "rssi": -127,
    "platform_data": ((),),
    "tx_power": -127,
}

BLE_DEVICE_DEFAULTS = {
    "name": None,
    "details": None,
}


def generate_advertisement_data(**kwargs: Any) -> AdvertisementData:
    """Generate advertisement data with defaults."""
    new = kwargs.copy()
    for key, value in ADVERTISEMENT_DATA_DEFAULTS.items():
        new.setdefault(key, value)
    return AdvertisementData(**new)


def generate_ble_device(
    address: str | None = None,
    name: str | None = None,
    details: Any | None = None,
    **kwargs: Any,
) -> BLEDevice:
    """Generate a BLEDevice with defaults."""
    new = kwargs.copy()
    if address is not None:
        new["address"] = address
    if name is not None:
        new["name"] = name
    if details is not None:
        new["details"] = details
    for key, value in BLE_DEVICE_DEFAULTS.items():
        new.setdefault(key, value)
    return BLEDevice(**new)


def make_service_info(
    address: str,
    name: str,
    advertisement_time: float | None = None,
    connectable: bool = True,
    manufacturer_data: dict[int, bytes] | None = None,
) -> BluetoothServiceInfoBleak:
    """Build a BluetoothServiceInfoBleak for a Hai-style advertisement."""
    adv = generate_advertisement_data(
        local_name=name, manufacturer_data=manufacturer_data or {}
    )
    device = generate_ble_device(address=address, name=name, details={})
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=adv.rssi,
        manufacturer_data=adv.manufacturer_data,
        service_data=adv.service_data,
        service_uuids=adv.service_uuids,
        source=SOURCE_LOCAL,
        device=device,
        advertisement=adv,
        connectable=connectable,
        time=advertisement_time if advertisement_time is not None else time.monotonic(),
        tx_power=adv.tx_power,
        raw=None,
    )


def inject_bluetooth_service_info_bleak(
    hass: HomeAssistant, info: BluetoothServiceInfoBleak
) -> None:
    """Inject a BluetoothServiceInfoBleak into the manager."""
    async_get_advertisement_callback(hass)(info)


def inject_hai_advertisement(
    hass: HomeAssistant,
    address: str,
    name: str = "haiS0123456",
    advertisement_time: float | None = None,
    manufacturer_data: dict[int, bytes] | None = None,
) -> BluetoothServiceInfoBleak:
    """Inject a connectable Hai wake advertisement and return its info."""
    info = make_service_info(
        address, name, advertisement_time, manufacturer_data=manufacturer_data
    )
    inject_bluetooth_service_info_bleak(hass, info)
    return info
