"""Sequential fixed-delay scheduler; knows only an application callable."""

import logging
from threading import Event
from typing import Callable

from rf_sentinel.errors import SentinelError

logger = logging.getLogger(__name__)


def run_schedule(run_survey: Callable, interval_seconds: float, stop: Event) -> None:
    if not 0 < interval_seconds <= 86400:
        raise ValueError("Invalid scheduler interval")
    while not stop.is_set():
        try:
            outcome = run_survey()
            logger.info("Survey completed: scan=%s notification=%s",
                        outcome.scan_status, outcome.notification_status)
        except SentinelError:
            logger.error("Survey failed; retrying at the next interval")
        # Fixed delay after completion prevents overlap and catch-up retry storms.
        stop.wait(interval_seconds)
