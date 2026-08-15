#!/usr/bin/env bash
# Capture Samsung AC WiFi adapter provisioning traffic
# Run: sudo ./capture.sh
# Requires: adapter in AP mode (SMARTAIRCON visible)
set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "Run with: sudo $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PCAP="${SCRIPT_DIR}/provisioning-$(date +%Y%m%d-%H%M%S).pcap"
WIFI_IFACE="wlp0s20f3"
MON_IFACE="mon0"
AP_BSSID="78:25:AD:10:8C:3C"
AP_PSK="1111122222"
AP_SSID="SMARTAIRCON"

# Run commands as the original user for things that need it
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

cleanup() {
    echo ""
    echo "=== Cleaning up ==="
    if [ "$MON_IFACE" = "$WIFI_IFACE" ]; then
        # Direct monitor mode — restore to managed
        ip link set "$WIFI_IFACE" down 2>/dev/null || true
        iw dev "$WIFI_IFACE" set type managed 2>/dev/null || true
        ip link set "$WIFI_IFACE" up 2>/dev/null || true
    else
        # Sub-interface mode — delete mon0
        ip link set "$MON_IFACE" down 2>/dev/null || true
        iw dev "$MON_IFACE" del 2>/dev/null || true
        ip link set "$WIFI_IFACE" up 2>/dev/null || true
    fi
    nmcli dev set "$WIFI_IFACE" managed yes 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT

echo "=== Samsung AC Provisioning Capture ==="
echo "Output: $PCAP"
echo ""

# 1. Verify SMARTAIRCON is visible
echo "[1/5] Scanning for SMARTAIRCON..."
if ! nmcli dev wifi list 2>/dev/null | grep -q "$AP_BSSID"; then
    echo "SMARTAIRCON not visible. Press/hold AP button for 5+ seconds, then re-run."
    exit 1
fi
AP_CHANNEL=$(nmcli dev wifi list 2>/dev/null | grep "$AP_BSSID" | awk '{print $4}')
echo "  Found: $AP_BSSID, channel $AP_CHANNEL"

# 2. Set up monitor interface
echo "[2/5] Setting up monitor interface..."
# Force disconnect and bring down
nmcli dev set "$WIFI_IFACE" managed no
nmcli dev disconnect "$WIFI_IFACE" 2>/dev/null || true
sleep 1
ip link set "$WIFI_IFACE" down
# Try sub-interface first
if iw dev "$WIFI_IFACE" interface add "$MON_IFACE" type monitor 2>/dev/null; then
    ip link set "$MON_IFACE" up
    iw dev "$MON_IFACE" set channel "$AP_CHANNEL"
    echo "  $MON_IFACE ready on channel $AP_CHANNEL"
else
    # Fallback: switch main interface directly to monitor
    echo "  Sub-interface failed, switching $WIFI_IFACE directly to monitor..."
    MON_IFACE="$WIFI_IFACE"
    iw dev "$WIFI_IFACE" set type monitor
    ip link set "$WIFI_IFACE" up
    iw dev "$WIFI_IFACE" set channel "$AP_CHANNEL"
    echo "  $WIFI_IFACE in direct monitor mode on channel $AP_CHANNEL"
fi

# 3. Start capture
echo "[3/5] Starting capture..."
sudo -u "$REAL_USER" dumpcap -i "$MON_IFACE" -s 0 -w "$PCAP" &
DUMPCAP_PID=$!
sleep 2

# 4. Verify capture is live
echo "[4/5] Verifying capture..."
sleep 3
SIZE1=$(stat -c%s "$PCAP" 2>/dev/null || echo 0)
sleep 2
SIZE2=$(stat -c%s "$PCAP" 2>/dev/null || echo 0)

# Check for SMARTAIRCON frames (tshark as root can read the file)
if tshark -r "$PCAP" -Y "wlan.bssid==${AP_BSSID}" 2>/dev/null | grep -q .; then
    if [ "$SIZE1" != "$SIZE2" ]; then
        echo "  SMARTAIRCON frames confirmed, capture growing."
    else
        echo "  SMARTAIRCON frames found but capture may have stalled."
    fi
else
    echo "  WARNING: No SMARTAIRCON frames yet. Proceeding anyway."
fi

# 5. Wait for user
echo ""
echo "=== CAPTURE RUNNING (PID $DUMPCAP_PID) ==="
echo ""
echo "On the Kindle:"
echo "  1. Connect to SMARTAIRCON (password: $AP_PSK)"
echo "  2. Open Samsung Smart AC app"
echo "  3. Run the network setup flow"
echo "  4. Use test credentials: TestSamsungAC / TestPass1234"
echo ""
echo "Press ENTER when done."
read -r

# Stop capture
echo "[5/5] Stopping capture..."
kill -INT "$DUMPCAP_PID" 2>/dev/null || true
sleep 1

# Fix ownership
chown "$REAL_USER:$REAL_USER" "$PCAP" 2>/dev/null || true

# Stats
echo ""
echo "=== Capture complete ==="
ls -lh "$PCAP"
echo ""
echo "Protocol hierarchy:"
tshark -r "$PCAP" -q -z io,phs 2>/dev/null || true
echo ""
echo "EAPOL frames: $(tshark -r "$PCAP" -Y eapol 2>/dev/null | wc -l)"
echo "SMARTAIRCON data frames: $(tshark -r "$PCAP" -Y "wlan.bssid==${AP_BSSID} && wlan.fc.type==2" 2>/dev/null | wc -l)"
echo ""
echo "To decrypt in Wireshark:"
echo "  wireshark $PCAP"
echo "  Edit → Preferences → IEEE 802.11 → Decryption Keys → +"
echo "  Key type: wpa-pwd   Key: ${AP_PSK}:${AP_SSID}"
