#!/usr/bin/env python3
"""
provisioning.py — Provision a Samsung AC WiFi adapter (MIM-H02) onto a WiFi network.

Workflow:
  1. Hold the AP button on the adapter for ~5 seconds until the SMARTAIRCON
     access point appears.
  2. Run this script with the target WiFi credentials:

       sudo python3 provisioning.py <SSID> <PASSWORD>
       sudo python3 provisioning.py --auth WPA --encrypt TKIP MySSID MyPassword
       sudo python3 provisioning.py --auth OPEN CafeWiFi ""

The script:
  1. Connects this machine to SMARTAIRCON (WPA2-PSK 1111122222) via nmcli
  2. Performs an mTLS handshake with the adapter (client cert ac14k_m.pem)
  3. Sends the DRC-1.00 provisioning command APConnectionConfig
  4. Verifies the response Status
  5. Cleans up: removes the SMARTAIRCON connection, restores WiFi

The provisioning XML command format was reverse-engineered from Smart AC.apk:
  <Request Type="APConnectionConfig">
    <ConnectionConfig SSID="..." AuthMode="WPA2" EncryptType="AES" Key1="..."/>
  </Request>
"""

import argparse
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape


def xml_attr(value):
    """Escape a value for use inside an XML double-quoted attribute."""
    return escape(value, {'"': '&quot;'})

SCRIPT_DIR = Path(__file__).resolve().parent
CERT_FILE = SCRIPT_DIR / "ac14k_m.pem"

AP_SSID = "SMARTAIRCON"
AP_PSK = "1111122222"
ADAPTER_IP = "192.168.1.254"
ADAPTER_PORT = 2878
CON_NAME = "provision-ac"


# --------------------------------------------------------------------------
# nmcli helpers
# --------------------------------------------------------------------------

def nm(*args, check=True, capture=True):
    """Run nmcli, return stdout text (stripped)."""
    cmd = ["nmcli"] + list(args)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"nmcli {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def find_wifi_iface():
    """Pick the first WiFi device nmcli knows about."""
    out = nm("dev", "status")
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "wifi":
            return parts[0]
    raise RuntimeError("No WiFi device found via nmcli")


def ap_visible(iface):
    out = nm("dev", "wifi", "list", "ifname", iface)
    return AP_SSID in out


def connect_to_ap(iface):
    """Connect this machine to the adapter's SMARTAIRCON AP."""
    nm("dev", "disconnect", iface, check=False)
    time.sleep(1)
    nm("con", "delete", CON_NAME, check=False)
    nm("con", "add", "type", "wifi", "ifname", iface,
       "con-name", CON_NAME, "ssid", AP_SSID)
    nm("con", "modify", CON_NAME, "wifi-sec.key-mgmt", "wpa-psk")
    nm("con", "modify", CON_NAME, "wifi-sec.psk", AP_PSK)
    nm("con", "modify", CON_NAME, "ipv4.method", "auto")
    nm("con", "up", CON_NAME)
    time.sleep(6)


