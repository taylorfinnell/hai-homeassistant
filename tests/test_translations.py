"""Every translation key the code can emit must resolve in en.json.

Custom integrations get no build-time placeholder expansion, and a missing
key degrades silently: Home Assistant renders the raw key string to the user
instead of raising. Nothing else in the suite would notice.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from custom_components.hai import binary_sensor, coordinator, number, sensor

TRANSLATIONS = json.loads(
    (
        Path(__file__).parent.parent
        / "custom_components"
        / "hai"
        / "translations"
        / "en.json"
    ).read_text(encoding="utf-8")
)

# Modules that raise translated exceptions rather than naming entities.
EXCEPTION_SOURCES = (coordinator, number)

_TRANSLATION_KEY_PATTERN = re.compile(r'translation_key="([a-z0-9_]+)"')


def entity_names(platform: str) -> dict:
    """Return the name translations for one platform."""
    return TRANSLATIONS["entity"][platform]


def test_sensor_translation_keys_resolve() -> None:
    """Every sensor description names a real translation."""
    names = entity_names("sensor")
    for key, description in sensor.SENSOR_DESCRIPTIONS.items():
        assert description.translation_key == key, key
        assert names[key]["name"], key


def test_number_translation_keys_resolve() -> None:
    """Every threshold description names a real translation."""
    names = entity_names("number")
    for key, description in number.NUMBER_DESCRIPTIONS.items():
        assert description.translation_key == key, key
        assert names[key]["name"], key


def test_binary_sensor_translation_key_resolves() -> None:
    """The activity sensor names a real translation."""
    description = binary_sensor.SHOWER_ACTIVE_DESCRIPTION
    assert entity_names("binary_sensor")[description.translation_key]["name"]


def test_exception_translation_keys_resolve() -> None:
    """Every translated error the code can raise has a message.

    Scanning the source rather than a hand-maintained list means a new
    HomeAssistantError with a new key fails here until it is translated.
    """
    messages = TRANSLATIONS["exceptions"]
    found: set[str] = set()
    for module in EXCEPTION_SOURCES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        found.update(_TRANSLATION_KEY_PATTERN.findall(source))

    assert found, "no translation keys found; did the pattern stop matching?"
    for key in found:
        assert key in messages, key
        assert messages[key]["message"], key


def test_no_unused_exception_messages() -> None:
    """Translations for errors that no longer exist are dead weight."""
    used: set[str] = set()
    for module in EXCEPTION_SOURCES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        used.update(_TRANSLATION_KEY_PATTERN.findall(source))

    assert set(TRANSLATIONS["exceptions"]) == used


def test_config_flow_aborts_are_translated() -> None:
    """Discovery can abort for reasons the user has to be able to read."""
    aborts = TRANSLATIONS["config"]["abort"]
    for reason in (
        "already_configured",
        "already_in_progress",
        "no_devices_found",
        "not_supported",
    ):
        assert aborts[reason], reason
