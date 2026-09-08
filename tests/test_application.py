import subprocess
import signal
import sys

import pytest

from rf_sentinel.application import _shutdown_signal, main


def test_main_returns_success_and_prints_identity(capsys):
    assert main() == 0
    assert capsys.readouterr().out == "RF Sentinel\n"


def test_application_runs_without_hardware(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "rf_sentinel"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == "RF Sentinel\n"
    assert result.stderr == ""


def test_survey_mode_composes_without_hardware(monkeypatch, capsys, tmp_path):
    from rf_sentinel.workflow import SurveyOutcome
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("RF_SENTINEL_DATA_DIR", str(tmp_path))
    calls = []
    def run(self):
        calls.append(self)
        return SurveyOutcome("success", "disabled")
    monkeypatch.setattr("rf_sentinel.workflow.SurveyWorkflow.run", run)
    assert main(["survey"]) == 0
    assert calls[0].notifier is None
    assert calls[0].data_dir == tmp_path
    assert "Огляд: success" in capsys.readouterr().out


def test_schedule_mode_uses_recovery_delay_and_status_path(monkeypatch, tmp_path):
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("RF_SENTINEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RF_SENTINEL_SURVEY_RECOVERY_SECONDS", "7")
    calls = []
    monkeypatch.setattr(
        "rf_sentinel.scheduler.run_continuous",
        lambda run, recovery, stop, status: calls.append((run, recovery, stop, status)),
    )
    assert main(["schedule"]) == 0
    assert calls[0][1] == 7
    assert calls[0][3] == tmp_path / "status.json"


def test_invalid_configuration_has_safe_output(monkeypatch, capsys):
    marker = "synthetic-sensitive-value"
    monkeypatch.setenv("RF_SENTINEL_REPORT_INTERVAL_MINUTES", marker)
    assert main(["survey"]) == 1
    output = capsys.readouterr()
    assert marker not in output.err + output.out
    assert "помилка конфігурації або огляду" in output.err


def test_survey_scan_failure_exit_status(monkeypatch, capsys):
    from rf_sentinel.workflow import SurveyOutcome
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr("rf_sentinel.workflow.SurveyWorkflow.run",
                        lambda self: SurveyOutcome("scan_failed", "disabled"))
    assert main(["survey"]) == 1
    assert "scan_failed" in capsys.readouterr().out


def test_scheduler_keyboard_interrupt_exit(monkeypatch):
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_CHAT_ID", "")
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr("rf_sentinel.scheduler.run_continuous", interrupt)
    assert main(["schedule"]) == 130


def test_sigterm_requests_graceful_interrupt():
    with pytest.raises(KeyboardInterrupt):
        _shutdown_signal(signal.SIGTERM, None)
