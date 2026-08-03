"""Unit tests for the Hai BLE protocol module (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import struct
from unittest.mock import AsyncMock, patch

from bleak.exc import BleakError
import pytest

from custom_components.hai import protocol
from custom_components.hai.protocol import (
    APP_VERSION,
    AVERAGE_TEMPERATURE,
    BATTERY_VOLTAGE,
    BOOTLOADER_VERSION,
    COLOR_CHARACTERISTICS,
    CURRENT_DURATION,
    CURRENT_START_TIME,
    CURRENT_TEMPERATURE,
    CURRENT_VOLUME,
    FIRST_LEVEL_COLOR,
    FIRST_LEVEL_THRESHOLD,
    FLOW_RATE,
    FOURTH_LEVEL_COLOR,
    LAST_SHOWER,
    LEVEL_CONFIGURATION,
    LIFETIME_AVERAGE_TEMPERATURE,
    LIFETIME_VOLUME,
    PRODUCT_ID,
    SECOND_LEVEL_THRESHOLD,
    SESSION_ID,
    SETTINGS_CHARACTERISTICS,
    TEMPERATURE_LEVEL_COLOR,
    THIRD_LEVEL_THRESHOLD,
    HaiProtocolClient,
    HaiProtocolError,
    HaiUnsupportedError,
    HaiWriteVerificationError,
    decode_last_shower,
    format_app_version,
    format_color,
    xor_transform,
)

from .bluetooth import generate_ble_device

ADDRESS = "AA:BB:CC:DD:EE:FF"

# struct.pack("<IHHIIH", 30, 3173, 756, 106348, 1707528100, 3015) XORed with
# the key; decodes to session 30, 31.73 degC, 756 s, 106348 mL,
# 2024-02-10T01:21:40Z, initial temperature 30.15 degC.
LAST_SHOWER_ENCRYPTED = bytes.fromhex("1f020304600af5006f9b0406a5cdc561c20d")
LAST_SHOWER_PLAIN = bytes.fromhex("1e000000650cf4026c9f0100a4cfc665c70b")

# Wire bytes observed on real hardware. Under the shipped encrypted=True
# hypothesis the third threshold decodes to 75,708 and the temperature colour
# to pure green; read as plaintext they are 67,249,597 and a near-green that
# is one XOR key away from it. See the protocol module docstring.
OBSERVED_THIRD_THRESHOLD = bytes.fromhex("bd250204")
OBSERVED_TEMPERATURE_COLOR = bytes.fromhex("01fd03")
OBSERVED_FOURTH_COLOR = bytes.fromhex("fe2203")
OBSERVED_LEVEL_CONFIGURATION = bytes.fromhex(
    "01020304050601024c410406970e0f04050601020304050601fd038005a4"
)

# Reads issued by one mid-shower poll of a fully featured device.
POLL_READ_COUNT = 14
SETTINGS_READ_COUNT = len(SETTINGS_CHARACTERISTICS)


def _encode(spec: protocol.CharacteristicSpec, *values: int) -> bytes:
    payload = struct.pack(spec.fmt, *values)
    return xor_transform(payload) if spec.encrypted else payload


def default_payloads() -> dict[str, bytes]:
    """GATT payload map for a fully featured, mid-shower device."""
    return {
        SESSION_ID.uuid: _encode(SESSION_ID, 30),
        CURRENT_TEMPERATURE.uuid: _encode(CURRENT_TEMPERATURE, 3173),
        AVERAGE_TEMPERATURE.uuid: _encode(AVERAGE_TEMPERATURE, 3150),
        CURRENT_VOLUME.uuid: _encode(CURRENT_VOLUME, 12345),
        FLOW_RATE.uuid: _encode(FLOW_RATE, 100),
        CURRENT_DURATION.uuid: _encode(CURRENT_DURATION, 120),
        CURRENT_START_TIME.uuid: _encode(CURRENT_START_TIME, 1707528100),
        LIFETIME_VOLUME.uuid: _encode(LIFETIME_VOLUME, 106348),
        LIFETIME_AVERAGE_TEMPERATURE.uuid: _encode(LIFETIME_AVERAGE_TEMPERATURE, 3090),
        LAST_SHOWER.uuid: LAST_SHOWER_ENCRYPTED,
        PRODUCT_ID.uuid: bytes.fromhex("0a1b2c"),
        BATTERY_VOLTAGE.uuid: _encode(BATTERY_VOLTAGE, 3537),
        APP_VERSION.uuid: _encode(APP_VERSION, 110),
        BOOTLOADER_VERSION.uuid: _encode(BOOTLOADER_VERSION, 3),
        # Configuration block.
        FIRST_LEVEL_THRESHOLD.uuid: _encode(FIRST_LEVEL_THRESHOLD, 20000),
        SECOND_LEVEL_THRESHOLD.uuid: _encode(SECOND_LEVEL_THRESHOLD, 40000),
        THIRD_LEVEL_THRESHOLD.uuid: OBSERVED_THIRD_THRESHOLD,
        COLOR_CHARACTERISTICS[0].uuid: _encode(COLOR_CHARACTERISTICS[0], 0, 0, 0),
        COLOR_CHARACTERISTICS[1].uuid: _encode(COLOR_CHARACTERISTICS[1], 0, 0, 0),
        COLOR_CHARACTERISTICS[2].uuid: _encode(COLOR_CHARACTERISTICS[2], 0, 0, 0),
        FOURTH_LEVEL_COLOR.uuid: OBSERVED_FOURTH_COLOR,
        TEMPERATURE_LEVEL_COLOR.uuid: OBSERVED_TEMPERATURE_COLOR,
        LEVEL_CONFIGURATION.uuid: OBSERVED_LEVEL_CONFIGURATION,
    }


SETTINGS_UUIDS = frozenset(spec.uuid for spec in SETTINGS_CHARACTERISTICS)


class FakeCharacteristic:
    """Minimal stand-in for BleakGATTCharacteristic."""

    def __init__(self, uuid: str, properties: list[str]) -> None:
        self.uuid = uuid
        self.properties = properties


class FakeServices:
    """Minimal stand-in for BleakGATTServiceCollection."""

    def __init__(self, present: set[str], writable: set[str] | None = None) -> None:
        self._present = present
        self._writable = writable if writable is not None else set(present)

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        if uuid not in self._present:
            return None
        properties = ["read"]
        if uuid in self._writable:
            properties.append("write")
        return FakeCharacteristic(uuid, properties)


class FakeBleakClient:
    """Fake bleak client backed by a payload map."""

    def __init__(
        self,
        payloads: dict[str, bytes],
        present: set[str] | None = None,
        errors: dict[str, Exception] | None = None,
        writable: set[str] | None = None,
        write_errors: dict[str, Exception] | None = None,
        stored_override: bytes | None = None,
        read_gate: asyncio.Event | None = None,
    ) -> None:
        self._payloads = payloads
        self._present = present if present is not None else set(payloads)
        self._errors = errors or {}
        self._writable = writable
        self._write_errors = write_errors or {}
        self._stored_override = stored_override
        self._read_gate = read_gate
        self.read_blocked = asyncio.Event()
        self.read_uuids: list[str] = []
        self.writes: list[tuple[str, bytes, bool | None]] = []
        self.disconnect_calls = 0

    @property
    def services(self) -> FakeServices:
        return FakeServices(self._present, self._writable)

    @staticmethod
    def _uuid_of(target: object) -> str:
        return getattr(target, "uuid", target)  # type: ignore[return-value]

    async def read_gatt_char(self, target: object) -> bytearray:
        uuid = self._uuid_of(target)
        if self._read_gate is not None:
            self.read_blocked.set()
            await self._read_gate.wait()
        self.read_uuids.append(uuid)
        if uuid in self._errors:
            raise self._errors[uuid]
        return bytearray(self._payloads[uuid])

    async def write_gatt_char(
        self, target: object, data: bytes, response: bool | None = None
    ) -> None:
        uuid = self._uuid_of(target)
        self.writes.append((uuid, bytes(data), response))
        if uuid in self._write_errors:
            raise self._write_errors[uuid]
        # The device echoes what was written unless a test forces a mismatch.
        self._payloads[uuid] = (
            self._stored_override if self._stored_override is not None else bytes(data)
        )

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def _fake_connection(client: FakeBleakClient):
    return patch(
        "custom_components.hai.protocol.establish_connection",
        AsyncMock(return_value=client),
    )


async def poll_with(
    client: FakeBleakClient,
    name: str | None = "haiS0123456",
    read_settings: bool = False,
    protocol_client: HaiProtocolClient | None = None,
):
    """Run a poll against a fake client."""
    ble_device = generate_ble_device(address=ADDRESS, name=name, details={})
    with _fake_connection(client):
        return await (protocol_client or HaiProtocolClient()).async_poll(
            ble_device, read_settings=read_settings
        )


async def write_with(
    client: FakeBleakClient,
    spec: protocol.CharacteristicSpec,
    raw_value: int,
    protocol_client: HaiProtocolClient | None = None,
) -> int:
    """Run a threshold write against a fake client."""
    ble_device = generate_ble_device(address=ADDRESS, name="haiS0123456", details={})
    with _fake_connection(client):
        return await (
            protocol_client or HaiProtocolClient()
        ).async_write_threshold(ble_device, spec, raw_value)


def test_xor_round_trip() -> None:
    """The transform is symmetric and matches the pinned sample."""
    assert xor_transform(xor_transform(LAST_SHOWER_PLAIN)) == LAST_SHOWER_PLAIN
    assert xor_transform(LAST_SHOWER_PLAIN) == LAST_SHOWER_ENCRYPTED
    assert xor_transform(LAST_SHOWER_ENCRYPTED) == LAST_SHOWER_PLAIN
    assert xor_transform(b"") == b""


def test_last_shower_fixture_decodes() -> None:
    """The known e622140a fixture decodes to the documented values."""
    record = decode_last_shower(xor_transform(LAST_SHOWER_ENCRYPTED))
    assert record.session_id == 30
    assert record.temperature_c == pytest.approx(31.73)
    assert record.duration_s == 756
    assert record.volume_ml == 106348
    assert record.start_time == datetime(2024, 2, 10, 1, 21, 40, tzinfo=UTC)
    assert record.initial_temperature_c == pytest.approx(30.15)


def test_last_shower_length_is_exact() -> None:
    """Truncated and padded payloads are both rejected."""
    with pytest.raises(HaiProtocolError):
        decode_last_shower(LAST_SHOWER_PLAIN[:-1])
    with pytest.raises(HaiProtocolError):
        decode_last_shower(LAST_SHOWER_PLAIN + b"\x00")


@pytest.mark.parametrize(
    ("raw", "formatted"),
    [(110, "1.10"), (105, "1.05"), (100, "1.00"), (203, "2.03"), (1, "0.01")],
)
def test_app_version_keeps_trailing_zeros(raw: int, formatted: str) -> None:
    """Versions never pass through float, so 1.10 stays 1.10."""
    assert format_app_version(raw) == formatted


def test_format_color() -> None:
    """Colours render as uppercase #RRGGBB."""
    assert format_color((0, 0, 0)) == "#000000"
    assert format_color((255, 32, 0)) == "#FF2000"
    assert format_color((1, 253, 3)) == "#01FD03"


