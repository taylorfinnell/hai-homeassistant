"""Writable LED colours for the Hai integration.

The shower head lights one of four colours by how much water the running
shower has used, plus a fifth for temperature. They are exposed as text
entities holding ``#RRGGBB`` rather than as ``light`` entities: Home Assistant
couples brightness to every RGB colour mode, so a light would carry a
brightness slider that either does nothing or silently rescales the stored
colour, and five entities in the light domain would be caught by any
"turn off all the lights" automation -- which here would write #000000 over
the user's configuration.
"""

from __future__ import annotations

from typing import cast

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import HaiConfigEntry, HaiCoordinator, HaiUpdate
from .protocol import COLOR_PATTERN, HaiProtocolError, parse_color

# Writes are user-initiated and hit the radio. Unlike the read-only platforms
# this must not be 0, which would remove the platform semaphore entirely.
PARALLEL_UPDATES = 1

COLOR_KEYS: tuple[str, ...] = (
    "first_level_color",
    "second_level_color",
    "third_level_color",
    "fourth_level_color",
    "temperature_level_color",
)

TEXT_DESCRIPTIONS: dict[str, TextEntityDescription] = {
    key: TextEntityDescription(
        key=key,
        translation_key=key,
        icon="mdi:palette",
        entity_category=EntityCategory.CONFIG,
        mode=TextMode.TEXT,
        native_min=7,
        native_max=7,
        pattern=COLOR_PATTERN,
    )
    for key in COLOR_KEYS
}


def _entity_key(key: str) -> PassiveBluetoothEntityKey:
    return PassiveBluetoothEntityKey(key=key, device_id=None)


def text_update_to_bluetooth_data_update(
    coordinator: HaiCoordinator, update: HaiUpdate
) -> PassiveBluetoothDataUpdate[str | None]:
    """Convert a HaiUpdate into a processor update for the text platform.

    Settings are omitted entirely (rather than published as None) whenever they
    were not read, so the processor's per-key merge retains the cached colour.
    """
    devices = {None: coordinator.device_info()}
    descriptions: dict[PassiveBluetoothEntityKey, TextEntityDescription] = {}
    data: dict[PassiveBluetoothEntityKey, str | None] = {}

    if (settings := update.settings) is not None:
        for key in COLOR_KEYS:
            if key in settings.led_colors and key in settings.writable_keys:
                descriptions[_entity_key(key)] = TEXT_DESCRIPTIONS[key]
                data[_entity_key(key)] = settings.led_colors[key]

    return PassiveBluetoothDataUpdate(
        devices=devices,
        entity_descriptions=descriptions,
        entity_data=data,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Hai LED colour texts."""
    coordinator = entry.runtime_data
    processor: PassiveBluetoothDataProcessor[str | None, HaiUpdate] = (
        PassiveBluetoothDataProcessor(
            lambda update: text_update_to_bluetooth_data_update(coordinator, update)
        )
    )
    entry.async_on_unload(
        processor.async_add_entities_listener(HaiColorText, async_add_entities)
    )
    # Registration dispatches restored data immediately, so the entities
    # listener above must already be attached.
    entry.async_on_unload(
        coordinator.async_register_processor(processor, TextEntityDescription)
    )


class HaiColorText(
    PassiveBluetoothProcessorEntity[
        PassiveBluetoothDataProcessor[str | None, HaiUpdate]
    ],
    TextEntity,
):
    """One LED colour as a #RRGGBB string."""

    @property
    def _coordinator(self) -> HaiCoordinator:
        return cast(HaiCoordinator, self.processor.coordinator)

    @property
    def native_value(self) -> str | None:
        """Return the cached colour for this key."""
        return self.processor.entity_data.get(self.entity_key)

    @property
    def available(self) -> bool:
        """Available once a colour is known, awake or asleep.

        Deliberately not gated on Bluetooth presence: Home Assistant's entity
        service dispatch drops unavailable entities before the platform is
        called, so a presence-gated entity would make text.set_value a silent
        no-op for the ~99% of the time the shower head is asleep, instead of
        raising the "device is asleep" error.
        """
        return self.native_value is not None

    async def async_set_value(self, value: str) -> None:
        """Write the colour and publish only the verified read-back."""
        try:
            parse_color(value)
        except HaiProtocolError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_color",
                translation_placeholders={"value": value},
            ) from err

        update = await self._coordinator.async_write_color(self.entity_key.key, value)
        # Publish through the processor so the verified value reaches restore
        # storage, not just this entity's state.
        self.processor.async_handle_update(update)
        # Per-key dispatch is filtered to changed values, so rewriting the
        # colour it already had would otherwise never write state.
        self.async_write_ha_state()
