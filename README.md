# Hai Smart Shower Head for Home Assistant

A custom [Home Assistant](https://www.home-assistant.io) integration for the
[hai smart showerhead](https://gethai.com/products/hai-showerhead). It listens
for the shower head's Bluetooth wake advertisements and reads shower data over
a local GATT connection — no cloud, no app account.

## How it works

The shower head sleeps between showers and only advertises while water is
running. This integration treats those advertisements as wake signals:

1. Water runs → the shower head advertises → Home Assistant wakes the
   integration.
2. The integration connects over BLE (directly or through an ESPHome
   Bluetooth proxy) and reads a full snapshot roughly every 10 seconds while
   the shower is active.
3. When the shower ends, the device goes back to sleep. Live values become
   unavailable; lifetime and last-shower values stay available from cache,
   including across Home Assistant restarts.

## Requirements

- Home Assistant **2026.7.2 or newer** (the version this release is tested
  against).
- A Bluetooth adapter usable by Home Assistant, or an
  [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy/)
  with active connections enabled, in range of the shower.
- A shower head that has been paired **at least once with the official hai
  app** so it runs current firmware. Factory firmware exposes a different
  GATT layout and is not supported; polls against it fail with a "missing
  required characteristics" error in the logs.

Firmware support is based on the GATT layout observed on app-updated
firmware ([protocol notes](https://gist.github.com/taylorfinnell/87c79939a63ec2cb607ed2ebe28db5ce)).
Optional characteristics (flow rate, battery voltage, lifetime average
temperature) are probed per device, and their entities appear only when the
firmware actually exposes them.

## Installation

### HACS (custom repository)

1. In HACS, open **⋮ → Custom repositories**.
2. Add `https://github.com/taylorfinnell/hai-homeassistant` with category
   **Integration**.
3. Install **Hai Smart Shower Head** and restart Home Assistant.

### Manual

1. Copy `custom_components/hai/` from this repository into
   `<config>/custom_components/hai/`.
2. Restart Home Assistant.

### First discovery

Run the shower for a little while. The shower head only advertises with
water running, so discovery can take a minute; once seen, Home Assistant
shows a discovered **hai** device under **Settings → Devices & services**.
You can also add it manually via **Add integration → hai** while the water
is running.

## Entities

| Entity | Unit | Kind | Notes |
|---|---|---|---|
| Current shower temperature | °C | Live | |
| Current shower average temperature | °C | Live | Aggregate; no statistics |
| Current shower volume | mL | Live | Resets per shower |
| Current shower duration | s | Live | |
| Flow rate | L/min | Live | Only on firmware that exposes it |
| Lifetime volume | L | Retained | Long-term statistics deliberately off until the counter's reset behavior is verified on hardware |
| Lifetime average temperature | °C | Retained | Only on firmware that exposes it |
| Last shower temperature | °C | Retained | |
| Last shower duration | s | Retained | |
| Last shower volume | mL | Retained | |
| Battery voltage | V | Retained | Diagnostic, disabled by default |
| Shower active | on/off | Activity | See below |

**Live** entities have values only while a shower is running *and* the
integration has completed a fresh read for that shower. Between showers they
are unavailable — that is by design, not a bug.

**Retained** entities keep their last value while the shower head sleeps and
across Home Assistant restarts. No `input_number` persistence workaround is
needed anymore.

**Shower active** turns on with the first wake advertisement. It turns off
when Home Assistant declares the device's advertisements stale, which can
take several minutes after the water stops. Treat it as a convenience
trigger for automations, never as a safety signal.

The Water dashboard needs a water sensor with long-term statistics; the
lifetime volume entity will opt in once its rollover/factory-reset behavior
has been confirmed on hardware.

## Troubleshooting

- **Everything unavailable and the device never appears:** the shower head
  only talks while water is running. Run the shower and watch
  **Settings → Devices & services**.
- **"Missing required characteristics" in the log:** the shower head is on
  factory firmware. Pair it once with the official hai app to update.
- Download diagnostics from the device page to see the last full snapshot,
  firmware versions (app and bootloader), and polling state.

## Development

```bash
pip install -r requirements_test.txt
pytest tests/
ruff check .
```

Protocol details live in `custom_components/hai/protocol.py` behind a typed
snapshot; its tests run without Home Assistant. The
[PoC script and GATT notes](https://gist.github.com/taylorfinnell/87c79939a63ec2cb607ed2ebe28db5ce)
document the observed characteristic map.

## Thanks

Thanks to [@adizanni's Hydrao integration](https://github.com/adizanni/hydrao)
for the original inspiration for v1 of this integration.