def test_observed_samples_decode_under_the_shipped_hypothesis() -> None:
    """Pin what the real hardware bytes mean with encrypted=True.

    These are the values the encryption question turns on: read as plaintext
    the threshold is 67,249,597 mL (~67,000 L) and the temperature colour is a
    near-green, both of which are exactly one XOR key away from a sane value.
    """
    (threshold,) = struct.unpack(
        THIRD_LEVEL_THRESHOLD.fmt, xor_transform(OBSERVED_THIRD_THRESHOLD)
    )
    assert threshold == 75708
    assert int.from_bytes(OBSERVED_THIRD_THRESHOLD, "little", signed=True) == 67249597

    assert format_color(tuple(xor_transform(OBSERVED_TEMPERATURE_COLOR))) == "#00FF00"
    assert format_color(tuple(OBSERVED_TEMPERATURE_COLOR)) == "#01FD03"

    # The composite blob XOR-decodes to eight leading zero bytes, and its
    # offset-24 field to the same pure green as e6221509.
    decoded = xor_transform(OBSERVED_LEVEL_CONFIGURATION)
    assert decoded[:8] == bytes(8)
    assert format_color(tuple(decoded[24:27])) == "#00FF00"


async def test_signed_threshold_decodes_negative() -> None:
    """Thresholds are signed, so a saturated payload is -1, not 4 billion."""
    payloads = default_payloads()
    payloads[THIRD_LEVEL_THRESHOLD.uuid] = xor_transform(b"\xff\xff\xff\xff")
    client = FakeBleakClient(payloads)
    snapshot = await poll_with(client, read_settings=True)

    assert snapshot.settings is not None
    assert snapshot.settings.thresholds_raw["third_level_threshold"] == -1


