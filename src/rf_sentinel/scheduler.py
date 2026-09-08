"""Послідовний continuous runner зі станом, recovery delay і graceful stop."""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Callable

from rf_sentinel.errors import SentinelError

logger = logging.getLogger(__name__)


@dataclass
class HealthState:
    application_started_at: str
    last_survey_started_at: str | None = None
    last_successful_survey_at: str | None = None
    last_telegram_success_at: str | None = None
    consecutive_survey_failures: int = 0
    consecutive_telegram_failures: int = 0
    total_completed_surveys: int = 0
    total_failed_surveys: int = 0
    current_state: str = "starting"
    current_artifact_dir: str | None = None

    @classmethod
    def started(cls) -> "HealthState":
        return cls(datetime.now(UTC).isoformat())

    def record(self, outcome) -> None:
        now = datetime.now(UTC).isoformat()
        self.last_survey_started_at = outcome.started_at
        self.current_artifact_dir = (
            outcome.artifact_dir.name if outcome.artifact_dir is not None else None
        )
        if outcome.scan_status == "success":
            self.total_completed_surveys += 1
            self.consecutive_survey_failures = 0
            self.last_successful_survey_at = now
            self.current_state = "running"
        else:
            self.total_failed_surveys += 1
            self.consecutive_survey_failures += 1
            self.current_state = "recovering"
        if outcome.notification_status == "sent":
            self.consecutive_telegram_failures = 0
            self.last_telegram_success_at = now
        elif outcome.notification_status == "failed":
            self.consecutive_telegram_failures += 1


def write_status(path: Path, state: HealthState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(state), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def run_continuous(run_survey: Callable, recovery_seconds: float, stop: Event,
                   status_path: Path, state: HealthState | None = None) -> HealthState:
    if not 0 < recovery_seconds <= 3600:
        raise ValueError("Некоректна затримка відновлення")
    state = state or HealthState.started()
    state.current_state = "running"
    write_status(status_path, state)
    try:
        while not stop.is_set():
            state.current_state = "scanning"
            state.last_survey_started_at = datetime.now(UTC).isoformat()
            write_status(status_path, state)
            try:
                outcome = run_survey()
            except SentinelError:
                logger.error("Цикл завершився помилкою application; повтор після затримки відновлення")
                state.total_failed_surveys += 1
                state.consecutive_survey_failures += 1
                state.current_state = "recovering"
                write_status(status_path, state)
                if stop.wait(recovery_seconds):
                    break
                continue
            state.record(outcome)
            write_status(status_path, state)
            logger.info(
                "Цикл завершено: сканування=%s, Telegram=%s, успішних=%d, невдалих=%d",
                outcome.scan_status, outcome.notification_status,
                state.total_completed_surveys, state.total_failed_surveys,
            )
            if outcome.scan_status != "success" and stop.wait(recovery_seconds):
                break
    finally:
        state.current_state = "stopped"
        write_status(status_path, state)
        logger.info("Безперервний monitoring зупинено коректно")
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
