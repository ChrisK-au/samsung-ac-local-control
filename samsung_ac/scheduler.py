"""
Scheduler for timed AC actions.

Supports both:
1. App-side one-shot timers (turn on/off after N minutes)
2. App-side schedules (APScheduler) - more flexible, needs server running
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)


class ACScheduler:
    def __init__(self, ac_protocol, schedule_path: str | Path = None):
        self.ac = ac_protocol
        self.schedule_path = Path(schedule_path) if schedule_path else Path(__file__).parent.parent / "schedules.yaml"
        self.scheduler = BackgroundScheduler()
        self.scheduler.start()
        self._schedules: dict[str, dict] = {}
        self._timers: dict[str, dict] = {}
        self._load_schedules()

    def shutdown(self):
        self.scheduler.shutdown(wait=False)

    def get_schedules(self) -> list[dict]:
        """Return all active schedules."""
        result = []
        for schedule_id, info in self._schedules.items():
            job = self.scheduler.get_job(schedule_id)
            result.append({
                "id": schedule_id,
                "action": info["action"],
                "params": info.get("params", {}),
                "time": info.get("time_str", ""),
                "days": info.get("days", "daily"),
                "active": job is not None,
            })
        return result

    def get_timer_minutes(self, timer_type: str) -> int:
        """Return estimated minutes remaining for an app-side one-shot timer."""
        info = self._timers.get(timer_type)
        if not info:
            return 0

        deadline = self._parse_deadline(info.get("deadline"))
        if deadline is None:
            return 0

        remaining_seconds = (deadline - datetime.now()).total_seconds()
        if remaining_seconds <= 0:
            self._timers.pop(timer_type, None)
            self._save_schedules()
            return 0

        return int((remaining_seconds + 59) // 60)

    def set_app_timer(self, timer_type: str, minutes: int) -> bool:
        """Set an app-side one-shot timer to power the AC on or off."""
        if timer_type not in ("on", "off"):
            logger.error(f"Invalid timer type: {timer_type}")
            return False

        self._remove_app_timer_job(timer_type)

        if minutes <= 0:
            self._timers.pop(timer_type, None)
            self._save_schedules()
            logger.info(f"Cancelled {timer_type} timer")
            return True

        action = "power_on" if timer_type == "on" else "power_off"
        deadline = datetime.now() + timedelta(minutes=minutes)
        self._register_app_timer(timer_type, action, deadline)
        self._save_schedules()
        logger.info(f"Set {timer_type} timer for {minutes} minute(s)")
        return True

    def add_schedule(
        self,
        action: str,
        hour: int,
        minute: int = 0,
        days: str = "daily",
        params: Optional[dict] = None,
    ) -> str:
        """
        Add a scheduled action.

        Args:
            action: One of "power_on", "power_off", "set_temp", "set_mode"
            hour: Hour (0-23)
            minute: Minute (0-59)
            days: "daily", "weekdays", "weekends", or comma-separated day names
                  (mon,tue,wed,thu,fri,sat,sun)
            params: Extra parameters, e.g. {"temp": 22} for set_temp

        Returns:
            Schedule ID
        """
        params = params or {}
        schedule_id = f"{action}_{hour:02d}{minute:02d}_{days}"
        self._register_schedule(schedule_id, action, hour, minute, days, params)
        self._save_schedules()
        logger.info(f"Added schedule: {schedule_id} at {hour:02d}:{minute:02d} ({days})")
        return schedule_id

    def _register_schedule(
        self,
        schedule_id: str,
        action: str,
        hour: int,
        minute: int,
        days: str,
        params: dict,
    ):
        """Register a schedule with APScheduler and the in-memory schedule list."""

        # Build cron day_of_week
        if days == "daily":
            dow = "*"
        elif days == "weekdays":
            dow = "mon-fri"
        elif days == "weekends":
            dow = "sat,sun"
        else:
            dow = days  # e.g. "mon,tue,wed"

        trigger = CronTrigger(hour=hour, minute=minute, day_of_week=dow)

        def job_func():
            self._execute_action(action, params)

        self.scheduler.add_job(
            job_func,
            trigger=trigger,
            id=schedule_id,
            replace_existing=True,
        )

        self._schedules[schedule_id] = {
            "action": action,
            "params": params,
            "hour": hour,
            "minute": minute,
            "time_str": f"{hour:02d}:{minute:02d}",
            "days": days,
        }

    def _register_app_timer(self, timer_type: str, action: str, deadline: datetime):
        """Register an app-side one-shot timer with APScheduler."""
        job_id = f"timer_{timer_type}"

        def job_func():
            self._execute_timer(timer_type)

        self.scheduler.add_job(
            job_func,
            trigger=DateTrigger(run_date=deadline),
            id=job_id,
            replace_existing=True,
        )

        self._timers[timer_type] = {
            "type": timer_type,
            "action": action,
            "deadline": deadline.isoformat(timespec="seconds"),
        }

    def _remove_app_timer_job(self, timer_type: str):
        """Remove a one-shot timer job if it exists."""
        try:
            self.scheduler.remove_job(f"timer_{timer_type}")
        except Exception:
            pass

    def remove_schedule(self, schedule_id: str) -> bool:
        """Remove a schedule by ID."""
        removed = schedule_id in self._schedules
        try:
            self.scheduler.remove_job(schedule_id)
        except Exception as e:
            if not removed:
                logger.error(f"Failed to remove schedule {schedule_id}: {e}")
                return False

        self._schedules.pop(schedule_id, None)
        self._save_schedules()
        logger.info(f"Removed schedule: {schedule_id}")
        return True

    def _load_schedules(self):
        """Load persisted schedules from disk and register them."""
        if not self.schedule_path.exists():
            return

        try:
            with open(self.schedule_path) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Failed to load schedules from {self.schedule_path}: {e}")
            return

        schedules = data.get("schedules", [])
        for item in schedules:
            try:
                action = item["action"]
                hour = int(item["hour"])
                minute = int(item.get("minute", 0))
                days = item.get("days", "daily")
                params = item.get("params", {}) or {}
                schedule_id = item.get("id") or f"{action}_{hour:02d}{minute:02d}_{days}"
                self._register_schedule(schedule_id, action, hour, minute, days, params)
            except Exception as e:
                logger.error(f"Skipping invalid schedule {item}: {e}")

        if self._schedules:
            logger.info(f"Loaded {len(self._schedules)} schedule(s) from {self.schedule_path}")

        timers = data.get("timers", [])
        for item in timers:
            try:
                timer_type = item["type"]
                action = item["action"]
                deadline = self._parse_deadline(item.get("deadline"))
                if deadline is None or deadline <= datetime.now():
                    continue
                self._register_app_timer(timer_type, action, deadline)
            except Exception as e:
                logger.error(f"Skipping invalid timer {item}: {e}")

        if self._timers:
            logger.info(f"Loaded {len(self._timers)} app timer(s) from {self.schedule_path}")

    def _save_schedules(self):
        """Persist current schedules to disk."""
        schedules = []
        for schedule_id, info in self._schedules.items():
            hour = info.get("hour")
            minute = info.get("minute")
            if hour is None or minute is None:
                hour, minute = self._parse_time(info.get("time_str", "00:00"))
            schedules.append({
                "id": schedule_id,
                "action": info["action"],
                "hour": int(hour),
                "minute": int(minute),
                "days": info.get("days", "daily"),
                "params": info.get("params", {}),
            })

        try:
            self.schedule_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.schedule_path, "w") as f:
                yaml.dump(
                    {"schedules": schedules, "timers": list(self._timers.values())},
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )
        except Exception as e:
            logger.error(f"Failed to save schedules to {self.schedule_path}: {e}")

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int, int]:
        hour_str, minute_str = time_str.split(":", 1)
        return int(hour_str), int(minute_str)

    @staticmethod
    def _parse_deadline(deadline: str | None) -> datetime | None:
        if not deadline:
            return None
        try:
            return datetime.fromisoformat(deadline)
        except ValueError:
            return None

    def _execute_timer(self, timer_type: str):
        """Execute and clear an app-side one-shot timer."""
        info = self._timers.get(timer_type)
        if not info:
            return

        self._execute_action(info["action"], {})
        self._timers.pop(timer_type, None)
        self._save_schedules()

    def _execute_action(self, action: str, params: dict):
        """Execute a scheduled action."""
        logger.info(f"Executing scheduled action: {action} with params {params}")
        try:
            if action == "power_on":
                self.ac.set_power(True)
                mode = params.get("mode")
                if mode:
                    self.ac.set_mode(mode)
                temp = params.get("temp")
                if temp and mode != "Wind":
                    self.ac.set_temperature(int(temp))
            elif action == "power_off":
                self.ac.set_power(False)
            elif action == "set_temp":
                temp = params.get("temp", 24)
                self.ac.set_temperature(int(temp))
            elif action == "set_mode":
                mode = params.get("mode", "Auto")
                self.ac.set_mode(mode)
            elif action == "set_fan":
                speed = params.get("speed", "Auto")
                self.ac.set_fan_speed(speed)
            else:
                logger.warning(f"Unknown action: {action}")
        except Exception as e:
            logger.error(f"Schedule action failed: {e}")

    # --- Convenience methods ---

    def set_off_at(self, hour: int, minute: int = 0, days: str = "daily") -> str:
        """Schedule AC to turn off at a specific time."""
        return self.add_schedule("power_off", hour, minute, days)

    def set_on_at(self, hour: int, minute: int = 0, days: str = "daily") -> str:
        """Schedule AC to turn on at a specific time."""
        return self.add_schedule("power_on", hour, minute, days)

    def set_native_off_timer(self, minutes: int) -> bool:
        """Set the AC's built-in off timer (runs on the AC itself)."""
        return self.ac.set_off_timer(minutes)

    def set_native_on_timer(self, minutes: int) -> bool:
        """Set the AC's built-in on timer (runs on the AC itself)."""
        return self.ac.set_on_timer(minutes)
