import subprocess
import signal
import sys
from threading import Barrier, Thread
from datetime import UTC, datetime, timedelta

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
    monkeypatch.setenv("RF_SENTINEL_SURVEY_RECOVERY_DELAY_SECONDS", "7")
    calls = []
    monkeypatch.setattr(
        "rf_sentinel.scheduler.run_continuous",
        lambda run, recovery, stop, status, notify_status=None:
            calls.append((run, recovery, stop, status, notify_status)),
    )
    assert main(["schedule"]) == 0
    assert calls[0][1] == 7
    assert calls[0][3] == tmp_path / "status" / "health.json"
    assert callable(calls[0][4])
    assert calls[0][0].__self__.send_failure_reports is False


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
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr("rf_sentinel.scheduler.run_continuous", interrupt)
    assert main(["schedule"]) == 130


def test_report_scheduler_generates_without_telegram(monkeypatch, tmp_path):
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("RF_SENTINEL_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("RF_SENTINEL_DATA_DIR", str(tmp_path))
    captured = {}

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            captured["notifier"] = args[1]

    def forbidden_notifier(*args, **kwargs):
        raise AssertionError("Telegram notifier must not be initialized")

    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler",
                        lambda runner, stop: captured.update(runner=runner))
    monkeypatch.setattr("rf_sentinel.telegram.TelegramNotifier", forbidden_notifier)
    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)

    assert main(["report-schedule"]) == 0
    assert captured["notifier"] is None
    assert isinstance(captured["runner"], FakeRunner)


def test_sigterm_requests_graceful_interrupt():
    with pytest.raises(KeyboardInterrupt):
        _shutdown_signal(signal.SIGTERM, None)


def test_acquisition_composes_async_sink_over_sqlite(monkeypatch, tmp_path):
    from rf_sentinel.acquisition import AsyncMeasurementSink, SpectrumSweep
    from rf_sentinel.application import run_acquisition
    from rf_sentinel.config import Settings
    from rf_sentinel.storage import SQLiteMeasurementSink

    now = datetime(2026, 9, 8, tzinfo=UTC)
    sweep = SpectrumSweep(now, now + timedelta(seconds=1), 1, 24e6, 26e6,
                          (24.5e6, 25.5e6), 1e6, (-40.0, -50.0), "test")
    captured = {}

    class FakeWorker:
        def __init__(self, source, sink, *args):
            captured["sink"] = sink

        def run(self):
            assert captured["sink"].store_sweep(sweep).status == "accepted"
            captured["sink"].close()

    monkeypatch.setattr("rf_sentinel.observability.configure_operational_logging",
                        lambda *args: None)
    monkeypatch.setattr("rf_sentinel.application.signal.signal", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.application.signal.getsignal", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args: object())

    assert run_acquisition(Settings(data_dir=tmp_path)) == 0
    assert isinstance(captured["sink"], AsyncMeasurementSink)
    assert isinstance(captured["sink"]._downstream, SQLiteMeasurementSink)
    assert captured["sink"].persisted_count == 1

    stored = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    try:
        assert stored.fetch_sweep(sweep.sweep_id) == sweep
    finally:
        stored.close()


def test_acquisition_and_report_incident_use_independent_sqlite_connections(tmp_path):
    from rf_sentinel.acquisition import AsyncMeasurementSink, SpectrumSweep
    from rf_sentinel.scheduler import ScheduledReportRunner
    from rf_sentinel.storage import SQLiteMeasurementSink

    now = datetime(2026, 9, 8, tzinfo=UTC)
    sweep = SpectrumSweep(now, now + timedelta(seconds=1), 1, 24e6, 26e6,
                          (24.5e6, 25.5e6), 1e6, (-40.0, -50.0), "test")
    acquisition_storage = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    report_storage = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    acquisition_sink = AsyncMeasurementSink(acquisition_storage)
    runner = ScheduledReportRunner(object(), None, tmp_path, storage=report_storage)
    barrier = Barrier(2)
    errors = []

    def write_sweep():
        try:
            barrier.wait()
            receipt = acquisition_sink.store_sweep(sweep)
            acquisition_sink.flush(timeout=5)
            assert receipt.status == "persisted"
        except BaseException as error:
            errors.append(error)

    def write_incident():
        try:
            barrier.wait()
            runner._incident("report_generation", "synthetic report failure", "retry_scheduled")
        except BaseException as error:
            errors.append(error)

    acquisition_thread = Thread(target=write_sweep)
    report_thread = Thread(target=write_incident)
    acquisition_thread.start()
    report_thread.start()
    acquisition_thread.join(timeout=10)
    report_thread.join(timeout=10)
    try:
        assert not acquisition_thread.is_alive()
        assert not report_thread.is_alive()
        assert errors == []
        assert acquisition_storage.fetch_sweep(sweep.sweep_id) == sweep
        assert len(report_storage.query_incidents()) == 1
    finally:
        acquisition_sink.close(timeout=5)
        acquisition_storage.close()
        report_storage.close()