async def test_full_poll_snapshot() -> None:
    """Every field of a fully featured device decodes and converts."""
    client = FakeBleakClient(default_payloads())
    snapshot = await poll_with(client)

    assert snapshot.address == ADDRESS
    assert snapshot.name == "haiS0123456"
    assert snapshot.product_id == "0A1B2C"
    assert snapshot.app_version == "1.10"
    assert snapshot.bootloader_version == "3"
    assert snapshot.session_id == 30
    assert snapshot.current_temperature_c == pytest.approx(31.73)
    assert snapshot.current_average_temperature_c == pytest.approx(31.50)
    assert snapshot.current_volume_ml == 12345
    assert snapshot.current_duration_s == 120
    # 100 mL/s -> 6.0 L/min
    assert snapshot.current_flow_rate_lpm == pytest.approx(6.0)
    assert snapshot.current_start_time == datetime(2024, 2, 10, 1, 21, 40, tzinfo=UTC)
    assert snapshot.lifetime_volume_ml == 106348
    assert snapshot.lifetime_average_temperature_c == pytest.approx(30.90)
    assert snapshot.battery_voltage_v == pytest.approx(3.537)
    assert snapshot.last_shower.duration_s == 756
    assert snapshot.supported_optional_keys == {
        "flow_rate",
        "current_start_time",
        "lifetime_average_temperature",
        "battery_voltage",
    }
    assert client.disconnect_calls == 1
    # Pins the read budget: settings must not creep into every poll.
    assert len(client.read_uuids) == POLL_READ_COUNT


