# Samsung AC Local Control - Project Notes

Last updated: 2026-05-22 AWST

## Goal

Provide local-only web control for older Samsung air conditioners whose original Smart
Air Conditioner / CAC Android app is no longer supported on newer devices.

The target class of systems uses a Samsung WiFi adapter on the LAN and speaks an older
Samsung TLS/XML protocol on TCP port `2878`.

## Current State

- Python/Flask web app with a mobile-friendly control UI.
- Local network discovery for adapters listening on port `2878`.
- Pairing helper for adapters that require an auth token.
- Tested working controls on a ducted reverse-cycle system:
  - Power on/off
  - Target temperature up/down
  - Mode selection
  - Fan speed low/mid/high/auto
- App-side schedules persist across service/app restarts in `schedules.yaml`.
- App-side one-shot on/off timers persist while pending.
- One pending on-timer and one pending off-timer can run at the same time.
- The app remembers the last connected AC IP and can reconnect automatically or via
  the setup screen after startup/network interruptions.
- Power-on schedules can optionally set operation mode and target temperature.
- Fan-mode power-on schedules hide/ignore temperature because setpoint is not relevant.
- A faint UI build stamp helps confirm which version is deployed.
- Missed schedule/timer events are intentionally not replayed after reboot or downtime.

## Hardware Notes

- This was developed against an older Samsung ducted system using a MIM-H02-style WiFi adapter.
- Swing/louver controls may not be useful on ducted systems.
- Turbo fan mode may not be supported or useful on every system.
- Some adapters have both `AP` and `WPS` buttons. Pairing may require the physical `AP` button.

## Important Files

- `samsung_ac/protocol.py`: Samsung TLS/XML protocol implementation.
- `samsung_ac/web_app.py`: Flask app and REST API.
- `samsung_ac/templates/index.html`: Mobile web UI.
- `samsung_ac/static/style.css`: UI styling.
- `samsung_ac/discovery.py`: LAN discovery for devices listening on port `2878`.
- `samsung_ac/scheduler.py`: App-side schedules and one-shot timers.
- `pair.py`: One-time token pairing helper.
- `config.example.yaml`: Public example configuration.
- `config.yaml`: Private local configuration, ignored by Git.
- `schedules.yaml`: App-side schedule/timer persistence file, ignored by Git.
- `requirements.txt`: Python dependencies.

## Protocol Notes

- Modern OpenSSL rejects the adapter's old TLS parameters unless weak legacy ciphers are explicitly enabled.
- `protocol.py` uses RSA ciphers compatible with tested older adapters:
  - `AES128-SHA`
  - `AES256-SHA`
  - `DES-CBC3-SHA`
  - OpenSSL security level `0`
- Some adapters report room temperature in Fahrenheit-like values even while the setpoint is Celsius; values above `60` are converted to Celsius for display.
- Some adapters report no-error as `30303030`, which is hex/ASCII-style `0000`; this is normalized to error `0`.
- Native AC timer attributes (`AC_FUN_ONTIMER`/`AC_FUN_OFFTIMER`) were not reliable on the tested ducted unit, so the UI uses app-side one-shot timers that send normal power commands when due.

## Pairing Note

Some adapters return the token as:

```xml
<Update Type="GetToken" Status="Completed" Token="..."/>
```

The pairing helper accepts `Status="Completed"` as a successful token response.

## Deployment Notes

Recommended always-on deployment:

- Python virtual environment
- `systemd` service based on `samsung-ac.service.example`
- Web UI bound to the local LAN only
- DHCP reservation for the AC WiFi adapter if possible

The web UI has no built-in authentication. Do not expose it directly to the internet.
