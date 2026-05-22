"""
Samsung AC WiFi protocol implementation.

Communicates with Samsung HVAC WiFi adapters (MIM-H02 and similar) via TLS on port 2878.
Protocol uses XML messages for device discovery, status polling, and control.
"""

import logging
import os
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SAMSUNG_AC_PORT = 2878
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 5
CERT_DIR = Path(__file__).parent / "certs"


class OpMode(Enum):
    AUTO = "Auto"
    COOL = "Cool"
    DRY = "Dry"
    WIND = "Wind"
    HEAT = "Heat"


class FanSpeed(Enum):
    AUTO = "Auto"
    LOW = "Low"
    MID = "Mid"
    HIGH = "High"
    TURBO = "Turbo"


class SwingMode(Enum):
    OFF = "Off"
    INDIRECT = "Indirect"
    DIRECT = "Direct"
    CENTER = "Center"
    WIDE = "Wide"
    SWING_UD = "SwingUD"
    SWING_LR = "SwingLR"
    ROTATION = "Rotation"
    FIXED = "Fixed"


class ConvenientMode(Enum):
    OFF = "Off"
    QUIET = "Quiet"
    SLEEP = "Sleep"
    TURBO = "TurboMode"
    SMART = "Smart"


@dataclass
class ACStatus:
    """Current state of the air conditioner."""
    power: str = "Off"
    mode: str = "Auto"
    target_temp: int = 24
    current_temp: int = 0
    fan_speed: str = "Auto"
    swing: str = "Off"
    convenient_mode: str = "Off"
    on_timer: Optional[int] = None
    off_timer: Optional[int] = None
    error: int = 0
    duid: str = ""
    connected: bool = False
    raw_attrs: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "power": self.power,
            "mode": self.mode,
            "target_temp": self.target_temp,
            "current_temp": self.current_temp,
            "fan_speed": self.fan_speed,
            "swing": self.swing,
            "convenient_mode": self.convenient_mode,
            "on_timer": self.on_timer,
            "off_timer": self.off_timer,
            "error": self.error,
            "duid": self.duid,
            "connected": self.connected,
        }


# Map of AC attribute IDs to ACStatus field names and types
ATTR_MAP = {
    "AC_FUN_POWER": ("power", str),
    "AC_FUN_OPMODE": ("mode", str),
    "AC_FUN_TEMPSET": ("target_temp", int),
    "AC_FUN_TEMPNOW": ("current_temp", int),
    "AC_FUN_WINDLEVEL": ("fan_speed", str),
    "AC_FUN_DIRECTION": ("swing", str),
    "AC_FUN_COMODE": ("convenient_mode", str),
    "AC_FUN_ONTIMER": ("on_timer", int),
    "AC_FUN_OFFTIMER": ("off_timer", int),
    "AC_FUN_ERROR": ("error", int),
}


