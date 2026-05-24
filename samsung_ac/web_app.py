"""
Flask web application for Samsung AC control.

Provides a mobile-friendly web UI and REST API.
"""

import json
import logging
import os
import time
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request, send_file

from .discovery import scan_network, get_local_network
from .protocol import SamsungACProtocol, ACStatus, OpMode, FanSpeed, SwingMode
from .scheduler import ACScheduler
from .usage_log import UsageLogger

logger = logging.getLogger(__name__)

app = Flask(__name__,
            template_folder=str(Path(__file__).parent / "templates"),
            static_folder=str(Path(__file__).parent / "static"))

# Global state
ac: SamsungACProtocol = None
scheduler: ACScheduler = None
usage_logger: UsageLogger = None
config: dict = {}
schedule_path: Path = None
usage_log_path: Path = None
config_file_path: Path = None
last_reconnect_attempt = 0
RECONNECT_RETRY_SECONDS = 30


def get_config_path(config_path: str = None) -> Path:
    """Return the resolved config path."""
    if config_path is None:
        return Path(__file__).parent.parent / "config.yaml"
    return Path(config_path)


def get_schedule_path(config_path: str = None) -> Path:
    """Store schedules beside the active config file."""
    return get_config_path(config_path).parent / "schedules.yaml"


def get_usage_log_path(config_path: str = None) -> Path:
    """Store usage logs beside the active config file."""
    return get_config_path(config_path).parent / "usage_log.csv"


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file."""
    config_path = get_config_path(config_path)

    default_config = {
        "ac_host": "",
        "last_ac_host": "",
        "ac_port": 2878,
        "token": "",
        "web_port": 8080,
        "web_host": "0.0.0.0",
    }

    if config_path.exists():
        with open(config_path) as f:
            file_config = yaml.safe_load(f) or {}
        default_config.update(file_config)

    # Older config files only had ac_host. Keep a durable last-known host so
    # the setup screen can offer reconnect even if the active host is cleared.
    if default_config.get("ac_host") and not default_config.get("last_ac_host"):
        default_config["last_ac_host"] = default_config["ac_host"]

    return default_config


def save_config(cfg: dict, config_path: str = None):
    """Save configuration to YAML file."""
    config_path = get_config_path(config_path)
    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)


def _timer_attr_name(timer_type: str) -> str:
    return "on_timer" if timer_type == "on" else "off_timer"


def _status_with_app_timers() -> dict:
    """Return status with app-side one-shot timer estimates."""
    status = ac.status.to_dict()
    if scheduler:
        for timer_type in ("on", "off"):
            status[_timer_attr_name(timer_type)] = scheduler.get_timer_minutes(timer_type)
    if usage_logger:
        usage_logger.observe(status)
    return status


def init_app(cfg: dict = None, config_path: str = None):
    """Initialize the AC connection and scheduler."""
    global ac, scheduler, usage_logger, config, schedule_path, usage_log_path, config_file_path

    if cfg:
        config = cfg
    else:
        config = load_config(config_path)

    config_file_path = get_config_path(config_path)
    schedule_path = get_schedule_path(config_path)
    usage_log_path = get_usage_log_path(config_path)
    usage_logger = UsageLogger(usage_log_path)
    usage_logger.ensure_file()

    if config.get("ac_host") or config.get("last_ac_host"):
        host = config.get("ac_host") or config.get("last_ac_host")
        ac = SamsungACProtocol(
            host=host,
            port=config.get("ac_port", 2878),
            token=config.get("token", ""),
        )
        if ac.connect():
            config["ac_host"] = host
            config["last_ac_host"] = host
            save_config(config, config_file_path)
            logger.info(f"Connected to AC at {host}")
        else:
            logger.warning(f"Could not connect to AC at {host}")
    else:
        logger.info("No AC host configured - use /api/discover or set ac_host in config.yaml")
        ac = None

    if ac:
        scheduler = ACScheduler(ac, schedule_path=schedule_path)
    else:
        scheduler = None


def _remember_host(host: str):
    """Persist the active and last-used AC host."""
    if not host:
        return
    config["ac_host"] = host
    config["last_ac_host"] = host
    save_config(config, config_file_path)


def _configured_host() -> str:
    """Return the active host, falling back to the last successful host."""
    return config.get("ac_host") or config.get("last_ac_host") or ""


def _connect_to_host(host: str) -> bool:
    """Connect to a host and start the scheduler if successful."""
    global ac, scheduler
    if not host:
        return False

    if ac:
        ac.disconnect()

    candidate = SamsungACProtocol(
        host=host,
        port=config.get("ac_port", 2878),
        token=config.get("token", ""),
    )
    ok = candidate.connect()
    ac = candidate
    if ok:
        _remember_host(host)
        if scheduler:
            scheduler.shutdown()
        scheduler = ACScheduler(ac, schedule_path=schedule_path)
        # Wait a moment for device discovery
        time.sleep(2)
    elif scheduler:
        scheduler.shutdown()
        scheduler = None
    return ok


def _maybe_reconnect():
    """Try reconnecting to the configured host after boot/network interruptions."""
    global last_reconnect_attempt
    host = _configured_host()
    if not host:
        return
    if ac and ac.connected:
        return

    now = time.time()
    if now - last_reconnect_attempt < RECONNECT_RETRY_SECONDS:
        return
    last_reconnect_attempt = now
    logger.info(f"Trying to reconnect to AC at {host}")
    _connect_to_host(host)


# --- Web UI routes ---

@app.route("/")
def index():
    return render_template("index.html")


# --- REST API ---

@app.route("/api/status")
def api_status():
    _maybe_reconnect()
    if ac is None:
        return jsonify({
            "error": "AC not connected",
            "connected": False,
            "configured_host": _configured_host(),
        })
    status = _status_with_app_timers()
    status["configured_host"] = _configured_host()
    return jsonify(status)


@app.route("/api/power", methods=["POST"])
def api_power():
    if ac is None:
        return jsonify({"error": "AC not connected"}), 503
    data = request.json or {}
    on = data.get("on", True)
    ok = ac.set_power(on)
    return jsonify({"ok": ok, "power": "On" if on else "Off"})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    if ac is None:
        return jsonify({"error": "AC not connected"}), 503
    data = request.json or {}
    mode = data.get("mode", "Auto")
    ok = ac.set_mode(mode)
    return jsonify({"ok": ok, "mode": mode})


@app.route("/api/temperature", methods=["POST"])
def api_temperature():
    if ac is None:
        return jsonify({"error": "AC not connected"}), 503
    data = request.json or {}
    temp = int(data.get("temp", 24))
    ok = ac.set_temperature(temp)
    return jsonify({"ok": ok, "temp": temp})


@app.route("/api/fan", methods=["POST"])
def api_fan():
    if ac is None:
        return jsonify({"error": "AC not connected"}), 503
    data = request.json or {}
    speed = data.get("speed", "Auto")
    ok = ac.set_fan_speed(speed)
    return jsonify({"ok": ok, "speed": speed})


@app.route("/api/swing", methods=["POST"])
def api_swing():
    if ac is None:
        return jsonify({"error": "AC not connected"}), 503
    data = request.json or {}
    mode = data.get("mode", "Off")
    ok = ac.set_swing(mode)
    return jsonify({"ok": ok, "swing": mode})


@app.route("/api/timer", methods=["POST"])
def api_timer():
    """Set an app-side one-shot on/off timer (in minutes)."""
    if ac is None or scheduler is None:
        return jsonify({"error": "AC not connected"}), 503
    data = request.json or {}
    timer_type = data.get("type", "off")  # "on" or "off"
    minutes = int(data.get("minutes", 0))
    ok = scheduler.set_app_timer(timer_type, minutes)
    return jsonify({"ok": ok, "type": timer_type, "minutes": minutes})


@app.route("/api/schedule", methods=["GET"])
def api_schedule_list():
    if scheduler is None:
        return jsonify({"error": "Scheduler not available"}), 503
    return jsonify({"schedules": scheduler.get_schedules()})


@app.route("/api/schedule", methods=["POST"])
def api_schedule_add():
    if scheduler is None:
        return jsonify({"error": "Scheduler not available"}), 503
    data = request.json or {}
    action = data.get("action", "power_off")
    hour = int(data.get("hour", 0))
    minute = int(data.get("minute", 0))
    days = data.get("days", "daily")
    params = data.get("params", {})
    schedule_id = scheduler.add_schedule(action, hour, minute, days, params)
    return jsonify({"ok": True, "id": schedule_id})


@app.route("/api/schedule/<schedule_id>", methods=["DELETE"])
def api_schedule_remove(schedule_id):
    if scheduler is None:
        return jsonify({"error": "Scheduler not available"}), 503
    ok = scheduler.remove_schedule(schedule_id)
    return jsonify({"ok": ok})


@app.route("/api/discover", methods=["POST"])
def api_discover():
    """Scan the network for Samsung AC units."""
    data = request.json or {}
    network = data.get("network", None)
    devices = scan_network(network)
    return jsonify({"devices": devices, "network": network or get_local_network()})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Connect to a specific AC unit."""
    data = request.json or {}
    host = data.get("host", "")
    if not host:
        return jsonify({"error": "No host specified"}), 400

    ok = _connect_to_host(host)
    return jsonify({"ok": ok, "host": host})


