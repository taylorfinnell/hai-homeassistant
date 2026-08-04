"""Advertisement-driven polling coordinator for the Hai integration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_processor import (
    ActiveBluetoothProcessorCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MANUFACTURER, MODEL, WAKE_GENERATION_GAP_SECONDS
from .freshness import HaiFreshnessTracker
from .protocol import (
    COLOR_SPECS,
    THRESHOLD_SPECS,
    HaiProtocolClient,
    HaiProtocolError,
    HaiSettings,
    HaiSnapshot,
    HaiUnsupportedError,
    HaiWriteVerificationError,
)

_LOGGER = logging.getLogger(__name__)

type HaiConfigEntry = ConfigEntry[HaiCoordinator]


class HaiPollError(HomeAssistantError):
    """Raised when a poll cannot even start."""


class HaiUpdateSource(Enum):
    """Origin of a HaiUpdate."""

    ADVERTISEMENT = "advertisement"
    POLL = "poll"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class HaiUpdate:
    """The single typed update dispatched to every platform processor."""

    address: str
    name: str
    source: HaiUpdateSource
    wake_generation: int
    snapshot: HaiSnapshot | None  # None means advertisement-only or write-only
    settings: HaiSettings | None = None


def merge_settings(base: HaiSettings | None, incoming: HaiSettings) -> HaiSettings:
    """Overlay a partial settings read onto what is already known."""
    if base is None:
        return incoming
    return HaiSettings(
        thresholds_raw={**base.thresholds_raw, **incoming.thresholds_raw},
        led_colors={**base.led_colors, **incoming.led_colors},
        raw_hex={**base.raw_hex, **incoming.raw_hex},
        supported_keys=base.supported_keys | incoming.supported_keys,
        writable_keys=base.writable_keys | incoming.writable_keys,
    )


class HaiCoordinator(ActiveBluetoothProcessorCoordinator[HaiUpdate]):
    """Treat advertisements as wake signals; read GATT only while present.

    Advertisement handling performs no I/O. Polls are debounced by the
    framework (10 s default) and run only while a connectable path exists.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, address: str
    ) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.tracker = HaiFreshnessTracker(WAKE_GENERATION_GAP_SECONDS)
        self.client = HaiProtocolClient()
        self.last_snapshot: HaiSnapshot | None = None
        self.last_settings: HaiSettings | None = None
        self._settings_generation: int | None = None
        self._registry_metadata: tuple[str, str] | None = None
        super().__init__(
            hass,
            _LOGGER,
            address=address,
            mode=BluetoothScanningMode.PASSIVE,
            update_method=self._async_handle_advertisement,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_poll_device,
            connectable=False,
        )

    @property
    def settings_generation(self) -> int | None:
        """Wake generation whose poll last attempted a settings read."""
        return self._settings_generation

    @callback
    def device_info(self) -> DeviceInfo:
        """Return device info, enriched once polled metadata exists."""
        info = DeviceInfo(
            name=self.entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
        if (snapshot := self.last_snapshot) is not None:
            info["sw_version"] = snapshot.app_version
            info["model_id"] = snapshot.product_id
        return info

    @callback
    def _async_handle_advertisement(
        self, service_info: BluetoothServiceInfoBleak
    ) -> HaiUpdate:
        """Turn an advertisement into a metadata-only update. No I/O here."""
        generation = self.tracker.note_advertisement(service_info.time)
        return HaiUpdate(
            address=service_info.device.address,
            name=service_info.name or service_info.device.address,
            source=HaiUpdateSource.ADVERTISEMENT,
            wake_generation=generation,
            snapshot=None,
        )

    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        """Poll every advertisement burst while a connectable path exists.

        Cadence is paced by the framework's poll debouncer, not by a time
        condition here.
        """
        return (
            self.hass.state is CoreState.running
            and bluetooth.async_ble_device_from_address(
                self.hass, service_info.device.address, connectable=True
            )
            is not None
        )

    async def _async_poll_device(
        self, service_info: BluetoothServiceInfoBleak
    ) -> HaiUpdate:
        """Read a full snapshot; report failure to live entities; re-raise."""
        generation = self.tracker.generation
        address = self.address
        # Configuration only changes when a human changes it, so it is read on
        # the first poll of each wake generation rather than every 10 seconds.
        # Marked as attempted, not succeeded, so a firmware that errors on
        # these reads is not retried eight times per poll for a whole shower.
        read_settings = self._settings_generation != generation
        try:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, address, connectable=True
            )
            if ble_device is None:
                raise HaiPollError(
                    f"No connectable Bluetooth path to {address};"
                    " the device vanished between needs_poll and poll"
                )
            snapshot = await self.client.async_poll(
                ble_device, read_settings=read_settings
            )
        except Exception:
            # The framework records the failure but never notifies processor
            # entities; the tracker does, so live entities drop immediately.
            self.tracker.note_poll_failure(generation)
            raise
        finally:
            if read_settings:
                self._settings_generation = generation
            # The wake advertisement payload is static. Without clearing
            # history, the manager's change-detection guard would swallow the
            # next shower's identical advertisement and no poll would trigger.
            bluetooth.async_clear_advertisement_history(self.hass, address)
        self.last_snapshot = snapshot
        if snapshot.settings is not None:
            self.last_settings = merge_settings(self.last_settings, snapshot.settings)
        self.tracker.note_poll_success(generation)
        self._async_update_device_registry(snapshot)
        return HaiUpdate(
            address=address,
            name=snapshot.name,
            source=HaiUpdateSource.POLL,
            wake_generation=generation,
            snapshot=snapshot,
            settings=snapshot.settings,
        )

    def _connectable_device_or_raise(self) -> BLEDevice:
        """Resolve a connectable path, or fail with a readable error.

        A write is never queued for a later shower: a setting silently applied
        hours later is worse than a clear failure.
        """
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_asleep",
                translation_placeholders={"name": self.entry.title},
            )
        return ble_device

    def _translate_write_error(self, key: str, err: Exception) -> HomeAssistantError:
        """Turn a protocol-layer write failure into a user-facing error."""
        if isinstance(err, HaiWriteVerificationError):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_not_verified",
                translation_placeholders={
                    "name": self.entry.title,
                    "setting": key,
                    "error": str(err),
                },
            )
        if isinstance(err, HaiUnsupportedError):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setting_not_writable",
                translation_placeholders={"setting": key},
            )
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="write_failed",
            translation_placeholders={"name": self.entry.title, "error": str(err)},
        )

    def _after_write(self, settings: HaiSettings) -> HaiUpdate:
        """Cache a verified write and build the update to publish."""
        self.last_settings = merge_settings(self.last_settings, settings)
        # Re-read the whole block on the next poll so device-side clamping of
        # the values we did not write is picked up.
        self._settings_generation = None
        return HaiUpdate(
            address=self.address,
            name=self.entry.title,
            source=HaiUpdateSource.WRITE,
            wake_generation=self.tracker.generation,
            snapshot=None,
            settings=settings,
        )

    async def async_write_color(self, key: str, value: str) -> HaiUpdate:
        """Write one LED colour now and return the update to publish."""
        ble_device = self._connectable_device_or_raise()
        try:
            stored = await self.client.async_write_color(
                ble_device, COLOR_SPECS[key], value
            )
        except (HaiProtocolError, BleakError, TimeoutError, OSError) as err:
            raise self._translate_write_error(key, err) from err

        # A single-key update: the processor merges, so this is exactly right
        # and works even when nothing has been read yet.
        return self._after_write(
            HaiSettings(
                led_colors={key: stored},
                supported_keys=frozenset({key}),
                writable_keys=frozenset({key}),
            )
        )

    async def async_write_threshold(self, key: str, raw_value: int) -> HaiUpdate:
        """Write one threshold now and return the update to publish.

        Takes raw device units; the number entity owns the millilitre
        conversion.
        """
        ble_device = self._connectable_device_or_raise()
        try:
            stored = await self.client.async_write_threshold(
                ble_device, THRESHOLD_SPECS[key], raw_value
            )
        except (HaiProtocolError, BleakError, TimeoutError, OSError) as err:
            raise self._translate_write_error(key, err) from err

        settings = HaiSettings(
            thresholds_raw={key: stored},
            supported_keys=frozenset({key}),
            writable_keys=frozenset({key}),
        )
        update = self._after_write(settings)
        self._async_warn_if_unordered()
        return update

    @callback
    def _async_warn_if_unordered(self) -> None:
        """Warn when the three thresholds are not ascending.

        Ordering is not enforced: validating each value against its siblings
        would make "set third, then second, then first" impossible from the UI,
        and no evidence says the firmware requires monotonicity.
        """
        if self.last_settings is None:
            return
        values = [
            self.last_settings.thresholds_raw.get(key) for key in THRESHOLD_SPECS
        ]
        known = [value for value in values if value is not None]
        if len(known) == len(THRESHOLD_SPECS) and known != sorted(known):
            _LOGGER.warning(
                "Hai consumption thresholds are not ascending (%s); the device"
                " may treat the level bands as undefined",
                known,
            )

    @callback
    def _async_handle_unavailable(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Reset freshness before the framework notifies processors."""
        self.tracker.handle_unavailable()
        super()._async_handle_unavailable(service_info)

    @callback
    def _async_update_device_registry(self, snapshot: HaiSnapshot) -> None:
        """Apply polled metadata to the existing device registry entry.

        Processor entities copy DeviceInfo when constructed, so metadata that
        arrives with a later poll must be written to the registry explicitly.
        """
        metadata = (snapshot.app_version, snapshot.product_id)
        if metadata == self._registry_metadata:
            return
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(
            connections={(dr.CONNECTION_BLUETOOTH, self.address)}
        )
        if device is None:
            return
        device_registry.async_update_device(
            device.id,
            sw_version=snapshot.app_version,
            model_id=snapshot.product_id,
        )
        self._registry_metadata = metadata
