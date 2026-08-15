# Samsung AC WiFi Adapter Provisioning Notes

This project can currently discover and control a Samsung AC WiFi adapter after the
adapter is already connected to the local WiFi network. It does not yet support
putting the adapter onto a new WiFi network.

The likely future feature is an offline `provisioning.py` helper that runs on a
Linux laptop while the laptop is connected to the Samsung adapter's temporary AP
mode network.

## Adapter AP Details

Observed on 2026-06-12:

- Temporary AP SSID: `SMARTAIRCON`
- BSSID/MAC: `78:25:AD:10:8C:3C`
- Security: `WPA2-PSK`
- PSK/password: `1111122222`
- Channel: `1` (2412 MHz)
- PHY: 2.4 GHz, 802.11n, 20 MHz channel width
- Adapter IP in AP mode: `192.168.1.254`
- Client DHCP range: `192.168.1.100+` (observed `192.168.1.100`, `192.168.1.101`)
- DHCP domain suffix: `SAMSUNGSMART`
- AP mode is short-lived — the adapter disappears from scans during/after provisioning

## AP-Mode Protocol (Discovered 2026-07-23)

### Transport

Provisioning uses the **exact same protocol** as normal LAN-mode control:

| Detail | Value |
|--------|-------|
| Transport | TCP port **2878** |
| Encryption | **TLS** (same as normal mode) |
| Cipher | `AES128-SHA` (SSLv3, no forward secrecy) |
| TLS version | TLSv1.0 (server doesn't support newer) |
| SNI | Not used |
| Greeting | `DRC-1.00` |
| Protocol | XML request/response (DRC-1.00) |
| Authentication | **mTLS (client certificate required)** |

### mTLS Client Certificate

The Samsung app authenticates with a client certificate stored in
`assets/AC14K_M_KeyStore.bks` (Bouncy Castle keystore, BKS v1 format).

Keystore details:
- Store password: **empty** (`""`)
- Alias: `ac14k_m`
- Cert chain (4 certs):
  1. `CN=AC14K_M` (client cert, RSA 2048-bit, serial 0x09)
  2. `CN=RemoteAccessCA(CE)` (intermediate)
  3. `CN=CECA` (intermediate)
  4. `CN=ROOTCA` (root, same as `Source/samsung_ac/certs/ca_chain.pem`)
- Cert validity: 1960-01-01 to 2060-01-01

The client certificate chain has been extracted to PEM format (see
`Provisioning/apk_extracted/`). The root CA cert matches the one already
in `Source/samsung_ac/certs/ca_chain.pem`.

### Client Certificate + Private Key (SOLVED 2026-08-15)

The full client identity — private key **and** certificate chain — is available
in `Provisioning/ac14k_m.pem` (sourced from
[github.com/hmmferreira/samsung-aircon-8888-get-token](https://github.com/hmmferreira/samsung-aircon-8888-get-token)).

- Unencrypted RSA 2048-bit private key
- Full 4-cert chain: `AC14K_M → RemoteAccessCA(CE) → CECA → ROOTCA`
- Key modulus matches the `AC14K_M` client cert (verified)
- Works directly with `curl --cert` and Python `ssl.load_cert_chain()`

The key password inside the BKS keystore is no longer needed — we bypass the
keystore entirely and use the PEM directly for mTLS.

> Note: the historical blocker — the key password being generated at runtime
> by the native `rtdltkzl()→emJI()` chain in `libbrolib-ajni.so` — is moot.
> (The original BKS description is kept below for reference.)

The private key within `AC14K_M_KeyStore.bks` is encrypted with a **different
password** than the store password. The store password is empty; the key
password is not.

The key password is generated at runtime by the Samsung app via:
```
com.samsung.sm.a.g() → Sm.rtdltkzl() → Sm.emJI()
```
These are native methods in `libbrolib-ajni.so` (ARM64/ARMv7). The `.so` also
contains dedicated JNI functions `getClientCertPass` and `getServerCertPass`.

**Options to recover the key password:**
1. Run the `.so` in `qemu-aarch64` and call `getClientCertPass`
2. MITM the TLS session when near the AP (laptop proxies between Kindle and adapter)
3. Full reverse-engineering of the native string decryption in the `.so`

### XML Protocol Flow

The adapter in AP mode responds to the first command with
`<Update Type="InvalidateAccount"/>`, then requires client certificate
authentication before accepting any other commands.

Expected flow (same as normal mode):
1. TLS handshake with client certificate (mTLS)
2. Server sends: `DRC-1.00` + `<Update Type="InvalidateAccount"/>`
3. Client sends: `<Request Type="AuthToken"><User Token="..."/></Request>`
4. Server responds to auth
5. Client sends WiFi provisioning commands (exact XML TBD)
6. Server responds with success/failure

The WiFi provisioning XML command names are not yet known. Likely candidates:
`GetAPList`, `SetWiFi`, `ScanWiFi`, etc. The APK's `classes.dex` strings are
heavily obfuscated via native methods — command names are constructed at runtime.

### Related APK Assets

| File | Contents |
|------|----------|
| `assets/AC14K_M_KeyStore.bks` | Client cert + encrypted private key (1 entry) |
| `assets/DA_CertChain.bks` | CA certificates only (3 certs, no keys) |
| `assets/mycacert.bks` | Trusted root CA store (78 standard CA certs) |
| `assets/device_config.xml` | Device definition for refrigerator (not AC — app supports multiple device types) |
| `lib/arm64-v8a/libbrolib-ajni.so` | Native string obfuscation and crypto library |
| `lib/armeabi-v7a/libbrolib-ajni.so` | 32-bit ARM variant |

## Capture Infrastructure

### Working Monitor-Mode Capture

Intel AX201 (wlp0s20f3) requires the **parent interface to stay UP** when using
a monitor sub-interface. The working setup:

```bash
sudo nmcli dev set wlp0s20f3 managed no
sudo iw dev wlp0s20f3 interface add mon0 type monitor
sudo ip link set mon0 up
sudo iw dev mon0 set channel 1
dumpcap -i mon0 -s 0 -w provisioning.pcap
```

**Do not bring wlp0s20f3 down** — Intel AX cards stop the monitor sub-interface
when the parent goes down. Direct `set type monitor` on the main interface
produces malformed radiotap headers on this chipset.

A working capture script is at `Provisioning/capture.sh` (run with `sudo`).

### WPA2 Decryption

The adapter AP uses WPA2-PSK. To decrypt captured traffic in Wireshark:
1. Capture must include the 4-way EAPOL handshake (client must associate during capture)
2. Edit → Preferences → IEEE 802.11 → Decryption Keys → +
3. Key type: `wpa-pwd`, Key: `1111122222:SMARTAIRCON`

If the client is already associated, force a re-handshake with deauth before capturing:
```bash
sudo aireplay-ng -0 1 -a 78:25:AD:10:8C:3C mon0
```

### Successful Captures

- `aircrack/samsung-smartaircon-ap.pcap` — Original capture (WPA handshake only, no provisioning data)
- `Provisioning/provisioning-20260723-101504.pcap` — Full provisioning capture (520 SMARTAIRCON frames, 4 EAPOL, decrypted)

### AP-Mode TLS Probe

A probe script at `Provisioning/probe-ap.sh` (run with `sudo`) connects the
laptop to SMARTAIRCON and opens a TLS connection to `192.168.1.254:2878`.
It sends various XML requests to discover the protocol.

Currently times out after `InvalidateAccount` because mTLS client cert auth is required.

## Credential Handling Constraint

Do not rely on automatically reading WiFi credentials from a phone/tablet.

Modern Android apps and browser apps generally cannot read saved WiFi passwords.
The provisioning helper should ask the user to manually enter:

- Target SSID
- Security mode (WPA/WPA2/WEP/open)
- Encryption mode (AES/TKIP if needed)
- WiFi password

## Preferred User Workflow

Use a Linux laptop:

1. Put the Samsung AC WiFi adapter into AP mode (hold AP button 5+ seconds).
2. Run `Provisioning/provisioning.py` with the target WiFi credentials:

```bash
sudo python3 Provisioning/provisioning.py MyNetwork MyPassword
# or:  --auth WPA --encrypt TKIP   /   --auth OPEN "CafeWiFi" ""
```

The script does everything else: connects to SMARTAIRCON via nmcli, performs
the mTLS handshake with `ac14k_m.pem`, sends `APConnectionConfig`, verifies
`Status="Okay"`, then restores the normal WiFi connection.

3. Wait for the adapter to reboot and join the target network.
4. Reconnect the laptop/server to the normal LAN.
5. Use the existing web UI's `Scan Network` button to find the adapter again.

## Implementation Status

- `Provisioning/provisioning.py` — **implemented and field-tested** (2026-08-15)
  - Loads client cert + key (`ac14k_m.pem`) for mTLS
  - Connects to SMARTAIRCON WiFi via nmcli (WPA2-PSK `1111122222`)
  - Opens TLS to `192.168.1.254:2878`
  - Sends the decoded `APConnectionConfig` XML
  - Reports success/failure, cleans up and restores WiFi
  - mTLS handshake verified against a mock server trusting Samsung ROOTCA
  - **Field test PASSED**: adapter provisioned onto "Pasta", found at
    10.2.3.144 (MAC 78:25:ad:10:8c:3c), port 2878 serving DRC-1.00

### Field-test lessons (2026-08-15)

1. **No response = success**: the adapter accepts `APConnectionConfig` and
   reboots immediately, closing TLS with 0 bytes. The script treats an
   empty/closed response as "accepted".
2. **Bash quoting**: passwords containing `$`/`*` MUST be single-quoted
   (`'F$9mT.Pc*'`); double quotes let bash expand `$9` → silently corrupts
   the PSK (bit us twice).
3. **Slow adapter CPU**: ~30s to bring up SMARTAIRCON, 60-120s+ to boot and
   join the target network after provisioning. The script waits up to 60s
   for the AP (10s retries × 5) and up to 180s for the adapter to appear
   on the target network (port-2878 sweep, 10s retries).
4. **2.4GHz only**: the adapter's radio is 2.4GHz-only — a 5GHz-only SSID
   cannot be provisioned onto it.
5. **Verification via port sweep, not ARP**: passive ARP watching fails
   (adapter only appears in ARP once traffic flows). An active TCP connect
   to port 2878 across the subnet is the reliable check — it even detects
   the adapter before its green LED lights.

### Usage (final)

```bash
sudo python3 provisioning.py <SSID> <PASSWORD>          # wait ~60s for AP, ~180s to verify
sudo python3 provisioning.py <SSID> <PASSWORD> --no-wait-for-ap     # single AP scan
sudo python3 provisioning.py <SSID> <PASSWORD> --no-wait-to-verify  # single verify attempt
sudo python3 provisioning.py <SSID> <PASSWORD> --no-verify          # skip verification
sudo python3 provisioning.py --auth WPA --encrypt TKIP <SSID> <PASS>
sudo python3 provisioning.py --auth OPEN CafeWiFi ""
```

## Remaining Work

1. ~~Recover client private key~~ — **DONE** (`Provisioning/ac14k_m.pem`)
2. ~~Discover WiFi provisioning XML commands~~ — **DONE** (`APConnectionConfig`)
3. ~~Implement `provisioning.py`~~ — **DONE**; field-tested 2026-08-15 on "Pasta"
