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

### Upgrading from v1

There is no automatic upgrade path. v2 is a rewrite: entity unique IDs, units,
and stored state all changed, and no migration is provided. Remove the old
integration and add it again.

1. In **Settings → Devices & services**, open the existing **hai** entry and
   delete it. This removes its device and all of its entities.
2. Install v2 using one of the methods above and restart Home Assistant.
3. Run the shower so the head advertises, then add the integration again (see
   **First discovery** below).

Recorded history for the v1 entities is not carried over, and any automations,
scripts, or dashboard cards that referenced them need repointing at the new
entities. Export anything you want to keep before deleting the integration.

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
| Level 1–4 colour, temperature colour | `#RRGGBB` | Retained | Diagnostic, read-only |
| First/second/third level threshold | L | Config | Writable, **disabled by default** — see below |

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

### Device settings (experimental)

The shower head stores three consumption thresholds and five LED colours. They
are read once per shower rather than on every poll, so they cost nothing during
normal operation.

The colours are exposed read-only. The three thresholds are writable `number`
entities but **ship disabled**, because how the device encodes this block is not
yet confirmed. The published protocol notes say these characteristics are
unencrypted; the observed bytes say they are XOR-encrypted like the telemetry,
and this integration follows the bytes. Verify before enabling:

1. Download diagnostics from the device page and find the `settings` block. Each
   characteristic is listed with its raw bytes and both candidate decodings.
2. Compare `led_colors` and `thresholds_raw` against what the hai app shows.
3. If they match, enable the threshold entities in the entity settings.

This check cannot be skipped or automated: writes are confirmed by reading the
value back, and because the XOR transform is symmetric, a read-back succeeds
even when the encoding is wrong. Only the app can tell you what the device
actually holds. A threshold that decodes to an impossible volume shows as
unavailable, which also blocks writing to it.

Writes need a live Bluetooth connection, so they only work while water is
running. Setting a threshold while the shower head is asleep fails immediately
rather than queueing — a threshold that silently applied hours later would be
worse than a clear error. Nothing in this integration can erase your shower
history or factory-reset the device; those characteristics are deliberately not
implemented.

## Troubleshooting

- **Everything unavailable and the device never appears:** the shower head
  only talks while water is running. Run the shower and watch
  **Settings → Devices & services**.
- **"Missing required characteristics" in the log:** the shower head is on
  factory firmware. Pair it once with the official hai app to update.
- **Upgraded from v1 and old entities linger or everything is unavailable:**
  v2 does not migrate v1 config entries. Delete the old integration entry and
  add it again — see **Upgrading from v1**.
- **A threshold is unavailable:** its bytes decoded to a volume no shower could
  use, which usually means the encoding assumption is wrong for your firmware.
  Check the `settings` block in diagnostics.
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