async def test_poll_without_read_settings_skips_settings_reads() -> None:
    """Settings are not touched unless the coordinator asks for them."""
    client = FakeBleakClient(default_payloads())
    snapshot = await poll_with(client)

    assert snapshot.settings is None
    for uuid in SETTINGS_UUIDS:
        assert uuid not in client.read_uuids


async def test_poll_with_read_settings_decodes_thresholds_and_colors() -> None:
    """A settings read returns raw thresholds and #RRGGBB colours."""
    client = FakeBleakClient(default_payloads())
    snapshot = await poll_with(client, read_settings=True)

    settings = snapshot.settings
    assert settings is not None
    assert settings.thresholds_raw == {
        "first_level_threshold": 20000,
        "second_level_threshold": 40000,
        "third_level_threshold": 75708,
    }
    assert settings.led_colors == {
        "first_level_color": "#000000",
        "second_level_color": "#000000",
        "third_level_color": "#000000",
        "fourth_level_color": "#FF2000",
        "temperature_level_color": "#00FF00",
    }
    assert settings.supported_keys == frozenset(
        spec.key for spec in SETTINGS_CHARACTERISTICS
    )
    assert settings.writable_keys == settings.supported_keys
    # The composite blob is recorded raw and never decoded into a value.
    assert settings.raw_hex["level_configuration"] == (
        OBSERVED_LEVEL_CONFIGURATION.hex()
    )
    assert "level_configuration" not in settings.thresholds_raw
    assert "level_configuration" not in settings.led_colors
    assert len(client.read_uuids) == POLL_READ_COUNT + SETTINGS_READ_COUNT


