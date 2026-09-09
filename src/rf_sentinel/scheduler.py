"""Послідовний continuous runner зі станом, recovery delay і graceful stop."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Callable

from rf_sentinel.acquisition import run_continuous_loop
from rf_sentinel.errors import SentinelError
from rf_sentinel.health import AcquisitionHealth, write_health_snapshot

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
