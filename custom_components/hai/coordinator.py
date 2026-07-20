"""Advertisement-driven polling coordinator for the Hai integration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging

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

from .const import MANUFACTURER, MODEL, WAKE_GENERATION_GAP_SECONDS
from .freshness import HaiFreshnessTracker
from .protocol import HaiProtocolClient, HaiSnapshot

_LOGGER = logging.getLogger(__name__)

type HaiConfigEntry = ConfigEntry[HaiCoordinator]


class HaiPollError(HomeAssistantError):
    """Raised when a poll cannot even start."""


class HaiUpdateSource(Enum):
    """Origin of a HaiUpdate."""

    ADVERTISEMENT = "advertisement"
    POLL = "poll"


@dataclass(frozen=True, slots=True)
class HaiUpdate:
    """The single typed update dispatched to every platform processor."""

    address: str
    name: str
    source: HaiUpdateSource
    wake_generation: int
    snapshot: HaiSnapshot | None  # None means advertisement-only


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
        try:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, address, connectable=True
            )
            if ble_device is None:
                raise HaiPollError(
                    f"No connectable Bluetooth path to {address};"
                    " the device vanished between needs_poll and poll"
                )
            snapshot = await self.client.async_poll(ble_device)
        except Exception:
            # The framework records the failure but never notifies processor
            # entities; the tracker does, so live entities drop immediately.
            self.tracker.note_poll_failure(generation)
            raise
        finally:
            # The wake advertisement payload is static. Without clearing
            # history, the manager's change-detection guard would swallow the
            # next shower's identical advertisement and no poll would trigger.
            bluetooth.async_clear_advertisement_history(self.hass, address)
        self.last_snapshot = snapshot
        self.tracker.note_poll_success(generation)
        self._async_update_device_registry(snapshot)
        return HaiUpdate(
            address=address,
            name=snapshot.name,
            source=HaiUpdateSource.POLL,
            wake_generation=generation,
            snapshot=snapshot,
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
