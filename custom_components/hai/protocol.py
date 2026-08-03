"""Local BLE protocol client for Hai smart shower heads.

This module owns every protocol mechanic: connection lifecycle, capability
discovery, serialized GATT reads and writes, the XOR transform, payload
validation, and unit conversion into one typed snapshot. It is intentionally
free of Home Assistant imports so protocol fixtures can run without Home
Assistant.

Encryption of the configuration characteristics (the ``e62215xx`` block) is
UNVERIFIED. The published protocol notes claim they are plaintext; the observed
samples say otherwise:

* the composite ``e622150d`` blob XOR-decodes to eight leading zero bytes,
  which is a 2**-64 coincidence if it were really plaintext;
* that blob's temperature-colour field XOR-decodes to ``00 ff 00`` (pure
  green), exactly matching ``e6221509`` XOR-decoded;
* ``e6221503`` XOR-decodes to 75,708 mL (plausible) versus 67,249,597 mL
  (implausible) read as plaintext.

Only ``e6221501``/``e6221502`` argue for plaintext, and those samples are
almost certainly placeholders rather than captures. This module therefore ships
``encrypted=True`` for the whole block.

Read-back verification CANNOT settle this. XOR is symmetric, so writing a value
to a device that decrypts on write and encrypts on read stores the wrong bytes
and still reads back exactly what was written. Only comparing against the hai
app resolves it -- see the diagnostics ``settings`` block, which carries the
raw wire bytes for that purpose.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import struct

from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

# Repeating XOR key used by the firmware for the encrypted characteristics.
XOR_KEY = bytes((1, 2, 3, 4, 5, 6))

_CONNECT_MAX_ATTEMPTS = 2

# Raw device units per millilitre for the threshold characteristics. The vendor
# notes label them "mL"; see the module docstring for why that is unconfirmed.
# Correcting the scale after hardware testing changes this constant alone,
# because raw device units are what gets stored.
THRESHOLD_UNITS_PER_ML = 1

# A decoded threshold above this is not a shower volume, it is a decoding
# error. Entities refuse to publish (and therefore refuse to write) past it.
THRESHOLD_MAX_PLAUSIBLE_ML = 200_000


class HaiProtocolError(Exception):
    """Raised when a required characteristic is missing or malformed."""


class HaiUnsupportedError(HaiProtocolError):
    """The characteristic is absent or does not support write-with-response."""


class HaiWriteVerificationError(HaiProtocolError):
    """The device did not report the written value back."""


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

# Configuration block. Signed 32-bit thresholds and 3-byte RGB colours, all
# read+write. See the module docstring for the encryption caveat.
FIRST_LEVEL_THRESHOLD = CharacteristicSpec(
    "first_level_threshold", _uuid("1501"), "<i", True, False
)
SECOND_LEVEL_THRESHOLD = CharacteristicSpec(
    "second_level_threshold", _uuid("1502"), "<i", True, False
)
THIRD_LEVEL_THRESHOLD = CharacteristicSpec(
    "third_level_threshold", _uuid("1503"), "<i", True, False
)
FIRST_LEVEL_COLOR = CharacteristicSpec(
    "first_level_color", _uuid("1505"), "<BBB", True, False
)
SECOND_LEVEL_COLOR = CharacteristicSpec(
    "second_level_color", _uuid("1506"), "<BBB", True, False
)
THIRD_LEVEL_COLOR = CharacteristicSpec(
    "third_level_color", _uuid("1507"), "<BBB", True, False
)
FOURTH_LEVEL_COLOR = CharacteristicSpec(
    "fourth_level_color", _uuid("1508"), "<BBB", True, False
)
TEMPERATURE_LEVEL_COLOR = CharacteristicSpec(
    "temperature_level_color", _uuid("1509"), "<BBB", True, False
)
# Composite threshold+colour record. Read into diagnostics as a cross-check on
# the individual characteristics; never decoded into an entity and never
# written, because its layout is unknown.
LEVEL_CONFIGURATION = CharacteristicSpec(
    "level_configuration", _uuid("150d"), "", True, False
)

# Deliberately NOT modelled, and not to be added later without a decision:
#   e622150a  shower-history erase  - destructive
#   e622150e  factory reset         - destructive
#   e6221504  device time           - write-only, cannot be verified by read-back

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

THRESHOLD_CHARACTERISTICS: tuple[CharacteristicSpec, ...] = (
    FIRST_LEVEL_THRESHOLD,
    SECOND_LEVEL_THRESHOLD,
    THIRD_LEVEL_THRESHOLD,
)

COLOR_CHARACTERISTICS: tuple[CharacteristicSpec, ...] = (
    FIRST_LEVEL_COLOR,
    SECOND_LEVEL_COLOR,
    THIRD_LEVEL_COLOR,
    FOURTH_LEVEL_COLOR,
    TEMPERATURE_LEVEL_COLOR,
)

# Configuration only changes when a human changes it, so these are read once
# per wake generation rather than on every poll. They are deliberately kept out
# of OPTIONAL_CHARACTERISTICS, which is the every-poll set.
SETTINGS_CHARACTERISTICS: tuple[CharacteristicSpec, ...] = (
    *THRESHOLD_CHARACTERISTICS,
    *COLOR_CHARACTERISTICS,
    LEVEL_CONFIGURATION,
)

THRESHOLD_SPECS: dict[str, CharacteristicSpec] = {
    spec.key: spec for spec in THRESHOLD_CHARACTERISTICS
}


def format_app_version(raw: int) -> str:
    """Format the raw app version without losing trailing zeros (110 -> 1.10)."""
    return f"{raw // 100}.{raw % 100:02d}"


def format_color(values: tuple[int, ...]) -> str:
    """Format a decoded RGB triple as #RRGGBB."""
    red, green, blue = values
    return f"#{red:02X}{green:02X}{blue:02X}"


