"""Diagnostics support for the Hai integration."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import HaiConfigEntry
from .protocol import THRESHOLD_SPECS, HaiSettings, format_color, xor_transform

# "name" is redacted because it falls back to the Bluetooth address whenever
# the connectable BLEDevice has no advertised name, which is common.
TO_REDACT = {"address", "name"}


def _json_safe(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, frozenset):
            result[key] = sorted(value)
        else:
            result[key] = value
    return result


def _decode_candidates(key: str, raw_hex: str) -> dict[str, Any]:
    """Show a settings characteristic decoded both ways.

    Whether the e62215xx block is XOR-encrypted is unresolved, and read-back
    verification cannot settle it because XOR is symmetric. This puts both
    candidate readings side by side so one comparison against the hai app
    closes the question without another hardware session.
    """
    raw = bytes.fromhex(raw_hex)
    decrypted = xor_transform(raw)
    candidates: dict[str, Any] = {
        "raw": raw_hex,
        "xor_decoded": decrypted.hex(),
    }
    if key in THRESHOLD_SPECS and len(raw) == 4:
        candidates["as_plaintext_int"] = int.from_bytes(raw, "little", signed=True)
        candidates["as_xor_int"] = int.from_bytes(decrypted, "little", signed=True)
    elif len(raw) == 3:
        candidates["as_plaintext_color"] = format_color(tuple(raw))
        candidates["as_xor_color"] = format_color(tuple(decrypted))
    return candidates


def _settings_diagnostics(settings: HaiSettings | None) -> dict[str, Any] | None:
    if settings is None:
        return None
    return {
        "thresholds_raw": settings.thresholds_raw,
        "led_colors": settings.led_colors,
        "supported_keys": sorted(settings.supported_keys),
        "writable_keys": sorted(settings.writable_keys),
        "characteristics": {
            key: _decode_candidates(key, raw_hex)
            for key, raw_hex in sorted(settings.raw_hex.items())
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HaiConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    snapshot = coordinator.last_snapshot
    data = {
        "entry": {
            "title": entry.title,
            "address": entry.unique_id,
            "version": entry.version,
            "minor_version": entry.minor_version,
        },
        "coordinator": {
            "available": coordinator.available,
            "last_poll_successful": coordinator.last_poll_successful,
            "wake_generation": coordinator.tracker.generation,
            "live_data_fresh": coordinator.tracker.live_data_fresh,
            "settings_generation": coordinator.settings_generation,
        },
        # The bootloader version lives here on purpose: DeviceInfo has no
        # bootloader field and it must not masquerade as hw_version.
        "snapshot": asdict(snapshot, dict_factory=_json_safe) if snapshot else None,
        "settings": _settings_diagnostics(coordinator.last_settings),
    }
    return async_redact_data(data, TO_REDACT)
