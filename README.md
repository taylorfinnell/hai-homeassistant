# Hai Smart Shower Head

This custom component for [Home Assistant](https://www.home-assistant.io) is supporting active monitoring of the [Hai](https://gethai.com) [shower head](https://gethai.com/products/hai-showerhead) via BLE.

## Additional Info

The integration should work out of the box. In order to discover the hydrao device, you should activate it with running water until the integration is fully recognized by the config flow.
It works most of the times.

You will see the Average and Current Temperature of the water, the current and total volume of the consumed water (across all showers) and the duration of the shower.

The device is disconnecting by design, so the value of the sensors will be valid during a running shower, after the entities will become unavailable. You should persist the value of the sensors using input_numbers and automation to set the values (excluding unavailable states).

I only tested the integration with Spa model and only having one at home (do not know what happens if you have more than 1).

The integration is very experimental so use it in a test environment and only when you feel confident move it to your prod environment.

## Thanks

Huge thanks to [@adizanni](https://github.com/adizanni/hydrao/tree/main) for his Integration for Hydrao. This is basically a copy and paste with small
updates to handle the encoding of the Hai packets.