def decode_threshold(raw_value: int) -> int:
    """Convert a raw threshold reading to millilitres."""
    return raw_value // THRESHOLD_UNITS_PER_ML


def encode_threshold(milliliters: int) -> int:
    """Convert millilitres to raw device threshold units."""
    return milliliters * THRESHOLD_UNITS_PER_ML


def _centi_to_celsius(raw: int) -> float:
    return raw / 100.0


def _ml_per_s_to_l_per_min(raw: int) -> float:
    return raw * 60.0 / 1000.0


def _epoch_to_datetime(raw: int) -> datetime | None:
    if raw == 0:
        return None
    return datetime.fromtimestamp(raw, tz=UTC)


@dataclass(frozen=True, slots=True)
class HaiSettings:
    """Device configuration read from the e62215xx block.

    ``thresholds_raw`` holds raw device units, not millilitres: the scale is
    unverified, so conversion happens at the entity boundary and a correction
    never changes the meaning of an already-stored value.

    ``raw_hex`` carries the untouched wire bytes of every settings
    characteristic that was read. It exists so the encryption question in the
    module docstring can be settled from a downloaded diagnostics file without
    another hardware session.
    """

    thresholds_raw: dict[str, int] = field(default_factory=dict)
    led_colors: dict[str, str] = field(default_factory=dict)
    raw_hex: dict[str, str] = field(default_factory=dict)
    supported_keys: frozenset[str] = frozenset()
    writable_keys: frozenset[str] = frozenset()


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

    ``settings`` is tri-state: ``None`` means "not read on this poll", an empty
    ``HaiSettings`` means "read, and this firmware exposes none".
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
    settings: HaiSettings | None = None


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
    """Serialized read and write access to one Hai device.

    Connection and required-read failures escape to the caller (the polling
    coordinator); only missing or malformed *optional* characteristics are
    swallowed, logged, and omitted from the snapshot. One lock serializes
    everything, so a user-initiated write can never interleave with a poll.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _async_connected(
        self, ble_device: BLEDevice
    ) -> AsyncIterator[BleakClientWithServiceCache]:
        """Serialize access to the device, connect, and always disconnect."""
        async with self._lock:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.name or ble_device.address,
                max_attempts=_CONNECT_MAX_ATTEMPTS,
            )
            try:
                yield client
            finally:
                await client.disconnect()

    async def async_poll(
        self, ble_device: BLEDevice, *, read_settings: bool = False
    ) -> HaiSnapshot:
        """Connect, read a full snapshot, and always disconnect."""
        async with self._async_connected(ble_device) as client:
            return await self._async_read_snapshot(
                client, ble_device, read_settings=read_settings
            )

    async def async_write_threshold(
        self, ble_device: BLEDevice, spec: CharacteristicSpec, raw_value: int
    ) -> int:
        """Write one threshold in raw device units and verify by read-back.

        Takes and returns raw units, not millilitres: the scale is unverified,
        so the conversion lives at the entity boundary and stored values never
        change meaning if it turns out to be wrong.
        """
        payload = struct.pack(spec.fmt, raw_value)
        stored = await self._async_write_verified(ble_device, spec, payload)
        (stored_value,) = struct.unpack(spec.fmt, stored)
        return stored_value

    async def _async_write_verified(
        self, ble_device: BLEDevice, spec: CharacteristicSpec, payload: bytes
    ) -> bytes:
        """Write plaintext ``payload`` and confirm the device reports it back."""
        async with self._async_connected(ble_device) as client:
            characteristic = client.services.get_characteristic(spec.uuid)
            if characteristic is None:
                raise HaiUnsupportedError(f"{spec.key}: characteristic not present")
            if "write" not in characteristic.properties:
                raise HaiUnsupportedError(
                    f"{spec.key}: characteristic is not writable"
                    f" ({', '.join(characteristic.properties)})"
                )
            wire = xor_transform(payload) if spec.encrypted else payload
            _LOGGER.debug(
                "Writing %s: plaintext %s, wire %s",
                spec.key,
                payload.hex(),
                wire.hex(),
            )
            await client.write_gatt_char(characteristic, wire, response=True)
            raw = bytes(await client.read_gatt_char(characteristic))
            stored = xor_transform(raw) if spec.encrypted else raw
            if stored != payload:
                raise HaiWriteVerificationError(
                    f"{spec.key}: wrote {payload.hex()},"
                    f" device reports {stored.hex()}"
                )
            return stored

    async def _async_read_snapshot(
        self,
        client: BleakClientWithServiceCache,
        ble_device: BLEDevice,
        *,
        read_settings: bool = False,
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

        # Settings are read last and never fail the poll: configuration is not
        # worth discarding a snapshot whose live values already succeeded.
        settings: HaiSettings | None = None
        if read_settings:
            try:
                settings = await self._async_read_settings(client)
            except (HaiProtocolError, BleakError, TimeoutError, OSError) as err:
                _LOGGER.warning(
                    "Could not read device settings, keeping cached values: %s", err
                )

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
            settings=settings,
        )

    async def _async_read_settings(
        self, client: BleakClientWithServiceCache
    ) -> HaiSettings:
        """Read the configuration block, keeping raw bytes for diagnostics."""
        present, writable = self._probe_settings(client.services)
        thresholds: dict[str, int] = {}
        led_colors: dict[str, str] = {}
        raw_hex: dict[str, str] = {}

        for spec in SETTINGS_CHARACTERISTICS:
            if spec.key not in present:
                continue
            raw = await self._async_read_raw(client, spec)
            raw_hex[spec.key] = raw.hex()
            if not spec.fmt:
                # The composite blob: recorded, never decoded.
                continue
            try:
                values = self._decode_values(spec, raw)
            except HaiProtocolError as err:
                present.discard(spec.key)
                writable.discard(spec.key)
                _LOGGER.warning(
                    "Ignoring malformed settings characteristic %s: %s", spec.key, err
                )
                continue
            if spec in THRESHOLD_CHARACTERISTICS:
                thresholds[spec.key] = values[0]
            else:
                led_colors[spec.key] = format_color(values)

        return HaiSettings(
            thresholds_raw=thresholds,
            led_colors=led_colors,
            raw_hex=raw_hex,
            supported_keys=frozenset(present),
            writable_keys=frozenset(writable),
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

    def _probe_settings(
        self, services: BleakGATTServiceCollection
    ) -> tuple[set[str], set[str]]:
        """Return (present, writable) settings keys for this connection.

        Writability needs its own probe: presence alone does not tell the number
        platform whether a threshold can be set.
        """
        present: set[str] = set()
        writable: set[str] = set()
        for spec in SETTINGS_CHARACTERISTICS:
            characteristic = services.get_characteristic(spec.uuid)
            if characteristic is None:
                _LOGGER.debug(
                    "Settings characteristic %s (%s) not present", spec.key, spec.uuid
                )
                continue
            present.add(spec.key)
            # write-with-response only; write-without-response cannot be used
            # with response=True and gives a backend-specific failure.
            if "write" in characteristic.properties:
                writable.add(spec.key)
        return present, writable

    async def _async_read_raw(
        self, client: BleakClientWithServiceCache, spec: CharacteristicSpec
    ) -> bytes:
        return bytes(await client.read_gatt_char(spec.uuid))

    def _decode_values(
        self, spec: CharacteristicSpec, raw: bytes
    ) -> tuple[int, ...]:
        payload = xor_transform(raw) if spec.encrypted else raw
        expected = struct.calcsize(spec.fmt)
        if len(payload) != expected:
            raise HaiProtocolError(
                f"{spec.key}: expected {expected} bytes,"
                f" got {len(payload)} ({payload.hex()})"
            )
        return struct.unpack(spec.fmt, payload)

    async def _async_read_values(
        self, client: BleakClientWithServiceCache, spec: CharacteristicSpec
    ) -> tuple[int, ...]:
        return self._decode_values(spec, await self._async_read_raw(client, spec))

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