async def test_settings_are_read_after_live_values() -> None:
    """Configuration never delays or displaces the live snapshot."""
    client = FakeBleakClient(default_payloads())
    await poll_with(client, read_settings=True)

    last_shower_index = client.read_uuids.index(LAST_SHOWER.uuid)
    first_settings_index = min(
        client.read_uuids.index(uuid) for uuid in SETTINGS_UUIDS
    )
    assert first_settings_index > last_shower_index


async def test_missing_settings_characteristics_are_omitted() -> None:
    """Firmware without the settings block still polls, reporting empty."""
    payloads = default_payloads()
    present = set(payloads) - SETTINGS_UUIDS
    client = FakeBleakClient(payloads, present=present)
    snapshot = await poll_with(client, read_settings=True)

    settings = snapshot.settings
    # Read, but nothing there: distinct from None, which means "not read".
    assert settings is not None
    assert settings.thresholds_raw == {}
    assert settings.led_colors == {}
    assert settings.supported_keys == frozenset()
    for uuid in SETTINGS_UUIDS:
        assert uuid not in client.read_uuids


async def test_read_only_settings_characteristic_is_not_writable() -> None:
    """Presence and writability are probed separately."""
    payloads = default_payloads()
    writable = set(payloads) - {FIRST_LEVEL_THRESHOLD.uuid}
    client = FakeBleakClient(payloads, writable=writable)
    snapshot = await poll_with(client, read_settings=True)

    settings = snapshot.settings
    assert settings is not None
    assert "first_level_threshold" in settings.supported_keys
    assert "first_level_threshold" not in settings.writable_keys
    assert "second_level_threshold" in settings.writable_keys


async def test_malformed_settings_payload_is_omitted() -> None:
    """One bad settings payload drops that key, not the whole block."""
    payloads = default_payloads()
    payloads[FIRST_LEVEL_COLOR.uuid] = b"\x01\x02"
    client = FakeBleakClient(payloads)
    snapshot = await poll_with(client, read_settings=True)

    settings = snapshot.settings
    assert settings is not None
    assert "first_level_color" not in settings.led_colors
    assert "first_level_color" not in settings.supported_keys
    assert settings.led_colors["temperature_level_color"] == "#00FF00"
    assert settings.thresholds_raw["first_level_threshold"] == 20000


async def test_settings_read_error_does_not_fail_poll() -> None:
    """A settings failure never discards a good live snapshot."""
    client = FakeBleakClient(
        default_payloads(),
        errors={FIRST_LEVEL_THRESHOLD.uuid: BleakError("settings boom")},
    )
    snapshot = await poll_with(client, read_settings=True)

    assert snapshot.settings is None
    assert snapshot.current_temperature_c == pytest.approx(31.73)
    assert snapshot.lifetime_volume_ml == 106348
    assert client.disconnect_calls == 1


async def test_session_zero_skips_current_reads() -> None:
    """No active session: current fields are None and never read."""
    payloads = default_payloads()
    payloads[SESSION_ID.uuid] = _encode(SESSION_ID, 0)
    client = FakeBleakClient(payloads)
    snapshot = await poll_with(client)

    assert snapshot.session_id == 0
    assert snapshot.current_temperature_c is None
    assert snapshot.current_average_temperature_c is None
    assert snapshot.current_volume_ml is None
    assert snapshot.current_duration_s is None
    assert snapshot.current_flow_rate_lpm is None
    assert snapshot.current_start_time is None
    # Retained data still read.
    assert snapshot.lifetime_volume_ml == 106348
    assert snapshot.last_shower.volume_ml == 106348
    for spec in (CURRENT_TEMPERATURE, CURRENT_VOLUME, CURRENT_DURATION, FLOW_RATE):
        assert spec.uuid not in client.read_uuids
    # Optional characteristics remain capability-confirmed by presence.
    assert "flow_rate" in snapshot.supported_optional_keys


