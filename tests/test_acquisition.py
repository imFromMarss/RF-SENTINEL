from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from rf_sentinel.acquisition import (ErrorClassification, LatestSweepSink,
                                     SpectrumAcquisitionWorker, SpectrumSweep,
                                     SweepCoverage, SweepProfile, SweepProfileMetadata,
                                     SweepQuality)
from rf_sentinel.config import Settings
from rf_sentinel.errors import ConfigurationError, ScanError
from rf_sentinel.observability import AcquisitionObserver, OperationalFormatter
from rf_sentinel.rtl_power import RTLPowerScanner

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def frame(duration=7):
    return SpectrumSweep(NOW, NOW + timedelta(seconds=duration), duration,
                         24e6, 26e6, (24.5e6, 25.5e6), 1e6, (-40., -50.), "rtl_power")


@pytest.mark.parametrize("change", [
    {"powers": ()}, {"powers": (float("nan"), -40)}, {"duration_seconds": -1},
    {"started_at": NOW.replace(tzinfo=None)}, {"finished_at": NOW - timedelta(seconds=1)},
    {"frequencies_hz": (25.5e6, 24.5e6)}, {"bin_width_hz": 0}, {"status": "bad"},
])
def test_invalid_sweep(change):
    with pytest.raises(ValueError):
        replace(frame(), **change)


def test_durable_contract_supports_all_terminal_outcomes():
    partial = replace(frame(), status="partial",
                      coverage=SweepCoverage("partial", 4, 2, 0.5),
                      quality=SweepQuality("degraded", ("missing_bins",)))
    failed = SpectrumSweep(
        NOW, NOW + timedelta(seconds=1), 1, 0, 0, (), 0, (), "rtl_power",
        status="failed", error_classification=ErrorClassification("device", "timeout"),
        requested_profile=SweepProfileMetadata(24e6, 26e6, 1e6, 1, 1),
        coverage=SweepCoverage("none", 2, 0, 0),
        quality=SweepQuality("unavailable"), correlation_id="attempt-1")

    assert partial.outcome == "partial"
    assert partial.coverage.status == "partial"
    assert failed.outcome == "failed"
    assert failed.powers == ()
    assert failed.error_classification.code == "timeout"
    assert failed.schema_version == "spectrum-sweep.v1"


class ClockStop:
    def __init__(self):
        self.time = 0
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.time += seconds
        return self.stopped


@pytest.mark.parametrize("duration, expected", [(7, [53, 53]), (60, []), (62, [])])
def test_cadence_sink_no_overlap(tmp_path, duration, expected):
    clock = ClockStop()
    sink = LatestSweepSink()
    starts = []
    def acquire(profile):
        assert len(starts) == 0 or sink.latest is not None
        starts.append(clock.time)
        clock.time += duration
        if len(starts) == 2:
            clock.set()
        return frame(duration)
    observer = AcquisitionObserver(tmp_path / "health.json", SweepProfile(), 60, 60)
    SpectrumAcquisitionWorker(SimpleNamespace(acquire=acquire), sink, SweepProfile(),
                              observer, clock, monotonic=lambda: clock.time, now=lambda: NOW).run()
    assert starts == [0, max(60, duration)]
    assert clock.waits == expected
    assert sink.latest is not None
    assert sink.latest.powers == frame(duration).powers
    assert sink.latest.status == "success"
    assert sink.latest.sweep_id
    health = json.loads((tmp_path / "health.json").read_text())
    assert health["total_sweeps"] == 2
    assert health["application_status"] == "stopped"
    assert health["actual_bin_width_hz"] == 1e6
    assert health["cadence_budget_seconds"] == 60
    assert health["last_sweep_duration_seconds"] == duration
    assert health["last_sweep_cadence_seconds"] == max(60, duration)


def test_failure_recovery_and_secret_safe_logs(tmp_path, caplog):
    clock = ClockStop()
    calls = []
    def acquire(profile):
        calls.append(clock.time)
        if len(calls) < 3:
            raise ScanError("synthetic-secret https://private.invalid")
        clock.set()
        return frame(12)
    observer = AcquisitionObserver(tmp_path / "health.json", SweepProfile(), 10, 60)
    with caplog.at_level(logging.INFO):
        SpectrumAcquisitionWorker(SimpleNamespace(acquire=acquire), LatestSweepSink(),
                                  SweepProfile(), observer, clock,
                                  monotonic=lambda: clock.time).run()
    assert calls == [0, 60, 120]
    assert observer.state.failed_sweeps == 2
    assert observer.state.consecutive_sweep_failures == 0
    assert any(getattr(r, "context", {}).get("event") == "recovery_success" for r in caplog.records)
    output = "".join(OperationalFormatter().format(r) for r in caplog.records)
    assert "synthetic-secret" not in output
    assert "private.invalid" not in output
    assert "synthetic-secret" not in (tmp_path / "health.json").read_text()


@pytest.mark.parametrize("during_wait", [False, True])
def test_shutdown(tmp_path, during_wait):
    clock = ClockStop()
    def acquire(profile):
        if during_wait:
            raise ScanError("помилка")
        raise KeyboardInterrupt
    def wait(seconds):
        raise KeyboardInterrupt
    clock.wait = wait
    observer = AcquisitionObserver(tmp_path / "health.json", SweepProfile(), 10, 60)
    with pytest.raises(KeyboardInterrupt):
        SpectrumAcquisitionWorker(SimpleNamespace(acquire=acquire), LatestSweepSink(),
                                  SweepProfile(), observer, clock).run()
    assert clock.stopped
    assert observer.state.application_status == "stopped"


