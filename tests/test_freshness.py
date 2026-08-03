"""Unit tests for the wake-generation tracker (no Home Assistant needed)."""

from __future__ import annotations

from custom_components.hai.freshness import HaiFreshnessTracker

GAP = 300.0


def make_tracker() -> HaiFreshnessTracker:
    """Build a tracker with the production gap."""
    return HaiFreshnessTracker(GAP)


def test_first_advertisement_starts_generation_one() -> None:
    """Generation 0 means "never woken", so the first wake is 1."""
    tracker = make_tracker()
    assert tracker.generation == 0
    assert tracker.note_advertisement(1000.0) == 1


def test_repeated_advertisements_keep_the_generation() -> None:
    """In-shower repeats, including cache replays, are the same wake."""
    tracker = make_tracker()
    tracker.note_advertisement(1000.0)
    assert tracker.note_advertisement(1000.0) == 1
    assert tracker.note_advertisement(1000.0 + GAP) == 1


def test_advertisement_past_the_gap_starts_a_new_generation() -> None:
    """A gap longer than a shower means a new shower."""
    tracker = make_tracker()
    tracker.note_advertisement(1000.0)
    assert tracker.note_advertisement(1000.0 + GAP + 0.1) == 2


def test_never_woken_is_never_fresh() -> None:
    """A restored-but-asleep device must not present live data as fresh."""
    tracker = make_tracker()
    tracker.note_poll_success(0)
    assert tracker.live_data_fresh is False


def test_success_marks_the_current_generation_fresh() -> None:
    """Freshness follows a completed poll, not the advertisement."""
    tracker = make_tracker()
    generation = tracker.note_advertisement(1000.0)
    assert tracker.live_data_fresh is False
    tracker.note_poll_success(generation)
    assert tracker.live_data_fresh is True


def test_stale_generation_success_is_ignored() -> None:
    """A slow poll from the previous shower cannot freshen this one."""
    tracker = make_tracker()
    old = tracker.note_advertisement(1000.0)
    tracker.note_advertisement(1000.0 + GAP + 1)

    tracker.note_poll_success(old)
    assert tracker.live_data_fresh is False


def test_failure_clears_freshness_and_notifies() -> None:
    """Failed polls dispatch nothing through the framework, so notify here."""
    tracker = make_tracker()
    calls: list[int] = []
    tracker.add_listener(lambda: calls.append(1))

    generation = tracker.note_advertisement(1000.0)
    tracker.note_poll_success(generation)
    tracker.note_poll_failure(generation)

    assert tracker.live_data_fresh is False
    assert len(calls) == 1


def test_stale_generation_failure_notifies_without_clearing() -> None:
    """A late failure from an old shower must not drop current live data."""
    tracker = make_tracker()
    old = tracker.note_advertisement(1000.0)
    current = tracker.note_advertisement(1000.0 + GAP + 1)
    tracker.note_poll_success(current)

    calls: list[int] = []
    tracker.add_listener(lambda: calls.append(1))
    tracker.note_poll_failure(old)

    assert tracker.live_data_fresh is True
    assert len(calls) == 1


def test_unavailable_forces_the_next_wake_to_be_new() -> None:
    """Clearing the timestamp guarantees the next advertisement advances."""
    tracker = make_tracker()
    generation = tracker.note_advertisement(1000.0)
    tracker.note_poll_success(generation)

    tracker.handle_unavailable()
    assert tracker.live_data_fresh is False

    # Well inside the gap, but the device went away, so this is a new wake.
    assert tracker.note_advertisement(1010.0) == 2


def test_listener_can_unsubscribe() -> None:
    """The returned callable detaches exactly one listener."""
    tracker = make_tracker()
    calls: list[int] = []
    remove = tracker.add_listener(lambda: calls.append(1))

    tracker.handle_unavailable()
    remove()
    tracker.handle_unavailable()

    assert len(calls) == 1


def test_listener_may_unsubscribe_during_notification() -> None:
    """Notification iterates a copy, so self-removal cannot skip a listener."""
    tracker = make_tracker()
    calls: list[str] = []

    def first() -> None:
        calls.append("first")
        remove_first()

    remove_first = tracker.add_listener(first)
    tracker.add_listener(lambda: calls.append("second"))

    tracker.handle_unavailable()
    assert calls == ["first", "second"]
