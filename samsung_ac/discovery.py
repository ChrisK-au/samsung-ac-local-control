"""
Network discovery for Samsung AC WiFi adapters.

Scans the local network for devices listening on port 2878 (Samsung AC TLS port).
"""

import ipaddress
import logging
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

SAMSUNG_AC_PORT = 2878
SCAN_TIMEOUT = 1.0


def get_local_network() -> str:
    """Detect the local network CIDR (e.g. 192.168.1.0/24)."""
    try:
        # Connect to a public IP to find our local address (doesn't actually send data)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Assume /24 subnet
        network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return str(network)
    except Exception as e:
        logger.error(f"Could not detect local network: {e}")
        return "192.168.1.0/24"


def check_port(ip: str, port: int = SAMSUNG_AC_PORT, timeout: float = SCAN_TIMEOUT) -> bool:
    """Check if a specific port is open on the given IP."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def scan_network(network: str = None, port: int = SAMSUNG_AC_PORT) -> list[str]:
    """
    Scan the local network for Samsung AC units (port 2878 open).
    Returns a list of IP addresses.
    """
    if network is None:
        network = get_local_network()

    logger.info(f"Scanning {network} for Samsung AC on port {port}...")
    found = []

    try:
        net = ipaddress.IPv4Network(network, strict=False)
    except ValueError as e:
        logger.error(f"Invalid network: {e}")
        return found

    hosts = [str(ip) for ip in net.hosts()]

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(check_port, ip, port): ip for ip in hosts}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                if future.result():
                    logger.info(f"Found Samsung AC at {ip}")
                    found.append(ip)
            except Exception:
                pass

    return found


def try_arp_scan() -> list[dict]:
    """Try to get ARP table entries that might be Samsung devices."""
    results = []
    try:
        output = subprocess.check_output(["ip", "neigh"], timeout=5, text=True)
        for line in output.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 5 and parts[2] != "FAILED":
                ip = parts[0]
                mac = parts[4] if len(parts) > 4 else ""
                # Samsung OUI prefixes (common for their IoT/appliance devices)
                samsung_ouis = [
                    "00:12:fb", "00:16:32", "00:17:c9", "00:1e:e1",
                    "00:21:19", "00:23:39", "00:26:37", "08:d4:2b",
                    "10:d5:42", "14:49:e0", "18:67:b0", "1c:62:b8",
                    "24:18:1d", "28:cc:ff", "2c:ae:2b", "30:cd:a7",
                    "34:14:5f", "38:01:46", "3c:62:00", "40:b8:9a",
                    "44:6d:57", "48:44:f7", "4c:bc:98", "50:01:bb",
                    "54:40:ad", "58:c3:8b", "5c:49:7d", "60:6b:bd",
                    "64:1c:b0", "68:27:37", "6c:2f:2c", "70:2a:d5",
                    "74:45:ce", "78:47:1d", "7c:0a:3f", "80:65:6d",
                    "84:25:19", "88:32:9b", "8c:f5:a3", "90:18:7c",
                    "94:01:c2", "98:06:37", "9c:3a:af", "a0:82:1f",
                    "a4:08:ea", "a8:06:00", "ac:36:13", "b0:47:bf",
                    "b4:79:a7", "b8:d7:af", "bc:14:ef", "c0:97:27",
                    "c4:73:1e", "c8:14:79", "cc:07:ab", "d0:17:6a",
                    "d4:88:90", "d8:57:ef", "dc:71:96", "e0:db:55",
                    "e4:7c:f9", "e8:50:8b", "ec:1f:72", "f0:25:b7",
                    "f4:42:8f", "f8:04:2e", "fc:a1:3e",
                ]
                mac_lower = mac.lower()
                if any(mac_lower.startswith(oui) for oui in samsung_ouis):
                    results.append({"ip": ip, "mac": mac, "samsung": True})
    except Exception as e:
        logger.debug(f"ARP scan failed: {e}")

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"Local network: {get_local_network()}")
    print("Scanning for Samsung AC units...")
    devices = scan_network()
    if devices:
        for ip in devices:
            print(f"  Found: {ip}")
    else:
        print("  No devices found on port 2878")

    print("\nSamsung devices in ARP table:")
    for entry in try_arp_scan():
        print(f"  {entry['ip']} ({entry['mac']})")
