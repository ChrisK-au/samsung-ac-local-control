"""Usage-session CSV logging for observed AC power cycles."""

import csv
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_CSV_LINES = 500
MAX_DATA_ROWS = MAX_CSV_LINES - 1
CSV_FIELDS = [
    "start_time",
    "end_time",
    "duration_minutes",
    "start_mode",
    "end_mode",
    "modes_used",
    "start_target_temp",
    "end_target_temp",
    "min_target_temp",
    "max_target_temp",
    "start_room_temp",
    "end_room_temp",
    "min_room_temp",
    "max_room_temp",
    "start_fan_speed",
    "end_fan_speed",
    "fan_speeds_used",
    "change_count",
]


class UsageLogger:
    """Track observed on/off sessions and append completed sessions to CSV."""

    def __init__(self, log_path: str | Path, max_rows: int = MAX_DATA_ROWS):
        self.log_path = Path(log_path)
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._session: dict[str, Any] | None = None
        self._last_user_state: tuple[Any, Any, Any] | None = None

    def observe(self, status: dict):
        """Observe a status snapshot from the AC."""
        if not status.get("connected"):
            return

        power = status.get("power")
        now = datetime.now()

        with self._lock:
            if power == "On":
                if self._session is None:
                    self._start_session(status, now)
                else:
                    self._update_session(status)
                return

            if power == "Off" and self._session is not None:
                self._finish_session(status, now)

    def ensure_file(self):
        """Create the CSV file with headers if it does not already exist."""
        with self._lock:
            self._ensure_file_unlocked()

    def _start_session(self, status: dict, now: datetime):
        target_temp = _int_or_none(status.get("target_temp"))
        room_temp = _room_temp_or_none(status.get("current_temp"))
        mode = _clean_value(status.get("mode"))
        fan_speed = _clean_value(status.get("fan_speed"))

        self._session = {
            "start_time": now,
            "start_mode": mode,
            "end_mode": mode,
            "modes_used": _ordered_values(mode),
            "start_target_temp": target_temp,
            "end_target_temp": target_temp,
            "min_target_temp": target_temp,
            "max_target_temp": target_temp,
            "start_room_temp": room_temp,
            "end_room_temp": room_temp,
            "min_room_temp": room_temp,
            "max_room_temp": room_temp,
            "start_fan_speed": fan_speed,
            "end_fan_speed": fan_speed,
            "fan_speeds_used": _ordered_values(fan_speed),
            "change_count": 0,
        }
        self._last_user_state = (mode, target_temp, fan_speed)
        logger.info("Started AC usage session")

    def _update_session(self, status: dict):
        if self._session is None:
            return

        mode = _clean_value(status.get("mode"))
        target_temp = _int_or_none(status.get("target_temp"))
        room_temp = _room_temp_or_none(status.get("current_temp"))
        fan_speed = _clean_value(status.get("fan_speed"))

        current_user_state = (mode, target_temp, fan_speed)
        if self._last_user_state is not None and current_user_state != self._last_user_state:
            self._session["change_count"] += 1
        self._last_user_state = current_user_state

        self._session["end_mode"] = mode
        _append_unique(self._session["modes_used"], mode)
        self._session["end_target_temp"] = target_temp
        self._session["min_target_temp"] = _min_optional(self._session["min_target_temp"], target_temp)
        self._session["max_target_temp"] = _max_optional(self._session["max_target_temp"], target_temp)
        self._session["end_room_temp"] = room_temp
        self._session["min_room_temp"] = _min_optional(self._session["min_room_temp"], room_temp)
        self._session["max_room_temp"] = _max_optional(self._session["max_room_temp"], room_temp)
        self._session["end_fan_speed"] = fan_speed
        _append_unique(self._session["fan_speeds_used"], fan_speed)

    def _finish_session(self, status: dict, now: datetime):
        if self._session is None:
            return

        self._update_session(status)
        start_time = self._session["start_time"]
        duration_minutes = int((now - start_time).total_seconds() // 60)
        row = {
            "start_time": _format_datetime(start_time),
            "end_time": _format_datetime(now),
            "duration_minutes": duration_minutes,
            "start_mode": self._session["start_mode"],
            "end_mode": self._session["end_mode"],
            "modes_used": "|".join(self._session["modes_used"]),
            "start_target_temp": _format_optional(self._session["start_target_temp"]),
            "end_target_temp": _format_optional(self._session["end_target_temp"]),
            "min_target_temp": _format_optional(self._session["min_target_temp"]),
            "max_target_temp": _format_optional(self._session["max_target_temp"]),
            "start_room_temp": _format_optional(self._session["start_room_temp"]),
            "end_room_temp": _format_optional(self._session["end_room_temp"]),
            "min_room_temp": _format_optional(self._session["min_room_temp"]),
            "max_room_temp": _format_optional(self._session["max_room_temp"]),
            "start_fan_speed": self._session["start_fan_speed"],
            "end_fan_speed": self._session["end_fan_speed"],
            "fan_speeds_used": "|".join(self._session["fan_speeds_used"]),
            "change_count": self._session["change_count"],
        }

        self._append_row(row)
        self._session = None
        self._last_user_state = None
        logger.info("Finished AC usage session lasting %s minute(s)", duration_minutes)

    def _append_row(self, row: dict):
        self._ensure_file_unlocked()
        rows = self._read_rows_unlocked()
        rows.append(row)
        rows = rows[-self.max_rows:]
        self._write_rows_unlocked(rows)

    def _ensure_file_unlocked(self):
        if self.log_path.exists():
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_rows_unlocked([])

    def _read_rows_unlocked(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        try:
            with open(self.log_path, newline="") as f:
                return list(csv.DictReader(f))
        except Exception as e:
            logger.error("Failed to read usage log %s: %s", self.log_path, e)
            return []

    def _write_rows_unlocked(self, rows: list[dict]):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def _clean_value(value: Any) -> str:
    return "" if value is None else str(value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _room_temp_or_none(value: Any) -> int | None:
    temp = _int_or_none(value)
    if temp is None or temp <= 0:
        return None
    return temp


def _ordered_values(value: str) -> list[str]:
    return [value] if value else []


def _append_unique(values: list[str], value: str):
    if value and value not in values:
        values.append(value)


def _min_optional(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_optional(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_optional(value: Any) -> str:
    return "" if value is None else str(value)
