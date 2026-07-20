"""Local BLE protocol client for Hai smart shower heads.

This module owns every protocol mechanic: connection lifecycle, capability
discovery, serialized GATT reads, the XOR transform, payload validation, and
unit conversion into one typed snapshot. It is intentionally free of Home
Assistant imports so protocol fixtures can run without Home Assistant.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import struct

from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "e6221400-e12f-40f2-b0f5-aaa011c0aa8d"

# Repeating XOR key used by the firmware for the encrypted characteristics.
XOR_KEY = bytes((1, 2, 3, 4, 5, 6))

_CONNECT_MAX_ATTEMPTS = 2


class HaiProtocolError(Exception):
    """Raised when a required characteristic is missing or malformed."""


def xor_transform(data: bytes, key: bytes = XOR_KEY) -> bytes:
    """Apply the repeating XOR transform (symmetric for reads and writes)."""
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))


def _uuid(short: str) -> str:
    return f"e622{short}-e12f-40f2-b0f5-aaa011c0aa8d"


@dataclass(frozen=True, slots=True)
class CharacteristicSpec:
    """One GATT characteristic: identity, byte layout, and poll policy."""

    key: str
    uuid: str
    fmt: str
    encrypted: bool
    required: bool


SESSION_ID = CharacteristicSpec("session_id", _uuid("1401"), "<I", False, True)
CURRENT_TEMPERATURE = CharacteristicSpec(
    "current_temperature", _uuid("1402"), "<H", False, True
)
AVERAGE_TEMPERATURE = CharacteristicSpec(
    "average_temperature", _uuid("1403"), "<H", False, True
)
CURRENT_VOLUME = CharacteristicSpec("current_volume", _uuid("1404"), "<I", True, True)
FLOW_RATE = CharacteristicSpec("flow_rate", _uuid("1405"), "<I", True, False)
CURRENT_DURATION = CharacteristicSpec(
    "current_duration", _uuid("1406"), "<H", True, True
)
CURRENT_START_TIME = CharacteristicSpec(
    "current_start_time", _uuid("1407"), "<I", True, False
)
LIFETIME_VOLUME = CharacteristicSpec("lifetime_volume", _uuid("1408"), "<I", True, True)
LIFETIME_AVERAGE_TEMPERATURE = CharacteristicSpec(
    "lifetime_average_temperature", _uuid("1409"), "<H", False, False
)
LAST_SHOWER = CharacteristicSpec("last_shower", _uuid("140a"), "<IHHIIH", True, True)
PRODUCT_ID = CharacteristicSpec("product_id", _uuid("140b"), "", False, True)
BATTERY_VOLTAGE = CharacteristicSpec(
    "battery_voltage", _uuid("140c"), "<H", False, False
)
APP_VERSION = CharacteristicSpec("app_version", _uuid("150b"), "<H", False, True)
BOOTLOADER_VERSION = CharacteristicSpec(
    "bootloader_version", _uuid("150c"), "<B", False, True
)

REQUIRED_CHARACTERISTICS: tuple[CharacteristicSpec, ...] = (
    SESSION_ID,
    CURRENT_TEMPERATURE,
    AVERAGE_TEMPERATURE,
    CURRENT_VOLUME,
    CURRENT_DURATION,
    LIFETIME_VOLUME,
    LAST_SHOWER,
    PRODUCT_ID,
    APP_VERSION,
    BOOTLOADER_VERSION,
)

# Characteristics not present on every firmware; probed per connection and
# omitted (never failed) when the device does not expose them.
OPTIONAL_CHARACTERISTICS: tuple[CharacteristicSpec, ...] = (
    FLOW_RATE,
    CURRENT_START_TIME,
    LIFETIME_AVERAGE_TEMPERATURE,
    BATTERY_VOLTAGE,
)


def format_app_version(raw: int) -> str:
    """Format the raw app version without losing trailing zeros (110 -> 1.10)."""
    return f"{raw // 100}.{raw % 100:02d}"


def _centi_to_celsius(raw: int) -> float:
    return raw / 100.0


def _ml_per_s_to_l_per_min(raw: int) -> float:
    return raw * 60.0 / 1000.0


def _epoch_to_datetime(raw: int) -> datetime | None:
    if raw == 0:
        return None
    return datetime.fromtimestamp(raw, tz=UTC)


@dataclass(frozen=True, slots=True)
class HaiLastShower:
    """Decoded record of the most recently completed shower."""

    session_id: int
    temperature_c: float
    initial_temperature_c: float
    duration_s: int
    volume_ml: int
    start_time: datetime | None


@dataclass(frozen=True, slots=True)
class HaiSnapshot:
    """One complete, typed poll result.

    ``None`` in a current_* field means "no active session" or "characteristic
    not supported"; ``supported_optional_keys`` records which optional
    characteristics this firmware actually exposes.
    """

    address: str
    name: str
    product_id: str
    app_version: str
    bootloader_version: str
    session_id: int
    current_temperature_c: float | None
    current_average_temperature_c: float | None
    current_volume_ml: int | None
    current_duration_s: int | None
    current_flow_rate_lpm: float | None
    current_start_time: datetime | None
    lifetime_volume_ml: int
    lifetime_average_temperature_c: float | None
    battery_voltage_v: float | None
    last_shower: HaiLastShower
    supported_optional_keys: frozenset[str]


def decode_last_shower(payload: bytes) -> HaiLastShower:
    """Decode the already-decrypted last-shower record."""
    expected = struct.calcsize(LAST_SHOWER.fmt)
    if len(payload) != expected:
        raise HaiProtocolError(
            f"last_shower: expected {expected} bytes, got {len(payload)}"
        )
    session_id, temp, duration_s, volume_ml, start_ts, initial_temp = struct.unpack(
        LAST_SHOWER.fmt, payload
    )
    return HaiLastShower(
        session_id=session_id,
        temperature_c=_centi_to_celsius(temp),
        initial_temperature_c=_centi_to_celsius(initial_temp),
        duration_s=duration_s,
        volume_ml=volume_ml,
        start_time=_epoch_to_datetime(start_ts),
    )


class HaiProtocolClient:
    """Serialized read access to one Hai device.

    Connection and required-read failures escape to the caller (the polling
    coordinator); only missing or malformed *optional* characteristics are
    swallowed, logged, and omitted from the snapshot.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def async_poll(self, ble_device: BLEDevice) -> HaiSnapshot:
        """Connect, read a full snapshot, and always disconnect."""
        async with self._lock:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.name or ble_device.address,
                max_attempts=_CONNECT_MAX_ATTEMPTS,
            )
            try:
                return await self._async_read_snapshot(client, ble_device)
            finally:
                await client.disconnect()

    async def _async_read_snapshot(
        self, client: BleakClientWithServiceCache, ble_device: BLEDevice
    ) -> HaiSnapshot:
        services = client.services
        self._check_required(services)
        supported = self._probe_optional(services)

        product_id_raw = await client.read_gatt_char(PRODUCT_ID.uuid)
        product_id = bytes(product_id_raw).hex().upper()
        if not product_id:
            raise HaiProtocolError("product_id: empty payload")

        (app_version_raw,) = await self._async_read_required(client, APP_VERSION)
        (bootloader_raw,) = await self._async_read_required(client, BOOTLOADER_VERSION)
        (session_id,) = await self._async_read_required(client, SESSION_ID)

        current_temperature_c: float | None = None
        current_average_temperature_c: float | None = None
        current_volume_ml: int | None = None
        current_duration_s: int | None = None
        current_flow_rate_lpm: float | None = None
        current_start_time: datetime | None = None

        if session_id != 0:
            (raw_temp,) = await self._async_read_required(client, CURRENT_TEMPERATURE)
            current_temperature_c = _centi_to_celsius(raw_temp)
            (raw_avg,) = await self._async_read_required(client, AVERAGE_TEMPERATURE)
            current_average_temperature_c = _centi_to_celsius(raw_avg)
            (raw_volume,) = await self._async_read_required(client, CURRENT_VOLUME)
            current_volume_ml = raw_volume
            (raw_duration,) = await self._async_read_required(client, CURRENT_DURATION)
            current_duration_s = raw_duration
            if (
                flow_values := await self._async_read_optional(
                    client, FLOW_RATE, supported
                )
            ) is not None:
                current_flow_rate_lpm = _ml_per_s_to_l_per_min(flow_values[0])
            if (
                start_values := await self._async_read_optional(
                    client, CURRENT_START_TIME, supported
                )
            ) is not None:
                current_start_time = _epoch_to_datetime(start_values[0])

        (lifetime_volume_ml,) = await self._async_read_required(client, LIFETIME_VOLUME)

        lifetime_average_temperature_c: float | None = None
        if (
            lifetime_avg_values := await self._async_read_optional(
                client, LIFETIME_AVERAGE_TEMPERATURE, supported
            )
        ) is not None:
            lifetime_average_temperature_c = _centi_to_celsius(lifetime_avg_values[0])

        battery_voltage_v: float | None = None
        if (
            battery_values := await self._async_read_optional(
                client, BATTERY_VOLTAGE, supported
            )
        ) is not None:
            battery_voltage_v = battery_values[0] / 1000.0

        last_shower_raw = await client.read_gatt_char(LAST_SHOWER.uuid)
        last_shower = decode_last_shower(xor_transform(bytes(last_shower_raw)))

        return HaiSnapshot(
            address=ble_device.address,
            name=ble_device.name or ble_device.address,
            product_id=product_id,
            app_version=format_app_version(app_version_raw),
            bootloader_version=str(bootloader_raw),
            session_id=session_id,
            current_temperature_c=current_temperature_c,
            current_average_temperature_c=current_average_temperature_c,
            current_volume_ml=current_volume_ml,
            current_duration_s=current_duration_s,
            current_flow_rate_lpm=current_flow_rate_lpm,
            current_start_time=current_start_time,
            lifetime_volume_ml=lifetime_volume_ml,
            lifetime_average_temperature_c=lifetime_average_temperature_c,
            battery_voltage_v=battery_voltage_v,
            last_shower=last_shower,
            supported_optional_keys=frozenset(supported),
        )

    def _check_required(self, services: BleakGATTServiceCollection) -> None:
        missing = [
            spec.key
            for spec in REQUIRED_CHARACTERISTICS
            if services.get_characteristic(spec.uuid) is None
        ]
        if missing:
            raise HaiProtocolError(
                "Missing required characteristics (factory firmware is not"
                f" supported): {', '.join(missing)}"
            )

    def _probe_optional(self, services: BleakGATTServiceCollection) -> set[str]:
        supported: set[str] = set()
        for spec in OPTIONAL_CHARACTERISTICS:
            if services.get_characteristic(spec.uuid) is not None:
                supported.add(spec.key)
            else:
                _LOGGER.debug(
                    "Optional characteristic %s (%s) not present", spec.key, spec.uuid
                )
        return supported

    async def _async_read_values(
        self, client: BleakClientWithServiceCache, spec: CharacteristicSpec
    ) -> tuple[int, ...]:
        raw = await client.read_gatt_char(spec.uuid)
        payload = xor_transform(bytes(raw)) if spec.encrypted else bytes(raw)
        expected = struct.calcsize(spec.fmt)
        if len(payload) != expected:
            raise HaiProtocolError(
                f"{spec.key}: expected {expected} bytes,"
                f" got {len(payload)} ({payload.hex()})"
            )
        return struct.unpack(spec.fmt, payload)

    async def _async_read_required(
        self, client: BleakClientWithServiceCache, spec: CharacteristicSpec
    ) -> tuple[int, ...]:
        return await self._async_read_values(client, spec)

    async def _async_read_optional(
        self,
        client: BleakClientWithServiceCache,
        spec: CharacteristicSpec,
        supported: set[str],
    ) -> tuple[int, ...] | None:
        if spec.key not in supported:
            return None
        try:
            return await self._async_read_values(client, spec)
        except HaiProtocolError as err:
            supported.discard(spec.key)
            _LOGGER.warning(
                "Ignoring malformed optional characteristic %s: %s", spec.key, err
            )
            return None