def test_atomic_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    observer = AcquisitionObserver(path, SweepProfile(), 10, 60)
    observer.start(NOW)
    before = path.read_bytes()
    def fail_replace(self, target):
        assert path.read_bytes() == before
        assert json.loads(self.read_text())["application_status"] == "acquiring"
        raise OSError("помилка")
    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        observer.sweep_started(NOW)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_single_shot_command(monkeypatch):
    from test_rtl_power import FakeProcess
    processes = []
    def spawn(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        kwargs["stdout"].seek(0)
        kwargs["stdout"].truncate()
        kwargs["stdout"].write(b"2026-09-08, 00:00:10, 24000000, 26000000, 1000000, 20, -40, -50\n")
        processes.append(process)
        return process
    monkeypatch.setattr("subprocess.Popen", spawn)
    sweep = RTLPowerScanner().acquire(SweepProfile(24_000_000, 26_000_000, 1_000_000))
    assert len(sweep.powers) == 2
    assert "-1" in processes[0].command
    assert "-e" not in processes[0].command


def test_lock_excludes_second_scanner(monkeypatch):
    import fcntl
    import tempfile
    lock = Path(tempfile.gettempdir()) / "rf-sentinel-rtl-254.lock"
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ScanError, match="використовується"):
            RTLPowerScanner(254).acquire(SweepProfile())


@pytest.mark.parametrize("env", [{"ACQUISITION_CADENCE_BUDGET_SECONDS": "nan"},
    {"ACQUISITION_RECOVERY_SECONDS": "0"}, {"LOG_BACKUPS": "0"},
    {"ACQUISITION_START_HZ": "1"}, {"ACQUISITION_BIN_HZ": "secret"}])
def test_configuration_bounds(env):
    with pytest.raises(ConfigurationError):
        Settings.from_env({"RF_SENTINEL_" + k: v for k, v in env.items()})


def test_acquisition_ignores_telegram_credentials():
    assert not Settings.from_env({"RF_SENTINEL_TELEGRAM_BOT_TOKEN": "invalid"},
                                 acquisition_only=True).telegram_enabled


def test_sink_failure_stops_without_retry(tmp_path):
    clock = ClockStop()
    observer = AcquisitionObserver(tmp_path / "health.json", SweepProfile(), 10, 60)
    def store(sweep):
        raise OSError("synthetic-secret")
    with pytest.raises(OSError):
        SpectrumAcquisitionWorker(SimpleNamespace(acquire=lambda p: frame()),
                                  SimpleNamespace(store_sweep=store), SweepProfile(),
                                  observer, clock).run()
    assert clock.waits == []
    assert observer.state.application_status == "failed"
    assert "synthetic-secret" not in (tmp_path / "health.json").read_text()


def test_preexisting_stop_does_not_acquire(tmp_path):
    clock = ClockStop()
    clock.set()
    observer = AcquisitionObserver(tmp_path / "health.json", SweepProfile(), 10, 60)
    SpectrumAcquisitionWorker(None, LatestSweepSink(), SweepProfile(), observer, clock).run()
    assert observer.state.total_sweeps == 0


def test_kill_fallback_reaps_child(monkeypatch):
    import subprocess
    from test_rtl_power import FakeProcess
    processes = []
    def spawn(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        process.returncode = None
        def terminate():
            process.terminated = True
        def wait(timeout=None):
            if timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            process.waited = True
        process.terminate = terminate
        process.wait = wait
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr("rf_sentinel.rtl_power.time.sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        RTLPowerScanner().acquire(SweepProfile())
    assert processes[0].terminated and processes[0].killed and processes[0].waited


def test_log_rotation_is_bounded(tmp_path):
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(tmp_path / "test.log", maxBytes=200, backupCount=2)
    handler.setFormatter(OperationalFormatter())
    try:
        for _ in range(20):
            handler.handle(logging.LogRecord("acquisition", logging.INFO, "", 0,
                                            "Прохід завершено", (), None))
    finally:
        handler.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["test.log", "test.log.1", "test.log.2"]


def test_cli_signal_cleanup(tmp_path, monkeypatch):
    import signal
    from rf_sentinel.application import main
    handlers = {}
    restored = {}
    monkeypatch.setattr(Settings, "from_env", lambda **kw: Settings(data_dir=tmp_path))
    monkeypatch.setattr(signal, "getsignal", lambda sig: f"previous-{sig}")
    def install(sig, handler):
        if callable(handler):
            handlers[sig] = handler
        else:
            restored[sig] = handler
    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr("rf_sentinel.observability.configure_operational_logging", lambda *args: None)
    def acquire(self, profile):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    monkeypatch.setattr(RTLPowerScanner, "acquire", acquire)
    assert main(["acquire"]) == 130
    assert set(restored) == {signal.SIGINT, signal.SIGTERM}
    assert json.loads((tmp_path / "status" / "health.json").read_text())["application_status"] == "stopped"


def test_acquisition_ignores_legacy_report_configuration():
    settings = Settings.from_env({"RF_SENTINEL_SURVEY_BIN_HZ": "invalid",
                                  "RF_SENTINEL_TELEGRAM_ATTEMPTS": "invalid"},
                                 acquisition_only=True)
    assert settings.acquisition_cadence_budget_seconds == 60
