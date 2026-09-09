"""Послідовний continuous runner зі станом, recovery delay і graceful stop."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Callable
from zoneinfo import ZoneInfo

from rf_sentinel.acquisition import run_continuous_loop
from rf_sentinel.errors import SentinelError
from rf_sentinel.health import AcquisitionHealth, write_health_snapshot
from rf_sentinel.reporting import (completed_calendar_day, completed_calendar_hour,
                                   generate_report_package)

logger = logging.getLogger(__name__)

FAILURE_NOTIFICATION = (
    "RF Sentinel: сканування завершилося помилкою. "
    "Система спробує відновити роботу автоматично."
)
RECOVERY_NOTIFICATION = "RF Sentinel: моніторинг спектра відновлено."


HealthState = AcquisitionHealth
write_status = write_health_snapshot


def run_continuous(run_survey: Callable, recovery_seconds: float, stop: Event,
                   status_path: Path, state: HealthState | None = None,
                   notify_status: Callable[[str], str] | None = None) -> HealthState:
    if not 0 < recovery_seconds <= 3600:
        raise ValueError("Некоректна затримка відновлення")
    state = state or HealthState.started()

    def start():
        state.current_state = "running"
        write_status(status_path, state)

    def cycle():
        state.current_state = "scanning"
        state.last_survey_started_at = datetime.now(UTC).isoformat()
        write_status(status_path, state)
        return run_survey()

    def result(outcome):
        failures_before_cycle = state.consecutive_survey_failures
        state.record(outcome)
        if outcome.scan_status != "success":
            if failures_before_cycle == 0 and notify_status is not None:
                state.record_notification(notify_status(FAILURE_NOTIFICATION))
            elif failures_before_cycle > 0:
                logger.info("Повторна помилка; Telegram alert пригнічено")
        elif failures_before_cycle > 0 and notify_status is not None:
            state.record_notification(notify_status(RECOVERY_NOTIFICATION))
        write_status(status_path, state)
        logger.info("Цикл завершено: сканування=%s, Telegram=%s, успішних=%d, невдалих=%d",
                    outcome.scan_status, outcome.notification_status,
                    state.total_completed_surveys, state.total_failed_surveys)
        return outcome.scan_status == "success"

    def failure(error):
        failures_before_cycle = state.consecutive_survey_failures
        logger.error("Цикл завершився помилкою application; повтор після затримки відновлення")
        state.total_failed_surveys += 1
        state.consecutive_survey_failures += 1
        state.current_state = "recovering"
        if failures_before_cycle == 0 and notify_status is not None:
            state.record_notification(notify_status(FAILURE_NOTIFICATION))
        elif failures_before_cycle > 0:
            logger.info("Повторна помилка; Telegram alert пригнічено")
        write_status(status_path, state)

    def shutdown(_failed):
        state.current_state = "stopped"
        write_status(status_path, state)
        logger.info("Безперервний monitoring зупинено коректно")

    run_continuous_loop(cycle, recovery_seconds, stop, start, result, failure, shutdown,
                        retry_exceptions=(SentinelError,))
    return state


def run_schedule(run_survey: Callable, interval_seconds: float, stop: Event) -> None:
    if not 0 < interval_seconds <= 86400:
        raise ValueError("Некоректний інтервал scheduler")
    while not stop.is_set():
        try:
            outcome = run_survey()
            logger.info("Огляд завершено: scan=%s, Telegram=%s",
                        outcome.scan_status, outcome.notification_status)
        except SentinelError:
            logger.error("Огляд завершився помилкою; наступна спроба після інтервалу")
        # Фіксована затримка не допускає overlap та catch-up storm.
        stop.wait(interval_seconds)


class ScheduledReportRunner:
    """Deliver completed calendar reports without owning acquisition.

    The delivery ledger is deliberately separate from the sweep database: the
    report scheduler only reads SQLite through ``report_engine`` and owns no
    acquisition or storage lifecycle.  A window is recorded after a complete
    delivery, which makes normal polling and clean restarts idempotent.
    """

    def __init__(self, report_engine, notifier, data_dir: str | Path,
                 *, timezone: str = "Europe/Kyiv", clock: Callable[[], datetime] | None = None,
                 delivery_attempts: int = 1, sleeper: Callable[[float], object] | None = None):
        if delivery_attempts < 1:
            raise ValueError("delivery_attempts must be positive")
        self.report_engine = report_engine
        self.notifier = notifier
        self.data_dir = Path(data_dir)
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.delivery_attempts = delivery_attempts
        self.sleeper = sleeper
        self.state_path = self.data_dir / "reports" / "scheduled" / "delivery-state.json"
        self._delivered = self._load_delivered()

    @staticmethod
    def _key(kind: str, start: datetime, end: datetime) -> str:
        return f"{kind}:{start.isoformat()}:{end.isoformat()}"

    def _load_delivered(self) -> set[str]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            delivered = value.get("delivered", [])
            if not isinstance(delivered, list) or not all(isinstance(item, str) for item in delivered):
                raise ValueError
            return set(delivered)
        except FileNotFoundError:
            return set()
        except (OSError, ValueError, json.JSONDecodeError):
            logger.error("Scheduled reports: delivery state unavailable; starting empty")
            return set()

    def _persist_delivered(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"delivered": sorted(self._delivered)}, indent=2) + "\n",
                              encoding="utf-8")
        temporary.replace(self.state_path)

    def _deliver_window(self, kind: str, start: datetime, end: datetime) -> bool:
        key = self._key(kind, start, end)
        if key in self._delivered:
            return False
        try:
            report = self.report_engine.build(start, end)
            destination = (self.data_dir / "reports" / "scheduled" / kind /
                           f"{start.astimezone(self.zone):%Y%m%dT%H%M%S}-"
                           f"{end.astimezone(self.zone):%Y%m%dT%H%M%S}")
            package = generate_report_package(report, destination, self.timezone)
            delivery = None
            for attempt in range(self.delivery_attempts):
                delivery = self.notifier.send_package(package)
                if getattr(delivery, "status", None) == "sent":
                    break
                if self.sleeper is not None and attempt + 1 < self.delivery_attempts:
                    self.sleeper(0)
            if getattr(delivery, "status", None) != "sent":
                logger.error("Scheduled %s report delivery failed", kind)
                return False
            self._delivered.add(key)
            self._persist_delivered()
            logger.info("Scheduled %s report delivered: %s–%s", kind, start, end)
            return True
        except Exception:
            # Derived report and transport failures must not affect acquisition
            # or prevent the next calendar boundary from being evaluated.
            logger.exception("Scheduled %s report failed", kind)
            return False

    def run_pending(self, now: datetime | None = None) -> tuple[str, ...]:
        """Deliver reports due at ``now`` and return the successful kinds."""
        now = now or self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scheduler clock must return a timezone-aware datetime")
        hour_start, hour_end = completed_calendar_hour(now, self.timezone)
        delivered = []
        if self._deliver_window("hourly", hour_start, hour_end):
            delivered.append("hourly")
        local = now.astimezone(self.zone)
        if local.hour == 0:
            day_start, day_end = completed_calendar_day(now, self.timezone)
            if self._deliver_window("daily", day_start, day_end):
                delivered.append("daily")
        return tuple(delivered)


def run_report_scheduler(runner: ScheduledReportRunner, stop: Event,
                         *, clock: Callable[[], datetime] | None = None) -> None:
    """Run the calendar report scheduler until ``stop`` is set.

    The wait is injectable so boundary behavior can be tested without wall-clock
    sleeping.  The scheduler never calls acquisition; it only invokes runner.
    """
    clock = clock or runner.clock
    while not stop.is_set():
        now = clock()
        runner.run_pending(now)
        local = now.astimezone(runner.zone)
        next_hour = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        delay = max(0.0, (next_hour - local).total_seconds())
        stop.wait(delay)
