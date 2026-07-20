"""Constants for the Hai integration."""

from __future__ import annotations

DOMAIN = "hai"

MANUFACTURER = "hai"
MODEL = "Smart Showerhead"

# Advertised local-name prefix; must stay in sync with the manifest matcher.
LOCAL_NAME_PREFIX = "haiS"

# Seconds of advertisement silence after which the next advertisement is
# treated as a new wake burst (a new shower). Must stay comfortably longer
# than the in-shower advertisement/poll cycle, including a GATT connection
# during which the device may pause advertising. Hardware testing may tune it.
WAKE_GENERATION_GAP_SECONDS = 300.0


def advertisement_matches(local_name: str | None) -> bool:
    """Return True when an advertised local name identifies a Hai device.

    Standalone equivalent of the manifest's ``haiS*`` matcher, shared by the
    config flow's user step.
    """
    return local_name is not None and local_name.startswith(LOCAL_NAME_PREFIX)
