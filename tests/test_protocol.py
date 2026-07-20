"""Unit tests for the Hai BLE protocol module (no Home Assistant needed)."""

from __future__ import annotations

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
    CURRENT_DURATION,
    CURRENT_START_TIME,
    CURRENT_TEMPERATURE,
    CURRENT_VOLUME,
    FLOW_RATE,
    LAST_SHOWER,
    LIFETIME_AVERAGE_TEMPERATURE,
    LIFETIME_VOLUME,
    PRODUCT_ID,
    SESSION_ID,
    HaiProtocolClient,
    HaiProtocolError,
    decode_last_shower,
    format_app_version,
    xor_transform,
)

from .bluetooth import generate_ble_device

ADDRESS = "AA:BB:CC:DD:EE:FF"

# struct.pack("<IHHIIH", 30, 3173, 756, 106348, 1707528100, 3015) XORed with
# the key; decodes to session 30, 31.73 degC, 756 s, 106348 mL,
# 2024-02-10T01:21:40Z, initial temperature 30.15 degC.
LAST_SHOWER_ENCRYPTED = bytes.fromhex("1f020304600af5006f9b0406a5cdc561c20d")
LAST_SHOWER_PLAIN = bytes.fromhex("1e000000650cf4026c9f0100a4cfc665c70b")


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
    }


class FakeServices:
    """Minimal stand-in for BleakGATTServiceCollection."""

    def __init__(self, present: set[str]) -> None:
        self._present = present

    def get_characteristic(self, uuid: str) -> object | None:
        return object() if uuid in self._present else None


class FakeBleakClient:
    """Fake bleak client backed by a payload map."""

    def __init__(
        self,
        payloads: dict[str, bytes],
        present: set[str] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._payloads = payloads
        self._present = present if present is not None else set(payloads)
        self._errors = errors or {}
        self.read_uuids: list[str] = []
        self.disconnect_calls = 0

    @property
    def services(self) -> FakeServices:
        return FakeServices(self._present)

    async def read_gatt_char(self, uuid: str) -> bytearray:
        self.read_uuids.append(uuid)
        if uuid in self._errors:
            raise self._errors[uuid]
        return bytearray(self._payloads[uuid])

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


async def poll_with(client: FakeBleakClient, name: str | None = "haiS0123456"):
    """Run a poll against a fake client."""
    ble_device = generate_ble_device(address=ADDRESS, name=name, details={})
    with patch(
        "custom_components.hai.protocol.establish_connection",
        AsyncMock(return_value=client),
    ):
        return await HaiProtocolClient().async_poll(ble_device)


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
