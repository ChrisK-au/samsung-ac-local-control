#!/usr/bin/env python3
"""
Samsung AC WiFi pairing tool.

Run this script, then press and hold the AP button on the WiFi adapter
(MIM-H02) for 5 seconds. You have about 60 seconds to do this.

The AC will issue a token which is saved to config.yaml for future use.

Usage: python pair.py [ac_ip_address]
"""

import ssl
import socket
import sys
import time
import xml.etree.ElementTree as ET
import yaml
from pathlib import Path


def pair(host: str):
    print(f"Connecting to Samsung AC at {host}:2878...")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("AES128-SHA:AES256-SHA:DES-CBC3-SHA:@SECLEVEL=0")
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.minimum_version = ssl.TLSVersion.TLSv1

    sock = socket.create_connection((host, 2878), timeout=10)
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.settimeout(2)

    def recv_all():
        """Read all available data, return list of XML messages."""
        buf = ""
        while True:
            try:
                chunk = tls.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                break
        messages = []
        for line in buf.strip().split("\n"):
            line = line.strip()
            if line:
                messages.append(line)
        return messages

    def send(msg):
        print(f"  >> {msg}")
        tls.sendall((msg + "\n").encode())

    # Phase 1: Read greeting
    time.sleep(1)
    for msg in recv_all():
        print(f"  << {msg}")

    # Phase 2: Request token
    send('<Request Type="GetToken"/>')
    time.sleep(2)

    # Read all pending messages (may include InvalidateAccount + GetToken response)
    got_ready = False
    for msg in recv_all():
        print(f"  << {msg}")
        if "Ready" in msg:
            got_ready = True

    if got_ready:
        print()
        print("=" * 60)
        print("  The AC is ready for pairing!")
        print()
        print("  Press and HOLD the AP button on the WiFi adapter")
        print("  (MIM-H02 module) for 5 seconds.")
        print()
        print("  The AP button is on the front of the small WiFi")
        print("  module box, near the LED indicators.")
        print()
        print("  Waiting up to 60 seconds...")
        print("=" * 60)
        print()

        token = None
        for i in range(60):
            messages = recv_all()
            for msg in messages:
                print(f"  << {msg}")

                # Try to parse XML and find token
                try:
                    # Strip XML declaration if present
                    xml_str = msg.split("?>")[-1] if "?>" in msg else msg
                    root = ET.fromstring(xml_str)

                    # Check for token in GetToken response/update. Some adapters
                    # return Update/Completed instead of Response/Okay.
                    if root.get("Type") == "GetToken" and root.get("Status") in ("Okay", "Completed"):
                        # Token as attribute
                        token = root.get("Token", "")
                        # Token as child element
                        if not token:
                            te = root.find(".//Token")
                            if te is not None:
                                token = te.text or te.get("Value", "")
                        if not token:
                            # Some models return empty token = pairing approved
                            token = "paired"
                except ET.ParseError:
                    pass

                # Fallback: look for token-like string
                if not token and "Token" in msg and "Okay" in msg:
                    for part in msg.split('"'):
                        if len(part) > 8 and part not in ("GetToken", "Okay", "utf-8", "1.0"):
                            if all(c.isalnum() or c in "-_" for c in part):
                                token = part
                                break

            if token:
                break

            sys.stdout.write(".")
            sys.stdout.flush()
            time.sleep(1)

        print()
        if token:
            print(f"\n  SUCCESS! Token: {token}")

            # Save to config
            config_path = Path(__file__).parent / "config.yaml"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
            else:
                cfg = {}
            cfg["token"] = token
            cfg["ac_host"] = host
            with open(config_path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False)
            print(f"  Saved to {config_path}")

            # Test: authenticate with token and get device list
            print("\n  Testing connection...")
            send(f'<Request Type="AuthToken"><User Token="{token}"/></Request>')
            time.sleep(2)
            for msg in recv_all():
                print(f"  << {msg}")

            send('<Request Type="DeviceList"/>')
            time.sleep(2)
            for msg in recv_all():
                print(f"  << {msg}")

            send('<Request Type="DeviceState"/>')
            time.sleep(2)
            for msg in recv_all():
                print(f"  << {msg}")

            print("\n  Pairing complete! Run: python -m samsung_ac")
        else:
            print("\n  No token received within 60 seconds.")
            print("  Make sure you pressed the AP button on the WiFi module.")
            print("  Try running this script again.")
    else:
        print("\n  Did not receive 'Ready' status from AC.")
        print("  The AC may already be paired. Trying device list...")
        send('<Request Type="DeviceList"/>')
        time.sleep(2)
        for msg in recv_all():
            print(f"  << {msg}")

    tls.close()


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else None
    if not host:
        config_path = Path(__file__).parent / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            host = cfg.get("ac_host", "192.168.1.100")
        else:
            host = "192.168.1.100"

    pair(host)