def wait_for_ip(iface, timeout=20):
    """Wait for DHCP lease; fall back to a static address."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["ip", "-4", "addr", "show", iface],
                           capture_output=True, text=True)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout)
        if m:
            return m.group(1)
        time.sleep(1)
    # Fallback: static IP on the adapter's subnet
    subprocess.run(["ip", "addr", "add", "192.168.1.50/24", "dev", iface],
                   check=False, capture_output=True)
    return "192.168.1.50 (static)"


def cleanup(iface, prev_state_ok=True):
    """Disconnect from SMARTAIRCON and restore normal WiFi."""
    print("\n=== Cleaning up ===")
    nm("con", "down", CON_NAME, check=False)
    nm("con", "delete", CON_NAME, check=False)
    if prev_state_ok:
        print("  Restoring WiFi...")
        nm("dev", "connect", iface, check=False)
    print("  Done.")


# --------------------------------------------------------------------------
# TLS / provisioning
# --------------------------------------------------------------------------

def build_provisioning_xml(ssid, password, auth_mode, encrypt):
    attrs = [f'SSID="{xml_attr(ssid)}"', f'AuthMode="{auth_mode}"']
    if auth_mode not in ("OPEN", "WEP"):
        attrs.append(f'EncryptType="{encrypt}"')
    if password:
        attrs.append(f'Key1="{xml_attr(password)}"')
    return ('<Request Type="APConnectionConfig">'
            f'<ConnectionConfig {" ".join(attrs)}/>'
            '</Request>')


def tls_connect():
    """Open mTLS connection to the adapter. Returns SSL socket."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Adapter speaks TLS 1.0 / AES128-SHA only (SSLv3-era crypto).
    # @SECLEVEL=0 must be set BEFORE load_cert_chain: the Samsung chain
    # contains an MD5-signed root from 1960 which OpenSSL 3 rejects at
    # security level 2+.
    ctx.set_ciphers("AES128-SHA:AES256-SHA:DES-CBC3-SHA:@SECLEVEL=0")
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.load_cert_chain(str(CERT_FILE))

    print(f"  Connecting to {ADAPTER_IP}:{ADAPTER_PORT} with client cert {CERT_FILE.name}...")
    sock = socket.create_connection((ADAPTER_IP, ADAPTER_PORT), timeout=15)
    tls = ctx.wrap_socket(sock, server_hostname=ADAPTER_IP)
    tls.settimeout(15)
    print(f"  TLS connected: {tls.cipher()[0]}")
    return tls


def recv_until(tls, marker, timeout=8, settle=0.0):
    """Read until marker appears (or timeout). Optionally drain a bit more."""
    buf = b""
    tls.settimeout(timeout)
    try:
        while marker not in buf:
            chunk = tls.recv(4096)
            if not chunk:
                break
            buf += chunk
        if settle:
            time.sleep(settle)
            try:
                while True:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass
    except socket.timeout:
        pass
    return buf


def provision(ssid, password, auth_mode, encrypt):
    tls = tls_connect()
    try:
        # 1. Greeting: DRC-1.00 + <Update Type="InvalidateAccount"/>
        greeting = recv_until(tls, b"DRC-1.00", settle=1.0)
        print(f"  Greeting ({len(greeting)} B): "
              f"{greeting.decode('utf-8', 'replace').strip()[:200]}")

        # 2. Provisioning command
        xml = build_provisioning_xml(ssid, password, auth_mode, encrypt)
        print(f"  SEND: {xml}")
        tls.sendall(xml.encode())

        # 3. Response
        # NOTE: observed behaviour — the adapter accepts the config and
        # reboots immediately, closing the TLS connection with NO response
        # at all. An empty/closed response therefore means "accepted".
        resp = recv_until(tls, b"</Response>", timeout=15)
        text = resp.decode("utf-8", "replace")
        print(f"  RESP ({len(resp)} B): {text.strip()}")

        m = re.search(r'Status="([^"]+)"', text)
        status = m.group(1) if m else None

        if status == "Okay":
            print("\n*** PROVISIONING SUCCESSFUL ***")
            print(f"  The adapter will now reboot and join: {ssid}")
            return 0
        if status == "Fail":
            ec = re.search(r'ErrorCode="?([0-9]+)"?', text)
            print(f"\n*** PROVISIONING FAILED *** (Status=Fail"
                  f"{', ErrorCode=' + ec.group(1) if ec else ''})")
            print("  Check SSID/password and that the network is in range.")
            return 2
        if not resp:
            # Connection closed with no data: adapter rebooted after accepting.
            print("\n*** PROVISIONING ACCEPTED *** (no response — adapter rebooting)")
            print(f"  It should now join: {ssid}")
            print("  NOTE: if the password was wrong, the adapter will fail to")
            print("  join and you must re-provision (check quoting of $ and *).")
            return 0
        print(f"\n*** UNEXPECTED RESPONSE *** (Status={status!r})")
        print("  Full response above; adapter state unknown.")
        return 3
    finally:
        tls.close()


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