@app.route("/api/reconnect", methods=["POST"])
def api_reconnect():
    """Reconnect to the active or last-used AC unit."""
    host = _configured_host()
    if not host:
        return jsonify({"error": "No previous AC host saved"}), 400
    ok = _connect_to_host(host)
    return jsonify({"ok": ok, "host": host})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global ac, scheduler
    if ac:
        ac.disconnect()
    if scheduler:
        scheduler.shutdown()
        scheduler = None
    return jsonify({"ok": True})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force a status refresh from the AC."""
    if ac is None:
        return jsonify({"error": "AC not connected"}), 503
    ac.request_status()
    time.sleep(1)
    return jsonify(_status_with_app_timers())


@app.route("/api/usage-log")
def api_usage_log():
    """Download the AC usage session log as CSV."""
    path = usage_log_path or get_usage_log_path()
    logger_instance = usage_logger or UsageLogger(path)
    logger_instance.ensure_file()
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name="samsung_ac_usage_log.csv",
    )


def run(config_path: str = None):
    """Start the web application."""
    cfg = load_config(config_path)
    init_app(cfg, config_path=config_path)
    app.run(
        host=cfg.get("web_host", "0.0.0.0"),
        port=cfg.get("web_port", 8080),
        debug=False,
        use_reloader=False,
    )