async def test_missing_optional_characteristics_are_omitted() -> None:
    """A firmware without optional characteristics still polls cleanly."""
    payloads = default_payloads()
    optional_uuids = {
        FLOW_RATE.uuid,
        CURRENT_START_TIME.uuid,
        LIFETIME_AVERAGE_TEMPERATURE.uuid,
        BATTERY_VOLTAGE.uuid,
    }
    present = set(payloads) - optional_uuids
    client = FakeBleakClient(payloads, present=present)
    snapshot = await poll_with(client)

    assert snapshot.current_flow_rate_lpm is None
    assert snapshot.current_start_time is None
    assert snapshot.lifetime_average_temperature_c is None
    assert snapshot.battery_voltage_v is None
    assert snapshot.supported_optional_keys == frozenset()
    for uuid in optional_uuids:
        assert uuid not in client.read_uuids
    assert client.disconnect_calls == 1


async def test_missing_required_characteristic_fails_poll() -> None:
    """A missing required characteristic rejects the whole poll."""
    payloads = default_payloads()
    client = FakeBleakClient(payloads, present=set(payloads) - {LIFETIME_VOLUME.uuid})
    with pytest.raises(HaiProtocolError, match="lifetime_volume"):
        await poll_with(client)
    assert client.disconnect_calls == 1


async def test_malformed_required_characteristic_fails_poll() -> None:
    """A short required payload rejects the whole poll."""
    payloads = default_payloads()
    payloads[CURRENT_TEMPERATURE.uuid] = b"\x01"
    client = FakeBleakClient(payloads)
    with pytest.raises(HaiProtocolError, match="current_temperature"):
        await poll_with(client)
    assert client.disconnect_calls == 1


async def test_malformed_optional_characteristic_is_omitted() -> None:
    """A malformed optional payload is dropped, not fatal."""
    payloads = default_payloads()
    payloads[BATTERY_VOLTAGE.uuid] = b"\x01"
    client = FakeBleakClient(payloads)
    snapshot = await poll_with(client)
    assert snapshot.battery_voltage_v is None
    assert "battery_voltage" not in snapshot.supported_optional_keys
    assert client.disconnect_calls == 1


async def test_bleak_error_escapes_and_disconnects() -> None:
    """Connection-level read errors escape; disconnect is guaranteed."""
    client = FakeBleakClient(
        default_payloads(), errors={SESSION_ID.uuid: BleakError("boom")}
    )
    with pytest.raises(BleakError):
        await poll_with(client)
    assert client.disconnect_calls == 1


async def test_empty_product_id_fails_poll() -> None:
    """An empty product ID payload is malformed."""
    payloads = default_payloads()
    payloads[PRODUCT_ID.uuid] = b""
    client = FakeBleakClient(payloads)
    with pytest.raises(HaiProtocolError, match="product_id"):
        await poll_with(client)
    assert client.disconnect_calls == 1


async def test_zero_start_time_is_none() -> None:
    """A zero epoch start time decodes to None, not 1970."""
    payloads = default_payloads()
    payloads[CURRENT_START_TIME.uuid] = _encode(CURRENT_START_TIME, 0)
    client = FakeBleakClient(payloads)
    snapshot = await poll_with(client)
    assert snapshot.current_start_time is None
    assert "current_start_time" in snapshot.supported_optional_keys


async def test_name_falls_back_to_address() -> None:
    """A nameless advertisement falls back to the Bluetooth address."""
    client = FakeBleakClient(default_payloads())
    snapshot = await poll_with(client, name=None)
    assert snapshot.name == ADDRESS