# Samsung adapter OUI (first 3 bytes of MAC): 78:25:AD
SAMSUNG_OUI = "78:25:ad"


def verify_adapter_joined(ssid, password, auth_mode, encrypt, iface, timeout,
                          no_wait=False):
    """Join the target network and confirm the adapter shows up on it.

    The adapter reboots after provisioning and joins the target network.
    We connect the laptop to that same network and sweep for port 2878
    until the adapter appears (or timeout / --no-wait-to-verify).
    """
    print("\n=== Verification: looking for the adapter on target network ===")

    if auth_mode == "OPEN":
        password = ""

    # Reconnect to the target network (cleanup already dropped SMARTAIRCON)
    print(f"  Connecting to {ssid}...")
    nm("con", "delete", CON_NAME, check=False)
    try:
        if auth_mode == "OPEN":
            nm("con", "add", "type", "wifi", "ifname", iface,
               "con-name", CON_NAME, "ssid", ssid)
            nm("con", "modify", CON_NAME, "wifi-sec.key-mgmt", "none")
        else:
            nm("con", "add", "type", "wifi", "ifname", iface,
               "con-name", CON_NAME, "ssid", ssid)
            nm("con", "modify", CON_NAME, "wifi-sec.key-mgmt", "wpa-psk")
            nm("con", "modify", CON_NAME, "wifi-sec.psk", password)
        nm("con", "modify", CON_NAME, "ipv4.method", "auto")
        nm("con", "up", CON_NAME)
    except RuntimeError as e:
        print(f"  Could not join {ssid}: {e}")
        nm("con", "delete", CON_NAME, check=False)
        return 3

    # Wait for an IP, then actively sweep the subnet for the adapter's
    # control port (2878). Passive ARP watching is unreliable: the adapter
    # takes 60-120s+ to boot after provisioning and only appears in the ARP
    # table once traffic flows to it.
    my_ip = wait_for_ip(iface, timeout=20)
    print(f"  Joined {ssid}, laptop IP {my_ip}. Sweeping subnet for port 2878...")

    # Get subnet from our own IP
    m = re.match(r"(\d+\.\d+\.\d+)\.\d+", my_ip)
    if not m:
        print(f"  Cannot derive subnet from {my_ip!r}; skipping scan.")
        nm("con", "delete", CON_NAME, check=False)
        return 3
    subnet = m.group(1)

    deadline = time.time() + timeout
    found = False
    while time.time() < deadline and not found:
        found = sweep_port(subnet, ADAPTER_PORT)
        if not found:
            if no_wait:
                print("  adapter not up yet (--no-wait-to-verify)")
                break
            print(f"  [{int(deadline - time.time())}s left] adapter not up yet, retrying...")
            time.sleep(10)

    nm("con", "delete", CON_NAME, check=False)
    if found:
        print("\n*** VERIFICATION PASSED: adapter is on the target network ***")
        return 0
    print("\n*** VERIFICATION FAILED: adapter not seen on the target network ***")
    print("  - wrong password? re-provision with correct quoting")
    print("  - wrong SSID? check the network name")
    print("  - adapter may need more time to boot (check its green LED)")
    return 3


