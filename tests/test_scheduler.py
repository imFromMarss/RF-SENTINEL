import json
from datetime import UTC, datetime
from pathlib import Path

from rf_sentinel.errors import SentinelError
from rf_sentinel.scheduler import HealthState, run_continuous
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