class SamsungACProtocol:
    """
    Handles TLS communication with a Samsung AC WiFi adapter on port 2878.

    Protocol flow:
    1. TLS handshake (with Samsung's custom CA chain)
    2. Receive initial XML greeting from AC
    3. Send authentication token
    4. Request device list to discover DUID
    5. Request device state / send control commands
    """

    def __init__(self, host: str, port: int = SAMSUNG_AC_PORT, token: Optional[str] = None):
        self.host = host
        self.port = port
        self.token = token or ""
        self._sock: Optional[ssl.SSLSocket] = None
        self._lock = threading.Lock()
        self._connected = False
        self._status = ACStatus()
        self._duid: Optional[str] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._on_status_update = None
        self._buffer = ""

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> ACStatus:
        return self._status

    def set_status_callback(self, callback):
        """Set a callback function(status: ACStatus) called on each status update."""
        self._on_status_update = callback

    def connect(self) -> bool:
        """Establish TLS connection to the AC unit."""
        try:
            # Create SSL context - Samsung uses self-signed certs with weak crypto.
            # The AC has a 1024-bit RSA cert and small DH parameters, so we must:
            # - Disable certificate verification (self-signed, expired cert from 1970)
            # - Use RSA key exchange ciphers (DH params are too small for modern OpenSSL)
            # - Set security level to 0 to allow the weak crypto
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers("AES128-SHA:AES256-SHA:DES-CBC3-SHA:@SECLEVEL=0")
            # Force TLSv1 max - the AC doesn't support newer
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
            ctx.minimum_version = ssl.TLSVersion.TLSv1

            raw_sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
            self._sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
            self._sock.settimeout(READ_TIMEOUT)
            self._connected = True
            self._status.connected = True

            logger.info(f"TLS connected to {self.host}:{self.port}")
            logger.info(f"Cipher: {self._sock.cipher()}")

            # Start reader thread
            self._running = True
            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()

            # Wait for initial greeting
            time.sleep(1)

            return True

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._connected = False
            self._status.connected = False
            return False

    def disconnect(self):
        """Close the connection."""
        self._running = False
        self._connected = False
        self._status.connected = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _send(self, xml_str: str):
        """Send an XML message to the AC."""
        if not self._sock:
            raise ConnectionError("Not connected")
        with self._lock:
            data = xml_str + "\n"
            logger.debug(f"SEND: {xml_str}")
            self._sock.sendall(data.encode("utf-8"))

    def _read_loop(self):
        """Background thread that reads and processes responses from the AC."""
        while self._running and self._sock:
            try:
                data = self._sock.recv(4096)
                if not data:
                    logger.warning("Connection closed by AC")
                    self._connected = False
                    self._status.connected = False
                    break

                self._buffer += data.decode("utf-8", errors="replace")

                # Process complete XML messages (each ends with a newline or closing tag)
                while self._buffer:
                    # Find complete XML documents in buffer
                    msg, sep, rest = self._try_extract_xml(self._buffer)
                    if msg is None:
                        break
                    self._buffer = rest
                    self._handle_message(msg.strip())

            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Read error: {e}")
                    self._connected = False
                    self._status.connected = False
                break

    def _try_extract_xml(self, buf: str):
        """Try to extract a complete XML message from the buffer."""
        # Messages are line-delimited or terminated by closing XML tags
        # Look for common response endings
        for end_tag in ["</Response>", "</Update>", "</Request>", "/>"]:
            idx = buf.find(end_tag)
            if idx >= 0:
                end = idx + len(end_tag)
                return buf[:end], True, buf[end:].lstrip("\r\n")

        # Also handle the DRC-1.00 greeting line
        if "DRC-1.00" in buf:
            idx = buf.find("\n")
            if idx >= 0:
                return buf[:idx], True, buf[idx+1:]

        return None, False, buf

    def _handle_message(self, msg: str):
        """Process an incoming XML message from the AC."""
        if not msg:
            return

        logger.debug(f"RECV: {msg}")

        # Handle the initial greeting: <?xml version="1.0" encoding="utf-8" ?><Update Type="InvalidateAccount"/>
        if "DRC-1.00" in msg:
            logger.info(f"Received greeting: {msg}")
            # Send authentication after greeting
            self._send_auth()
            return

        if "InvalidateAccount" in msg:
            logger.info("Received InvalidateAccount - sending auth")
            self._send_auth()
            return

        try:
            root = ET.fromstring(msg)
        except ET.ParseError:
            logger.debug(f"Could not parse XML: {msg[:200]}")
            return

        tag = root.tag
        msg_type = root.get("Type", "")
        status = root.get("Status", "")

        if tag == "Response":
            self._handle_response(root, msg_type, status)
        elif tag == "Update":
            self._handle_update(root, msg_type)
        elif tag == "Request":
            # AC can send requests too (rare)
            logger.debug(f"Received Request: {msg_type}")

    def _send_auth(self):
        """Send authentication token to the AC."""
        if self.token:
            xml = f'<Request Type="AuthToken"><User Token="{self.token}"/></Request>'
        else:
            xml = '<Request Type="GetToken"/>'
        self._send(xml)

    def _handle_response(self, root, msg_type: str, status: str):
        """Handle a Response message from the AC."""
        if status and status != "Okay":
            logger.warning(f"Response error: Type={msg_type} Status={status}")

        if msg_type == "AuthToken" or msg_type == "Authenticate":
            if status == "Okay":
                logger.info("Authentication successful")
                self._request_device_list()
            else:
                logger.warning(f"Auth response: {status}")
                # Try requesting device list anyway - some units don't require auth
                self._request_device_list()

        elif msg_type == "GetToken":
            # AC sends us a token to use
            token_elem = root.find(".//Token")
            if token_elem is not None:
                self.token = token_elem.text or token_elem.get("Value", "")
                logger.info(f"Received token: {self.token}")
            # Now authenticate with the token
            if self.token:
                xml = f'<Request Type="AuthToken"><User Token="{self.token}"/></Request>'
                self._send(xml)
            else:
                self._request_device_list()

        elif msg_type == "DeviceList":
            self._parse_device_list(root)

        elif msg_type == "DeviceState":
            self._parse_device_state(root)

        elif msg_type == "DeviceControl":
            logger.info(f"Control response: {status}")
            # Request updated state after control
            if self._duid:
                time.sleep(0.5)
                self.request_status()

    def _handle_update(self, root, msg_type: str):
        """Handle an Update (push notification) from the AC."""
        if msg_type == "Status":
            self._parse_status_update(root)
        elif msg_type == "InvalidateAccount":
            logger.info("InvalidateAccount update - re-authenticating")
            self._send_auth()

    def _request_device_list(self):
        """Request the list of devices from the AC."""
        self._send('<Request Type="DeviceList"/>')

    def _parse_device_list(self, root):
        """Parse device list response and request state for first device."""
        for device in root.iter("Device"):
            duid = device.get("DUID", "")
            model = device.get("ModelID", "")
            dev_type = device.get("Type", "")
            logger.info(f"Found device: DUID={duid} Model={model} Type={dev_type}")
            if duid and (dev_type == "AC" or not self._duid):
                self._duid = duid
                self._status.duid = duid

        if self._duid:
            logger.info(f"Using device DUID: {self._duid}")
            self.request_status()
        else:
            logger.warning("No devices found in device list")

    def request_status(self):
        """Request current device state."""
        if self._duid:
            self._send(f'<Request Type="DeviceState"><Device DUID="{self._duid}"/></Request>')

    def _parse_device_state(self, root):
        """Parse a DeviceState response, updating our status."""
        for attr in root.iter("Attr"):
            attr_id = attr.get("ID", "")
            value = attr.get("Value", "")
            self._update_attr(attr_id, value)

        if self._on_status_update:
            self._on_status_update(self._status)

    def _parse_status_update(self, root):
        """Parse a Status update (push) from the AC."""
        for attr in root.iter("Attr"):
            attr_id = attr.get("ID", "")
            value = attr.get("Value", "")
            self._update_attr(attr_id, value)

        if self._on_status_update:
            self._on_status_update(self._status)

    def _update_attr(self, attr_id: str, value: str):
        """Update a single attribute in the status."""
        self._status.raw_attrs[attr_id] = value

        if attr_id == "AC_FUN_TEMPNOW":
            try:
                temp = int(value)
            except (ValueError, TypeError):
                logger.debug(f"Could not parse {attr_id}={value} as temperature")
                return
            # Some MIM-H02 units report current temperature in Fahrenheit even
            # while the setpoint is Celsius.
            if temp > 60:
                temp = round((temp - 32) * 5 / 9)
            self._status.current_temp = temp
            return

        if attr_id == "AC_FUN_ERROR" and value == "30303030":
            # Hex-encoded ASCII "0000" means no error.
            self._status.error = 0
            return

        if attr_id in ATTR_MAP:
            field_name, field_type = ATTR_MAP[attr_id]
            try:
                setattr(self._status, field_name, field_type(value))
            except (ValueError, TypeError):
                logger.debug(f"Could not parse {attr_id}={value} as {field_type}")

    # --- Control methods ---

    def _control(self, attr_id: str, value: str):
        """Send a control command to set an attribute."""
        if not self._duid:
            logger.error("No device DUID - cannot send control")
            return False
        xml = (
            f'<Request Type="DeviceControl">'
            f'<Control CommandID="cmd" DUID="{self._duid}">'
            f'<Attr ID="{attr_id}" Value="{value}"/>'
            f'</Control>'
            f'</Request>'
        )
        try:
            self._send(xml)
            return True
        except Exception as e:
            logger.error(f"Control failed: {e}")
            return False

    def set_power(self, on: bool) -> bool:
        return self._control("AC_FUN_POWER", "On" if on else "Off")

    def set_mode(self, mode: str) -> bool:
        """Set operation mode: Auto, Cool, Dry, Wind, Heat"""
        valid = {m.value for m in OpMode}
        if mode not in valid:
            logger.error(f"Invalid mode: {mode}. Valid: {valid}")
            return False
        return self._control("AC_FUN_OPMODE", mode)

    def set_temperature(self, temp: int) -> bool:
        """Set target temperature (16-30)."""
        temp = max(16, min(30, temp))
        return self._control("AC_FUN_TEMPSET", str(temp))

    def set_fan_speed(self, speed: str) -> bool:
        """Set fan speed: Auto, Low, Mid, High, Turbo"""
        valid = {s.value for s in FanSpeed}
        if speed not in valid:
            logger.error(f"Invalid fan speed: {speed}. Valid: {valid}")
            return False
        return self._control("AC_FUN_WINDLEVEL", speed)

    def set_swing(self, mode: str) -> bool:
        """Set swing/direction mode."""
        return self._control("AC_FUN_DIRECTION", mode)

    def set_convenient_mode(self, mode: str) -> bool:
        """Set convenient mode: Off, Quiet, Sleep, TurboMode, Smart"""
        return self._control("AC_FUN_COMODE", mode)

    def set_on_timer(self, minutes: int) -> bool:
        """Set on-timer in minutes (0 to disable)."""
        return self._control("AC_FUN_ONTIMER", str(minutes))

    def set_off_timer(self, minutes: int) -> bool:
        """Set off-timer in minutes (0 to disable)."""
        return self._control("AC_FUN_OFFTIMER", str(minutes))