def sweep_port(subnet, port, timeout=1):
    """TCP-connect sweep of subnet .1-.254 for an open port. Returns host IP or None."""
    import concurrent.futures as cf

    def probe(i):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((f"{subnet}.{i}", port))
            return f"{subnet}.{i}"
        except Exception:
            return None
        finally:
            s.close()

    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        futures = [ex.submit(probe, i) for i in range(1, 255)]
        for fut in cf.as_completed(futures):
            host = fut.result()
            if host:
                print(f"  FOUND adapter at {host}:{port}")
                return host
    return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Provision a Samsung AC WiFi adapter onto a WiFi network.",
        epilog=("Run with sudo (needs nmcli control). "
                "Press the AP button on the adapter first."))
    ap.add_argument("ssid", help="Target WiFi network name")
    ap.add_argument("password", nargs="?", default="",
                    help="WiFi password (empty for open networks)")
    ap.add_argument("--auth", default="WPA2", choices=["WPA2", "WPA", "WEP", "OPEN"],
                    help="Target network auth mode (default WPA2)")
    ap.add_argument("--encrypt", default="AES", choices=["AES", "TKIP"],
                    help="Target network encryption (default AES)")
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip the post-provisioning check that the adapter "
                         "joined the target network (default: verify)")
    ap.add_argument("--no-wait-to-verify", action="store_true",
                    help="Skip the wait for the adapter to appear after "
                         "provisioning; verify once immediately (default: "
                         "wait up to --timeout)")
    ap.add_argument("--timeout", type=int, default=180,
                    help="Seconds to wait for the adapter to appear on the "
                         "target network after provisioning (default 180)")
    ap.add_argument("--no-wait-for-ap", action="store_true",
                    help="Only scan for SMARTAIRCON once; fail immediately if "
                         "not visible (default: wait 10s between rescans, "
                         "5 retries, ~60s total)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("Run with sudo: sudo python3 provisioning.py <SSID> [PASSWORD]")
        sys.exit(1)
    if not CERT_FILE.exists():
        print(f"Client certificate not found: {CERT_FILE}")
        sys.exit(1)
    if args.auth == "OPEN":
        args.password = ""

    print("=== Samsung AC WiFi Provisioning ===")
    print(f"  Target SSID:  {args.ssid}")
    print(f"  Auth mode:    {args.auth}")
    print(f"  Encryption:   {args.encrypt}")

    try:
        iface = find_wifi_iface()
    except RuntimeError as e:
        print(f"  ERROR: {e}")
        sys.exit(1)
    print(f"  WiFi iface:   {iface}")

    # 1. Verify AP visible
    print(f"\n[1/5] Scanning for {AP_SSID}...")
    attempts = 1 if args.no_wait_for_ap else 6  # 1 immediate + 5 retries
    for attempt in range(1, attempts + 1):
        if ap_visible(iface):
            print("  Found.")
            break
        if attempt == attempts:
            print(f"  {AP_SSID} not visible after retries. Hold the AP button "
                  "on the adapter for ~5s, then re-run.")
            sys.exit(1)
        print(f"  {AP_SSID} not visible (attempt {attempt}/{attempts}). "
              "Waiting 10s before rescan...")
        time.sleep(10)

    # 2. Connect
    print(f"\n[2/5] Connecting to {AP_SSID}...")
    try:
        connect_to_ap(iface)
    except RuntimeError as e:
        print(f"  Failed to connect: {e}")
        cleanup(iface)
        sys.exit(1)

    # 3. IP
    print(f"\n[3/5] Waiting for link to {ADAPTER_IP}...")
    my_ip = wait_for_ip(iface)
    print(f"  Laptop IP: {my_ip}")
    r = subprocess.run(["ping", "-c", "2", "-W", "2", ADAPTER_IP],
                       capture_output=True)
    print("  Adapter reachable." if r.returncode == 0
          else "  WARNING: adapter not pingable, trying anyway...")

    # 4. Provision
    print(f"\n[4/5] Sending provisioning command to {ADAPTER_IP}:{ADAPTER_PORT}...")
    try:
        rc = provision(args.ssid, args.password, args.auth, args.encrypt)
    except (socket.timeout, ssl.SSLError, ConnectionError) as e:
        print(f"  TLS/provisioning error: {e}")
        rc = 1

    # 5. Cleanup
    print(f"\n[5/5] Finished (rc={rc}).")
    cleanup(iface)

    # 6. Verification (optional): adapter should appear on the target network
    if rc == 0 and not args.no_verify:
        sys.exit(verify_adapter_joined(args.ssid, args.password, args.auth,
                                       args.encrypt, iface, args.timeout,
                                       no_wait=args.no_wait_to_verify))
    sys.exit(rc)


if __name__ == "__main__":
    main()
