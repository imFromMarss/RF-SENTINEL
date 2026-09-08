import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rf_sentinel.errors import SentinelError
from rf_sentinel.scheduler import (
    FAILURE_NOTIFICATION,
    RECOVERY_NOTIFICATION,
    HealthState,
    run_continuous,
)
from rf_sentinel.workflow import SurveyOutcome


class Stop:
    def __init__(self, after):
        self.after = after
        self.checks = 0
        self.delays = []

    def is_set(self):
        self.checks += 1
        return self.checks > self.after

    def wait(self, seconds):
        self.delays.append(seconds)
        return False


def test_continuous_runner_starts_next_success_without_delay(tmp_path):
    outcomes = iter((
        SurveyOutcome("success", "sent", Path("one"), "2026-09-08T00:00:00+00:00"),
        SurveyOutcome("success", "failed", Path("two"), "2026-09-08T00:30:00+00:00"),
    ))
    stop = Stop(2)
    state = run_continuous(lambda: next(outcomes), 60, stop, tmp_path / "status.json")
    assert stop.delays == []
    assert state.total_completed_surveys == 2
    assert state.total_failed_surveys == 0
    assert state.consecutive_telegram_failures == 1
    assert state.current_state == "stopped"
    assert json.loads((tmp_path / "status.json").read_text())["total_completed_surveys"] == 2


def test_scan_failure_waits_before_recovery(tmp_path):
    outcomes = iter((
        SurveyOutcome("scan_failed", "disabled", Path("bad"), "2026-09-08T00:00:00+00:00"),
        SurveyOutcome("success", "sent", Path("good"), "2026-09-08T00:01:00+00:00"),
    ))
    stop = Stop(2)
    state = run_continuous(lambda: next(outcomes), 45, stop, tmp_path / "status.json")
    assert stop.delays == [45]
    assert state.total_failed_surveys == 1
    assert state.total_completed_surveys == 1
    assert state.consecutive_survey_failures == 0


def test_application_error_is_safe_and_retried(tmp_path, caplog):
    calls = []
    stop = Stop(2)
    def run():
        calls.append(1)
        if len(calls) == 1:
            raise SentinelError("synthetic-sensitive-value")
        return SurveyOutcome("success", "disabled", Path("ok"),
                             datetime.now(UTC).isoformat())
    state = run_continuous(run, 20, stop, tmp_path / "status.json")
    assert stop.delays == [20]
    assert state.total_failed_surveys == 1
    assert "synthetic-sensitive-value" not in caplog.text


def test_repeated_failures_wait_each_time_and_notify_transitions_only(tmp_path):
    outcomes = iter((
        SurveyOutcome("scan_failed", "suppressed", Path("bad-1"),
                      "2026-09-08T00:00:00+00:00"),
        SurveyOutcome("scan_failed", "suppressed", Path("bad-2"),
                      "2026-09-08T00:01:00+00:00"),
        SurveyOutcome("success", "sent", Path("good"),
                      "2026-09-08T00:02:00+00:00"),
    ))
    notifications = []
    stop = Stop(3)

    def notify(message):
        notifications.append(message)
        return "sent"

    state = run_continuous(
        lambda: next(outcomes), 60, stop, tmp_path / "status.json",
        notify_status=notify,
    )

    assert stop.delays == [60, 60]
    assert notifications == [FAILURE_NOTIFICATION, RECOVERY_NOTIFICATION]
    assert state.total_failed_surveys == 2
    assert state.total_completed_surveys == 1
    assert state.consecutive_survey_failures == 0


def test_shutdown_interrupts_recovery_wait_and_persists_stopped(tmp_path):
    class InterruptingStop(Stop):
        def wait(self, seconds):
            self.delays.append(seconds)
            raise KeyboardInterrupt

    stop = InterruptingStop(10)
    with pytest.raises(KeyboardInterrupt):
        run_continuous(
            lambda: SurveyOutcome("scan_failed", "suppressed"),
            60, stop, tmp_path / "status.json",
        )

    assert stop.delays == [60]
    assert json.loads((tmp_path / "status.json").read_text())["current_state"] == "stopped"
