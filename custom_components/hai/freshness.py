"""Wake-generation freshness tracking for live entity availability."""

from __future__ import annotations

from collections.abc import Callable


class HaiFreshnessTracker:
    """Track advertisement wake bursts and whether live data is fresh.

    Home Assistant's processor framework merges updates without labeling an
    advertisement as the start of a new wake burst, and a failed poll does not
    notify processor entities. This tracker fills both gaps: it assigns a wake
    generation to every advertisement, records which generation has a fully
    successful poll, and notifies its own listeners on poll failure and on
    unavailability so live entities re-evaluate their availability.
    """

    def __init__(self, gap_seconds: float) -> None:
        self._gap_seconds = gap_seconds
        self._generation = 0
        self._fresh_generation: int | None = None
        self._last_advertisement_time: float | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def generation(self) -> int:
        """Wake generation of the most recent advertisement burst."""
        return self._generation

    @property
    def live_data_fresh(self) -> bool:
        """Return True when the current generation has a successful poll."""
        return self._generation != 0 and self._fresh_generation == self._generation

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to freshness resets; return an unsubscribe callable."""
        self._listeners.append(listener)

        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    def note_advertisement(self, monotonic_time: float) -> int:
        """Record an advertisement; advance the generation on a new wake burst.

        Repeated in-shower advertisements (including identical packets
        re-delivered after the advertisement cache is cleared) fall inside the
        gap and keep the current generation.
        """
        last = self._last_advertisement_time
        if last is None or (monotonic_time - last) > self._gap_seconds:
            self._generation += 1
        self._last_advertisement_time = monotonic_time
        return self._generation

    def note_poll_success(self, generation: int) -> None:
        """Mark a generation fresh if it is still the current one."""
        if generation == self._generation:
            self._fresh_generation = generation

    def note_poll_failure(self, generation: int) -> None:
        """Mark the failed generation stale and notify subscribed entities."""
        if generation == self._generation:
            self._fresh_generation = None
        self._notify()

    def handle_unavailable(self) -> None:
        """Reset freshness when the device stops advertising."""
        self._fresh_generation = None
        self._last_advertisement_time = None
        self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