async def test_write_threshold_verifies_read_back() -> None:
    """A write encodes, requires a response, and confirms by reading back."""
    client = FakeBleakClient(default_payloads())
    stored = await write_with(client, FIRST_LEVEL_THRESHOLD, 45000)

    assert stored == 45000
    assert len(client.writes) == 1
    uuid, payload, response = client.writes[0]
    assert uuid == FIRST_LEVEL_THRESHOLD.uuid
    assert payload == xor_transform(struct.pack(FIRST_LEVEL_THRESHOLD.fmt, 45000))
    assert response is True
    # The read-back happens after the write, on the same connection.
    assert client.read_uuids == [FIRST_LEVEL_THRESHOLD.uuid]
    assert client.disconnect_calls == 1


async def test_write_threshold_mismatch_raises() -> None:
    """A device that reports a different value fails verification."""
    client = FakeBleakClient(
        default_payloads(),
        stored_override=xor_transform(struct.pack(FIRST_LEVEL_THRESHOLD.fmt, 999)),
    )
    with pytest.raises(HaiWriteVerificationError, match="first_level_threshold"):
        await write_with(client, FIRST_LEVEL_THRESHOLD, 45000)
    assert client.disconnect_calls == 1


async def test_write_missing_characteristic_raises() -> None:
    """An absent characteristic is refused before anything is written."""
    payloads = default_payloads()
    present = set(payloads) - {FIRST_LEVEL_THRESHOLD.uuid}
    client = FakeBleakClient(payloads, present=present)
    with pytest.raises(HaiUnsupportedError, match="not present"):
        await write_with(client, FIRST_LEVEL_THRESHOLD, 45000)
    assert client.writes == []
    assert client.disconnect_calls == 1


async def test_write_readonly_characteristic_raises() -> None:
    """A read-only characteristic is refused before anything is written."""
    payloads = default_payloads()
    writable = set(payloads) - {FIRST_LEVEL_THRESHOLD.uuid}
    client = FakeBleakClient(payloads, writable=writable)
    with pytest.raises(HaiUnsupportedError, match="not writable"):
        await write_with(client, FIRST_LEVEL_THRESHOLD, 45000)
    assert client.writes == []
    assert client.disconnect_calls == 1


async def test_write_bleak_error_escapes_and_disconnects() -> None:
    """Transport errors escape unwrapped; disconnect still runs."""
    client = FakeBleakClient(
        default_payloads(),
        write_errors={FIRST_LEVEL_THRESHOLD.uuid: BleakError("write boom")},
    )
    with pytest.raises(BleakError):
        await write_with(client, FIRST_LEVEL_THRESHOLD, 45000)
    assert client.disconnect_calls == 1


async def test_write_serializes_with_poll() -> None:
    """One lock covers polls and writes, so they never interleave."""
    protocol_client = HaiProtocolClient()
    gate = asyncio.Event()
    poll_client = FakeBleakClient(default_payloads(), read_gate=gate)
    write_client = FakeBleakClient(default_payloads())
    ble_device = generate_ble_device(address=ADDRESS, name="haiS0123456", details={})
    connections = [poll_client, write_client]

    with patch(
        "custom_components.hai.protocol.establish_connection",
        AsyncMock(side_effect=lambda *args, **kwargs: connections.pop(0)),
    ):
        poll_task = asyncio.create_task(protocol_client.async_poll(ble_device))
        await asyncio.wait_for(poll_client.read_blocked.wait(), timeout=1)

        write_task = asyncio.create_task(
            protocol_client.async_write_threshold(
                ble_device, FIRST_LEVEL_THRESHOLD, 45000
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)

        # The poll holds the lock inside read_gatt_char, so the write is
        # queued: it has not even opened a connection yet.
        assert write_client.writes == []
        assert connections == [write_client]

        gate.set()
        await poll_task
        await write_task

    assert len(write_client.writes) == 1
