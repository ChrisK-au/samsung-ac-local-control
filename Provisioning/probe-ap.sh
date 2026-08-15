#!/usr/bin/env bash
# Probe the Samsung AC adapter in AP mode on port 2878
# Run: sudo ./probe-ap.sh
# Requires: adapter in AP mode (SMARTAIRCON visible)
set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "Run with: sudo $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WIFI_IFACE="wlp0s20f3"
AP_SSID="SMARTAIRCON"
AP_PSK="1111122222"
ADAPTER_IP="192.168.1.254"
ADAPTER_PORT="2878"

REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

cleanup() {
    echo ""
    echo "=== Cleaning up ==="
    echo "  Disconnecting from $AP_SSID..."
    nmcli con down probe-ap 2>/dev/null || true
    nmcli con delete probe-ap 2>/dev/null || true
    echo "  Reconnecting to previous WiFi..."
    nmcli dev connect "$WIFI_IFACE" 2>/dev/null || true
    rm -f /tmp/probe-ap.py
    echo "Done."
}
trap cleanup EXIT

echo "=== Samsung AC AP-Mode Probe ==="
echo ""

# 1. Scan for SMARTAIRCON
echo "[1/5] Scanning for $AP_SSID..."
if ! nmcli dev wifi list 2>/dev/null | grep -q "$AP_SSID"; then
    echo "  SMARTAIRCON not visible. Press/hold AP button for 5+ seconds, then re-run."
    exit 1
fi
echo "  Found."

# 2. Connect
echo "[2/5] Connecting to $AP_SSID..."
nmcli dev disconnect "$WIFI_IFACE" 2>/dev/null || true
sleep 1

# Use explicit connection profile — nmcli's quick-connect can't detect key-mgmt
nmcli con delete probe-ap 2>/dev/null || true
nmcli con add type wifi ifname "$WIFI_IFACE" con-name probe-ap ssid "$AP_SSID" 2>&1 || true
nmcli con modify probe-ap wifi-sec.key-mgmt wpa-psk 2>&1 || true
nmcli con modify probe-ap wifi-sec.psk "$AP_PSK" 2>&1 || true
nmcli con modify probe-ap ipv4.method auto 2>&1 || true
nmcli con up probe-ap 2>&1
sleep 6

# Check connectivity
MY_IP=$(ip -4 addr show "$WIFI_IFACE" 2>/dev/null | grep -oP 'inet \K[\d.]+' || true)
if [ -z "$MY_IP" ]; then
    echo "  No IP from DHCP. Trying static IP 192.168.1.50..."
    ip addr add 192.168.1.50/24 dev "$WIFI_IFACE" 2>/dev/null || true
    MY_IP="192.168.1.50 (static)"
fi
echo "  Laptop IP: $MY_IP"

# 3. Ping adapter
echo "[3/5] Pinging adapter at $ADAPTER_IP..."
if ping -c 2 -W 2 "$ADAPTER_IP" >/dev/null 2>&1; then
    echo "  Adapter reachable."
else
    echo "  WARNING: Adapter not pingable. Trying anyway..."
fi

# 4. Write Python probe script
echo "[4/5] Running TLS probe on $ADAPTER_IP:$ADAPTER_PORT..."
cat > /tmp/probe-ap.py << 'PYEOF'
import socket
import ssl
import sys
import time

HOST = sys.argv[1]
PORT = int(sys.argv[2])

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
ctx.set_ciphers("AES128-SHA:AES256-SHA:DES-CBC3-SHA:@SECLEVEL=0")
ctx.maximum_version = ssl.TLSVersion.TLSv1_2
ctx.minimum_version = ssl.TLSVersion.TLSv1

print(f"Connecting to {HOST}:{PORT}...")
sock = socket.create_connection((HOST, PORT), timeout=10)
tls = ctx.wrap_socket(sock, server_hostname=HOST)
tls.settimeout(10)

print(f"TLS connected! Cipher: {tls.cipher()}")
print(f"Server cert subject: {tls.getpeercert(binary_form=False).get('subject', 'N/A')}")
print()

# Read the greeting
print("=== Reading adapter greeting ===")
buffer = b""
start = time.time()
while time.time() - start < 8:
    try:
        data = tls.recv(4096)
        if not data:
            print("Connection closed by adapter.")
            break
        buffer += data
        # Check if we have complete messages
        text = buffer.decode("utf-8", errors="replace")
        if "DRC-1.00" in text or "</Update>" in text or "</Response>" in text:
            break
    except socket.timeout:
        break
    except Exception as e:
        print(f"Recv error: {e}")
        break

text = buffer.decode("utf-8", errors="replace")
print(f"[{len(buffer)} bytes]")
print(text)
print()

# AP mode: try GetToken — the adapter may issue a provisioning token
print("=== Trying GetToken (let adapter give us a token) ===")
try:
    tls.sendall(b'<Request Type="GetToken"/>\n')
    time.sleep(2)
    data = tls.recv(4096)
    resp = data.decode('utf-8', errors='replace')
    print(f"RECV [{len(data)} bytes]: {resp}")
    
    # Extract token if present
    import re
    token_match = re.search(r'Token[^>]*>([^<]+)', resp)
    if token_match:
        TOKEN = token_match.group(1)
        print(f"\n>>> Got token: {TOKEN}")
        print(f"\n=== Authenticating with token ===")
        tls.sendall(f'<Request Type="AuthToken"><User Token="{TOKEN}"/></Request>\n'.encode())
        time.sleep(1.5)
        data = tls.recv(4096)
        print(f"RECV [{len(data)} bytes]: {data.decode('utf-8', errors='replace')}")
    
    # Try WiFi commands
    print(f"\n=== Trying WiFi commands ===")
    wifi_cmds = [
        '<Request Type="GetAPList"/>',
        '<Request Type="ScanWiFi"/>',
        '<Request Type="DeviceList"/>',
        '<Request Type="GetWiFiInfo"/>',
    ]
    for cmd in wifi_cmds:
        print(f"\nSEND: {cmd}")
        try:
            tls.sendall((cmd + "\n").encode())
            time.sleep(1.5)
            data = tls.recv(4096)
            print(f"RECV [{len(data)} bytes]: {data.decode('utf-8', errors='replace')}")
        except socket.timeout:
            print("  (timeout)")
        except Exception as e:
            print(f"  Error: {e}")
            break
    
except Exception as e:
    print(f"  Error: {e}")

print()
print("Done.")
tls.close()
PYEOF

# 5. Run the probe
echo ""
chmod 644 /tmp/probe-ap.py
python3 /tmp/probe-ap.py "$ADAPTER_IP" "$ADAPTER_PORT"
echo ""
echo "[5/5] Complete."
