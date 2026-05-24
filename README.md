# Samsung AC Local WiFi Control

Local WiFi control for Samsung ducted air conditioners with WiFi adapters (MIM-H02 and similar).
Provides a mobile-friendly web interface for older Samsung Smart Air Conditioner / CAC WiFi
adapters whose original Android app is no longer supported on newer devices.

This project is unofficial and is not affiliated with Samsung.

![Samsung AC Local WiFi Control mobile UI](docs/screenshot.png)

## Features

- Power on/off, mode (Auto/Cool/Heat/Dry/Fan), temperature, fan speed, swing direction
- App-side one-shot timers (turn on/off in N minutes)
- App-side scheduling (e.g. "turn off at 2am on weeknights", or turn on with
  a chosen mode and temperature)
- App-side schedules persist across app/server restarts via `schedules.yaml`
- Usage-session logging to CSV with mode, temperature, room temperature, fan speed,
  and mid-session change summaries
- Reconnects to the saved AC IP after service restarts/network interruptions, with
  a manual "Reconnect to last IP" option in the setup screen
- Faint build stamp in the UI so deployed updates can be confirmed at a glance
- Network discovery to find your AC automatically
- Mobile-friendly responsive UI - works great on phones
- No cloud, no login, no internet required - 100% local

## Quick Start

```bash
# Clone the repo, then enter the project directory
git clone https://github.com/ChrisK-au/samsung-ac-local-control.git
cd samsung-ac-local-control

# Install dependencies
pip install -r requirements.txt

# Copy and edit the local configuration
cp config.example.yaml config.yaml

# Run the app
python -m samsung_ac

# Or with verbose logging
python -m samsung_ac -v
```

Then open http://localhost:8080 in your browser (or from your phone: http://YOUR_SERVER_IP:8080).

## First-Time Pairing

Some Samsung WiFi adapters require a pairing token before they will accept control commands.
If your AC does not respond after discovery or manual IP entry, pair it once:

1. Find the AC WiFi adapter's IP address. You can use the web UI "Scan Network" button,
   check your router's DHCP client list, or run `python -m samsung_ac.discovery`.
2. Run the pairing helper:

   ```bash
   python pair.py 192.168.1.xxx
   ```

3. When the script says the AC is ready for pairing, press and hold the physical `AP`
   button on the Samsung WiFi adapter for about 5 seconds. On MIM-H02-style adapters,
   this button is on the front of the small WiFi module near the LED indicators.
4. Wait for the script to receive a token. It saves the token and AC IP to `config.yaml`.
5. Start the app:

   ```bash
   python -m samsung_ac
   ```

If your adapter has both `AP` and `WPS` buttons, use the `AP` button for pairing.
The token is private; do not publish your `config.yaml`.

## Configuration

Copy `config.example.yaml` to `config.yaml`, then edit it:

```yaml
ac_host: "192.168.1.xxx"  # Your AC WiFi adapter's IP
last_ac_host: ""           # Managed automatically after successful connection
ac_port: 2878
token: ""
web_port: 8080
```

If you don't know the AC's IP address, leave `ac_host` empty and use the "Scan Network" 
button in the web UI to discover it automatically.

App-side schedules and one-shot timers are saved in `schedules.yaml` beside `config.yaml`.
One pending on-timer and one pending off-timer can run at the same time; setting a new
on-timer only replaces the existing on-timer, and setting a new off-timer only replaces
the existing off-timer. They survive app/server restarts, but missed events are not
replayed if the server is off at the scheduled time.

When an AC IP is saved in `ac_host` or `last_ac_host`, the app will try to reconnect
automatically after startup or a temporary network failure. If automatic reconnect has
not completed yet, the setup screen shows a button to reconnect to the last saved IP.

Power-on schedules can optionally set mode and target temperature. If Fan mode is
selected, the temperature field is hidden in the UI and ignored by the scheduler.

Usage sessions are saved in `usage_log.csv` beside `config.yaml`. A session starts
when the app observes the AC is on and is written as one CSV row when the app observes
the AC has turned off. The log includes start/end mode, target temperature, room
temperature, fan speed, values used during the session, and a change count. The file is
capped to 500 total lines including the header and can be downloaded from the subtle
"Download log" link at the bottom of the web UI.

## Finding Your AC's IP Address

The AC WiFi adapter should be connected to your home WiFi network. You can find its IP by:

1. Using the built-in network scanner in the web UI
2. Checking your router's DHCP client list
3. Running: `python -m samsung_ac.discovery`

## Supported Models

This works with Samsung HVAC units that use the MIM-H02 (or similar) WiFi adapter, 
communicating via TLS on port 2878. This includes many Samsung ducted and split systems 
from the 2013-2020 era.

Tested with an older Samsung ducted reverse-cycle system using a MIM-H02-style WiFi adapter.

## How It Works

The Samsung WiFi adapter exposes a TLS server on port 2878 that accepts XML commands.
This app connects directly to that adapter on your local network - no Samsung cloud 
services needed.

The adapter uses legacy TLS settings that modern OpenSSL rejects by default. This project
explicitly enables the older cipher settings required by these adapters.

## Security Notes

- The web UI has no built-in authentication.
- Run it only on a trusted local network.
- Do not expose the web port directly to the internet.
- Keep `config.yaml` private because it may contain your AC pairing token.

## Running as a Service

To run on boot on a Linux server, use the included `samsung-ac.service.example` as a
starting point:

```bash
sudo cp samsung-ac.service.example /etc/systemd/system/samsung-ac.service
sudo nano /etc/systemd/system/samsung-ac.service
sudo systemctl daemon-reload
sudo systemctl enable samsung-ac
sudo systemctl start samsung-ac
```

## Not Included

The original Samsung APK and any proprietary Samsung app assets are not included. This
project implements the local TLS/XML protocol directly.

## License

MIT License. See `LICENSE`.
