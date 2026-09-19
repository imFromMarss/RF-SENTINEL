import subprocess
import sys
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Thread, enumerate as enumerate_threads

import pytest

from rf_sentinel.application import _StationProcessLock, run_station
from rf_sentinel.config import Settings


def _station_test_sweep():
    from rf_sentinel.acquisition import SpectrumSweep

    started = datetime(2026, 9, 18, tzinfo=UTC)
    return SpectrumSweep(
        started, started + timedelta(seconds=1), 1, 24_000_000, 24_250_000,
        (24_125_000,), 250_000, (-42.0,), "rtl_power",
    )


def test_station_composes_one_acquisition_and_report_runtime(monkeypatch, tmp_path):
    started = Event()
    report_ready = Event()
    captured = {}

    class FakeScanner:
        def __init__(self, *args, **kwargs):
            captured["scanner"] = self

    class FakeWorker:
        def __init__(self, scanner, sink, profile, observer, stop, *args, **kwargs):
            captured.update(worker=self, sink=sink, observer=observer, stop=stop)
            self.observer = observer
            self.sink = sink
            self.stop = stop

        def run(self):
            self.observer.state.total_sweeps = 1
            self.observer.state.application_status = "running"
            self.observer.save()
            started.set()
            report_ready.wait(2)
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, state=None, health_owner=None, **kwargs):
            captured.update(runner=self, state=state, health_owner=health_owner)
            self.state = state

    def fake_scheduler(runner, stop):
        assert started.wait(2)
        runner.state.report_status = "running"
        runner.state.total_completed_reports = 1
        runner.health_owner.save() if hasattr(runner, "health_owner") else None
        report_ready.set()
        stop.wait(2)

    class FakeRunnerWithOwner(FakeRunner):
        def __init__(self, *args, health_owner=None, **kwargs):
            super().__init__(*args, health_owner=health_owner, **kwargs)
            self.health_owner = health_owner

    def configure_logging(*args):
        captured["logging"] = captured.get("logging", 0) + 1
    monkeypatch.setattr("rf_sentinel.application.configure_logging", configure_logging)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", FakeScanner)
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunnerWithOwner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", fake_scheduler)
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 0
    assert captured["logging"] == 1
    assert captured["runner"].state is captured["observer"].state
    assert captured["runner"].health_owner is not None
    assert captured["scanner"] is not None
    assert captured["sink"].persisted_count == 0

    import json
    health = json.loads((tmp_path / "status" / "health.json").read_text())
    assert health["total_sweeps"] == 1
    assert health["report_status"] == "running"


def test_station_does_not_start_legacy_survey_path(monkeypatch, tmp_path):
    calls = []

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            calls.append("worker")
            self.stop = args[4]

        def run(self):
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            calls.append("reports")

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 0
    assert calls == ["worker", "reports"]
    import json
    summary = json.loads((tmp_path / "status" / "run-summary.json").read_text())
    assert summary["application"]["status"] == "stopped"
    assert summary["failures"]["status"] == "clean"
    assert summary["failures"]["components"] == []


def test_station_partial_startup_failure_closes_unstarted_async_sink(monkeypatch, tmp_path):
    acquisition = __import__("rf_sentinel.acquisition", fromlist=["AsyncMeasurementSink"])
    original_sink = acquisition.AsyncMeasurementSink
    captured = {}

    def sink_factory(*args, **kwargs):
        sink = original_sink(*args, **kwargs)
        captured["sink"] = sink
        return sink

    def fail_runner(*args, **kwargs):
        raise RuntimeError("report runner construction failed")

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.AsyncMeasurementSink", sink_factory)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", fail_runner)
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    with pytest.raises(RuntimeError, match="report runner construction failed"):
        run_station(Settings(data_dir=tmp_path))

    sink = captured["sink"]
    assert sink._writer_started is False
    assert not sink._writer.is_alive()
    assert sink._closed is True
    assert sink.telemetry_snapshot()["downstream_close_status"] == "complete"


def test_station_async_writer_terminal_failure_requests_stop(monkeypatch, tmp_path):
    captured = {}

    class FakeWorker:
        def __init__(self, _scanner, sink, _profile, _observer, stop, *args, **kwargs):
            self.sink = sink
            self.stop = stop
            captured["stop"] = stop

        def run(self):
            captured["receipt"] = self.sink.store_sweep(_station_test_sweep())
            assert self.stop.wait(2)

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    def fail_persistence(self, sweep):
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler",
                        lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())
    monkeypatch.setattr("rf_sentinel.storage.SQLiteMeasurementSink.store_sweep",
                        fail_persistence)

    assert run_station(Settings(data_dir=tmp_path)) == 1
    assert captured["stop"].is_set()
    assert captured["receipt"].status == "failed"
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    assert any(item["component"] == "measurement-writer"
               and item["code"] == "writer_failure"
               for item in summary["failures"]["components"])


@pytest.mark.parametrize("component", ["acquisition", "report-scheduler"])
@pytest.mark.parametrize("outcome", ["coordinated", "premature", "exception"])
def test_station_managed_component_return_semantics(
        monkeypatch, tmp_path, component, outcome):
    captured = {}

    def component_operation(stop):
        if outcome == "coordinated":
            stop.set()
        elif outcome == "exception":
            raise RuntimeError(f"{component} exploded")

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]
            captured["stop"] = self.stop

        def run(self):
            if component == "acquisition":
                component_operation(self.stop)
            else:
                self.stop.wait()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    def fake_scheduler(_runner, stop):
        if component == "report-scheduler":
            component_operation(stop)
        else:
            stop.wait()

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", fake_scheduler)
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == (0 if outcome == "coordinated" else 1)
    assert captured["stop"].is_set()

    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    health = __import__("json").loads(
        (tmp_path / "status" / "health.json").read_text())
    failures = [item for item in summary["failures"]["components"]
                if item["component"] == component]

    if outcome == "coordinated":
        assert failures == []
        assert summary["failures"]["status"] == "clean"
        assert (health["application_status"], health["shutdown_status"]) == (
            "stopped", "complete")
    else:
        assert len(failures) == 1
        assert summary["application"]["status"] == "failed"
        assert summary["failures"]["status"] == "failed"
        assert (health["application_status"], health["shutdown_status"]) == (
            "failed", "incomplete")
        assert len(summary["failures"]["components"]) <= 32
        if outcome == "premature":
            assert (failures[0]["category"], failures[0]["code"]) == (
                "runtime", "premature_exit")
        else:
            assert failures[0]["category"] == "RuntimeError"
            assert failures[0]["message"] == f"{component} exploded"

    assert not any(thread.name in {"station-acquisition", "station-reports"}
                   for thread in enumerate_threads())


def test_station_simultaneous_component_returns_record_one_premature_exit(
        monkeypatch, tmp_path):
    returning = Barrier(2)

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            returning.wait()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    def fake_scheduler(_runner, _stop):
        returning.wait()

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", fake_scheduler)
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 1

    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    failures = [item for item in summary["failures"]["components"]
                if item["component"] in {"acquisition", "report-scheduler"}]
    assert len(failures) == 1
    assert failures[0]["category"] == "runtime"
    assert failures[0]["code"] == "premature_exit"
    assert summary["failures"]["status"] == "failed"


def test_station_telegram_thread_still_alive_is_non_clean(monkeypatch, tmp_path):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]

        def run(self):
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    class StuckTelegram:
        def __init__(self):
            self.join_timeouts = []

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

    telegram = StuckTelegram()
    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())
    monkeypatch.setattr("rf_sentinel.application._start_telegram_polling",
                        lambda settings, stop, notifier, **kwargs: telegram)

    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    assert run_station(settings) == 1
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    assert summary["application"]["status"] in {"partial", "failed"}
    assert summary["failures"]["status"] != "clean"
    assert any(item["component"] == "telegram"
               and item["code"] == "incomplete"
               for item in summary["failures"]["components"])
    assert telegram.join_timeouts
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert health["application_status"] == "failed"
    assert health["shutdown_status"] == "incomplete"


def test_station_resource_sampler_incomplete_is_non_clean(monkeypatch, tmp_path):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]

        def run(self):
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    class IncompleteSampler:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def stop(self, timeout_seconds):
            assert timeout_seconds >= 0
            return False

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())
    monkeypatch.setattr("rf_sentinel.observability.LinuxResourceSampler", IncompleteSampler)

    assert run_station(Settings(data_dir=tmp_path)) == 1
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    assert summary["application"]["status"] in {"partial", "failed"}
    assert summary["failures"]["status"] != "clean"
    assert any(item["component"] == "resource-sampler"
               and item["code"] == "incomplete"
               for item in summary["failures"]["components"])
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert health["application_status"] == "failed"
    assert health["shutdown_status"] == "incomplete"


def test_station_supervisor_reaps_rtl_power_when_acquisition_is_stuck(
        monkeypatch, tmp_path):
    from rf_sentinel.rtl_power import RTLPowerScanner

    real_thread = Thread
    captured = {}
    scanner = RTLPowerScanner()

    class ActiveProcess:
        returncode = None
        terminated = False
        killed = False
        reaped = False
        waits = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            self.waits += 1
            if not self.killed:
                raise subprocess.TimeoutExpired("rtl_power", timeout)
            self.reaped = True
            return self.returncode

    process = ActiveProcess()
    scanner._register_process(process)

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            captured["stop"] = args[4]

        def run(self):
            raise AssertionError("stuck acquisition thread must not invoke its target")

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    class StuckAcquisitionThread:
        def start(self):
            captured["stop"].set()

        def join(self, timeout=None):
            assert timeout >= 0

        def is_alive(self):
            return True

    def thread_factory(*args, name=None, **kwargs):
        if name == "station-acquisition":
            return StuckAcquisitionThread()
        return real_thread(*args, name=name, **kwargs)

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.application.Thread", thread_factory)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner",
                        lambda *args, **kwargs: scanner)
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler",
                        lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 1
    assert process.terminated and process.killed and process.reaped
    assert process.waits == 2
    assert scanner.active_process is None
    still_owned = _StationProcessLock(tmp_path / "station.lock")
    assert not still_owned.acquire()


@pytest.mark.parametrize("stuck_component", ["station-acquisition", "station-reports"])
def test_station_worker_incomplete_is_non_clean(monkeypatch, tmp_path, stuck_component):
    real_thread = Thread
    captured = {"sink_closes": 0}

    class FakeSink:
        def __init__(self, *args, **kwargs):
            pass

        def close(self, *, deadline):
            captured["sink_closes"] += 1

        def telemetry_snapshot(self):
            return {"queue": {"outstanding": 0}, "persistence": {},
                    "shutdown_status": "complete", "writer_alive": False,
                    "drain_complete": True}

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]
            captured["stop"] = self.stop

        def run(self):
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    class StuckThread:
        def start(self):
            captured["stop"].set()

        def join(self, timeout=None):
            assert timeout >= 0

        def is_alive(self):
            return True

    def thread_factory(*args, name=None, **kwargs):
        if name == stuck_component:
            captured["stuck_daemon"] = kwargs.get("daemon")
            return StuckThread()
        return real_thread(*args, name=name, **kwargs)

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.application.Thread", thread_factory)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.AsyncMeasurementSink", FakeSink)
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 1
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    expected = "acquisition" if stuck_component == "station-acquisition" else "report-scheduler"
    assert any(item["component"] == expected and item["code"] == "incomplete"
               for item in summary["failures"]["components"])
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")
    assert captured["sink_closes"] == 1
    assert captured["stuck_daemon"] is True
    still_owned = _StationProcessLock(tmp_path / "station.lock")
    assert not still_owned.acquire()


def _install_fast_station_fakes(monkeypatch):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]
            self.observer = args[3]

        def run(self):
            self.observer.state.application_status = "stopped"
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())


def _install_managed_telegram_runtime(monkeypatch, runtime_operation, *, coordinated_stop=False):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]
            self.observer = args[3]

        def run(self):
            self.observer.state.application_status = "stopped"
            if coordinated_stop:
                self.stop.set()
            else:
                self.stop.wait()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    class FakePollingRuntime:
        def __init__(self, *args, **kwargs):
            self.stop = args[2]

        def run(self):
            return runtime_operation(self.stop)

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())
    monkeypatch.setattr("rf_sentinel.telegram.TelegramReportHandler.from_settings",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.telegram.TelegramPollingRuntime", FakePollingRuntime)


def test_station_telegram_coordinated_return_is_clean(monkeypatch, tmp_path):
    _install_managed_telegram_runtime(
        monkeypatch, lambda stop: stop.wait(), coordinated_stop=True)

    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    assert run_station(settings) == 0
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    assert summary["failures"]["status"] == "clean"
    assert not any(item["component"] == "telegram"
                   for item in summary["failures"]["components"])
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("stopped", "complete")


def test_station_telegram_premature_return_is_non_clean(monkeypatch, tmp_path):
    _install_managed_telegram_runtime(monkeypatch, lambda stop: None)

    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    assert run_station(settings) == 1
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    failures = [item for item in summary["failures"]["components"]
                if item["component"] == "telegram"]
    assert summary["application"]["status"] == "failed"
    assert summary["failures"]["status"] != "clean"
    assert [(item["category"], item["code"]) for item in failures] == [
        ("runtime", "premature_exit")]
    assert summary["acquisition"]["shutdown_status"] == "complete"


def test_station_telegram_runtime_exception_is_non_clean_and_finalizes(monkeypatch, tmp_path):
    def explode(_stop):
        raise RuntimeError("telegram exploded")

    _install_managed_telegram_runtime(monkeypatch, explode)

    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    assert run_station(settings) == 1
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    failures = [item for item in summary["failures"]["components"]
                if item["component"] == "telegram"]
    assert summary["application"]["status"] == "failed"
    assert summary["failures"]["status"] != "clean"
    assert len(failures) == 1
    assert failures[0]["category"] == "RuntimeError"
    assert failures[0]["message"] == "telegram exploded"
    assert len(summary["failures"]["components"]) <= 32
    assert summary["acquisition"]["shutdown_status"] == "complete"


def test_station_telegram_stop_exception_is_non_clean(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)

    class StopFailure:
        def stop(self):
            raise RuntimeError("stop failed")

        def join(self, timeout=None):
            raise AssertionError("join must not follow a failed stop")

        def is_alive(self):
            return False

    monkeypatch.setattr("rf_sentinel.application._start_telegram_polling",
                        lambda *args, **kwargs: StopFailure())
    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    assert run_station(settings) == 1
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    assert any(item["component"] == "telegram" and item["code"] == "shutdown_error"
               for item in summary["failures"]["components"])
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")


def test_station_telegram_join_exception_is_non_clean(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)

    class JoinFailure:
        def stop(self):
            pass

        def join(self, timeout=None):
            raise RuntimeError("join failed")

        def is_alive(self):
            return False

    monkeypatch.setattr("rf_sentinel.application._start_telegram_polling",
                        lambda *args, **kwargs: JoinFailure())
    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    assert run_station(settings) == 1
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    assert any(item["component"] == "telegram" and item["code"] == "shutdown_error"
               for item in summary["failures"]["components"])
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")


def test_station_telegram_alive_check_exception_retains_lock_until_terminal(
        monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)
    import time as real_time

    terminal = Event()
    retained = []

    class AliveCheckFailure:
        alive_checks = 0

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            self.alive_checks += 1
            if self.alive_checks == 1:
                raise RuntimeError("alive check failed")
            return not terminal.is_set()

    telegram = AliveCheckFailure()
    application = __import__("rf_sentinel.application", fromlist=["_retain_station_lock"])
    retain_station_lock = application._retain_station_lock

    def capture_survivors(lock, survivors):
        retained.extend(survivors)
        retain_station_lock(lock, survivors)

    monkeypatch.setattr("rf_sentinel.application._start_telegram_polling",
                        lambda *args, **kwargs: telegram)
    monkeypatch.setattr("rf_sentinel.application._retain_station_lock", capture_survivors)
    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    reusable = _StationProcessLock(tmp_path / "station.lock")
    try:
        assert run_station(settings) == 1
        assert telegram in retained
        assert not reusable.acquire()
        summary = __import__("json").loads(
            (tmp_path / "status" / "run-summary.json").read_text())
        assert any(item["component"] == "telegram" and item["code"] == "shutdown_error"
                   and "alive check" in item["message"]
                   for item in summary["failures"]["components"])
        assert summary["acquisition"]["shutdown_status"] == "complete"
        health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
        assert (health["application_status"], health["shutdown_status"]) == (
            "failed", "incomplete")
    finally:
        terminal.set()

    release_deadline = real_time.monotonic() + 2
    while real_time.monotonic() < release_deadline and not reusable.acquire():
        real_time.sleep(0.01)
    assert reusable._stream is not None
    reusable.release()


def test_station_lock_guardian_join_exception_retains_lock_until_terminal(tmp_path):
    import time as real_time
    from rf_sentinel.application import _retain_station_lock

    terminal = Event()
    join_failed = Event()

    class JoinFailure:
        def is_alive(self):
            return not terminal.is_set()

        def join(self, timeout=None):
            join_failed.set()
            raise RuntimeError("join failed")

    held = _StationProcessLock(tmp_path / "station.lock")
    assert held.acquire()
    _retain_station_lock(held, (JoinFailure(),))
    assert join_failed.wait(1)

    reusable = _StationProcessLock(tmp_path / "station.lock")
    try:
        assert not reusable.acquire()
    finally:
        terminal.set()

    release_deadline = real_time.monotonic() + 3
    while real_time.monotonic() < release_deadline and not reusable.acquire():
        real_time.sleep(0.01)
    assert reusable._stream is not None
    reusable.release()


def test_station_lock_owner_survives_guardian_thread_termination(
        monkeypatch, tmp_path):
    import time as real_time
    import rf_sentinel.application as application

    terminal = Event()
    guardian_finished = Event()

    class Survivor:
        def is_alive(self):
            return not terminal.is_set()

        def join(self, timeout=None):
            pass

    class TerminatedGuardian:
        def __init__(self, *, target, name, daemon):
            assert name == "station-lock-guardian"
            assert daemon is True

        def start(self):
            thread = Thread(target=guardian_finished.set, daemon=True)
            thread.start()
            thread.join()

    survivor = Survivor()
    held = _StationProcessLock(tmp_path / "station.lock")
    assert held.acquire()
    with monkeypatch.context() as context:
        context.setattr(application, "Thread", TerminatedGuardian)
        application._retain_station_lock(held, (survivor,))
    assert guardian_finished.is_set()
    assert held in application._retained_station_locks

    reusable = _StationProcessLock(tmp_path / "station.lock")
    assert not reusable.acquire()
    terminal.set()
    application._retain_station_lock(held, (survivor,))

    release_deadline = real_time.monotonic() + 2
    while real_time.monotonic() < release_deadline and not reusable.acquire():
        real_time.sleep(0.01)
    assert reusable._stream is not None
    assert held not in application._retained_station_locks
    reusable.release()


def test_station_is_single_sink_owner_with_exact_finalization_sequence(monkeypatch, tmp_path):
    events = []
    producer_finished = Event()
    close_deadlines = []
    storages = []

    class SpyStorage:
        def __init__(self, *_args, **_kwargs):
            self.name = "acquisition" if not storages else "report"
            storages.append(self)

        def close(self):
            events.append(f"{self.name}-downstream-close")

        def query_incidents(self):
            return []

        def storage_status(self):
            return {}

    class SpySink:
        def __init__(self, downstream, **kwargs):
            assert kwargs.get("close_downstream", True) is True
            self.downstream = downstream
            self.close_count = 0

        def close(self, *, deadline):
            assert producer_finished.is_set()
            close_deadlines.append(deadline)
            self.close_count += 1
            events.append("async-drain")
            self.downstream.close()

        def telemetry_snapshot(self):
            return {"queue": {"outstanding": 0}, "persistence": {},
                    "shutdown_status": "complete", "writer_alive": False,
                    "drain_complete": True}

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            assert kwargs["sink_lifecycle_owner"] == "station"
            self.stop = args[4]
            self.observer = args[3]

        def set_shutdown_deadline(self, _deadline):
            raise AssertionError("station must not delegate its sink deadline to worker")

        def run(self):
            self.observer.state.application_status = "stopped"
            events.append("producer-finished")
            producer_finished.set()
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    from rf_sentinel.observability import AcquisitionObserver
    original_capture = AcquisitionObserver.capture_final_sink_snapshot
    original_publish = AcquisitionObserver.publish_final_sink_snapshot

    def capture(self, sink):
        events.append("capture")
        return original_capture(self, sink)

    def publish(self, snapshot):
        events.append("publish")
        return original_publish(self, snapshot)

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.application.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.storage.SQLiteMeasurementSink", SpyStorage)
    monkeypatch.setattr("rf_sentinel.acquisition.AsyncMeasurementSink", SpySink)
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())
    monkeypatch.setattr(AcquisitionObserver, "capture_final_sink_snapshot", capture)
    monkeypatch.setattr(AcquisitionObserver, "publish_final_sink_snapshot", publish)

    assert run_station(Settings(data_dir=tmp_path)) == 0
    assert events == [
        "producer-finished", "async-drain", "acquisition-downstream-close",
        "report-downstream-close", "capture", "publish",
    ]
    assert close_deadlines == [130.0]
    assert len(storages) == 2
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    assert health["application_status"] == "stopped"
    assert health["shutdown_status"] == "complete"
    assert summary["application"]["status"] == "stopped"
    assert summary["failures"]["status"] == "clean"


def test_station_owner_accounts_sink_shutdown_failure_once(monkeypatch, tmp_path):
    events = []

    class FailingSink:
        def __init__(self, *args, **kwargs):
            pass

        def close(self, *, deadline):
            events.append("close")
            raise RuntimeError("sink shutdown failed")

        def telemetry_snapshot(self):
            events.append("capture")
            return {"queue": {"outstanding": 1}, "persistence": {},
                    "shutdown_status": "incomplete", "writer_alive": True,
                    "drain_complete": False}

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            assert kwargs["sink_lifecycle_owner"] == "station"
            self.stop = args[4]

        def run(self):
            events.append("producer-finished")
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.AsyncMeasurementSink", FailingSink)
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 1
    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    assert events == ["producer-finished", "close", "capture"]
    close_failures = [item for item in summary["failures"]["components"]
                      if item["component"] == "storage"
                      and "failed to stop cleanly" in item["message"]]
    assert len(close_failures) == 1
    assert summary["acquisition"]["shutdown_status"] == "incomplete"
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")


def test_station_downstream_close_failure_is_non_clean(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)
    storage = __import__("rf_sentinel.storage", fromlist=["SQLiteMeasurementSink"])
    original_close = storage.SQLiteMeasurementSink.close
    close_count = 0

    def fail_first_close(self):
        nonlocal close_count
        close_count += 1
        original_close(self)
        if close_count == 1:
            raise RuntimeError("downstream close failed")

    monkeypatch.setattr(storage.SQLiteMeasurementSink, "close", fail_first_close)

    assert run_station(Settings(data_dir=tmp_path)) == 1
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    assert any(item["component"] == "storage" and item["code"] == "shutdown_error"
               for item in summary["failures"]["components"])
    assert close_count == 2
    assert summary["acquisition"]["shutdown_status"] == "incomplete"
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")


def test_station_downstream_close_hang_is_bounded_before_single_capture(monkeypatch, tmp_path):
    import time as real_time

    actual_monotonic = real_time.monotonic
    _install_fast_station_fakes(monkeypatch)
    acquisition = __import__("rf_sentinel.acquisition", fromlist=["AsyncMeasurementSink"])
    original_sink = acquisition.AsyncMeasurementSink
    release = Event()
    close_started = Event()
    expired = Event()
    captures = []
    storages = []

    class SharedClock:
        def __call__(self):
            return 131.0 if expired.is_set() else 100.0

    clock = SharedClock()

    class HangingStorage:
        def __init__(self, *_args, **_kwargs):
            self.name = "acquisition" if not storages else "report"
            storages.append(self)

        def close(self):
            if self.name == "acquisition":
                close_started.set()
                expired.set()
                release.wait()

        def query_incidents(self):
            return []

        def storage_status(self):
            return {}

        @property
        def persistence_telemetry(self):
            return {}

    def sink_factory(downstream, **kwargs):
        return original_sink(downstream, deadline_monotonic=clock, **kwargs)

    observer = __import__("rf_sentinel.observability", fromlist=["AcquisitionObserver"])
    original_capture = observer.AcquisitionObserver.capture_final_sink_snapshot

    def capture_once(self, sink):
        assert close_started.is_set()
        captures.append("capture")
        return original_capture(self, sink)

    monkeypatch.setattr("rf_sentinel.application.time.monotonic", clock)
    monkeypatch.setattr("rf_sentinel.storage.SQLiteMeasurementSink", HangingStorage)
    monkeypatch.setattr("rf_sentinel.acquisition.AsyncMeasurementSink", sink_factory)
    monkeypatch.setattr(observer.AcquisitionObserver, "capture_final_sink_snapshot", capture_once)

    started = actual_monotonic()
    reusable = _StationProcessLock(tmp_path / "station.lock")
    try:
        assert run_station(Settings(data_dir=tmp_path)) == 1
        returned_elapsed = actual_monotonic() - started
        assert not reusable.acquire()
    finally:
        release.set()
    release_deadline = actual_monotonic() + 2
    while actual_monotonic() < release_deadline and not reusable.acquire():
        real_time.sleep(0.01)
    assert reusable._stream is not None
    reusable.release()
    assert returned_elapsed < 1.0
    assert captures == ["capture"]
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert summary["acquisition"]["shutdown_status"] == "incomplete"
    assert summary["failures"]["status"] != "clean"
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")


def test_station_report_close_survivor_retains_lock_until_terminal(monkeypatch, tmp_path):
    import time as real_time

    actual_monotonic = real_time.monotonic
    _install_fast_station_fakes(monkeypatch)
    acquisition = __import__("rf_sentinel.acquisition", fromlist=["AsyncMeasurementSink"])
    original_sink = acquisition.AsyncMeasurementSink
    release = Event()
    close_started = Event()
    expired = Event()
    storages = []

    class SharedClock:
        def __call__(self):
            return 131.0 if expired.is_set() else 100.0

    clock = SharedClock()

    class Storage:
        def __init__(self, *_args, **_kwargs):
            self.name = "acquisition" if not storages else "report"
            storages.append(self)

        def close(self):
            if self.name == "report":
                close_started.set()
                expired.set()
                release.wait()

        def query_incidents(self):
            return []

        def storage_status(self):
            return {}

        @property
        def persistence_telemetry(self):
            return {}

    def sink_factory(downstream, **kwargs):
        return original_sink(downstream, deadline_monotonic=clock, **kwargs)

    monkeypatch.setattr("rf_sentinel.application.time.monotonic", clock)
    monkeypatch.setattr("rf_sentinel.storage.SQLiteMeasurementSink", Storage)
    monkeypatch.setattr("rf_sentinel.acquisition.AsyncMeasurementSink", sink_factory)

    reusable = _StationProcessLock(tmp_path / "station.lock")
    try:
        assert run_station(Settings(data_dir=tmp_path)) == 1
        assert close_started.is_set()
        assert not reusable.acquire()
    finally:
        release.set()

    release_deadline = actual_monotonic() + 2
    while actual_monotonic() < release_deadline and not reusable.acquire():
        real_time.sleep(0.01)
    assert reusable._stream is not None
    reusable.release()


def test_station_report_storage_close_uses_no_budget_after_shared_deadline(
        monkeypatch, tmp_path):
    from rf_sentinel.application import _bounded_close

    joins = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            assert kwargs["daemon"] is True

        def start(self):
            pass

        def join(self, timeout=None):
            joins.append(timeout)

        def is_alive(self):
            return True

    monkeypatch.setattr("rf_sentinel.application.Thread", FakeThread)
    monkeypatch.setattr("rf_sentinel.application.time.monotonic", lambda: 130.0)
    status, error = _bounded_close(object(), deadline=130.0, thread_name="report-close")

    assert joins == [0.0]
    assert status == "incomplete"
    assert isinstance(error, TimeoutError)


@pytest.mark.parametrize(
    ("completion", "raises", "expected_status", "expected_join"),
    [
        (120.0, False, "complete", 10.0),
        (130.0, False, "complete", 0.0),
        (131.0, False, "incomplete", 0.0),
        (120.0, True, "failed", 10.0),
    ],
    ids=("before-deadline", "exact-deadline", "after-deadline", "exception-before-deadline"),
)
def test_bounded_report_storage_close_uses_helper_completion_timestamp(
        monkeypatch, completion, raises, expected_status, expected_join):
    from rf_sentinel.application import _bounded_close

    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    clock = Clock()
    joins = []

    class Storage:
        def close(self):
            clock.now = completion
            if raises:
                raise RuntimeError("close failed")

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            self.target()

        def join(self, timeout=None):
            joins.append(timeout)

        def is_alive(self):
            return False

    monkeypatch.setattr("rf_sentinel.application.Thread", ImmediateThread)
    monkeypatch.setattr("rf_sentinel.application.time.monotonic", clock)

    status, error = _bounded_close(Storage(), deadline=130.0, thread_name="report-close")

    assert status == expected_status
    assert joins == [expected_join]
    if expected_status == "complete":
        assert error is None
    elif expected_status == "failed":
        assert isinstance(error, RuntimeError)
    else:
        assert isinstance(error, TimeoutError)


def test_bounded_report_storage_late_completion_cannot_rewrite_timeout(
        monkeypatch):
    from rf_sentinel.application import _bounded_close

    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    clock = Clock()
    close_count = 0
    deferred = {}

    class Storage:
        def close(self):
            nonlocal close_count
            close_count += 1

    class DeferredThread:
        def __init__(self, *, target, name, daemon):
            assert daemon is True
            deferred["target"] = target

        def start(self):
            pass

        def join(self, timeout=None):
            assert timeout == 30.0

        def is_alive(self):
            return "completed" not in deferred

    monkeypatch.setattr("rf_sentinel.application.Thread", DeferredThread)
    monkeypatch.setattr("rf_sentinel.application.time.monotonic", clock)

    status, error = _bounded_close(Storage(), deadline=130.0, thread_name="report-close")
    assert status == "incomplete"
    assert isinstance(error, TimeoutError)

    clock.now = 131.0
    deferred["target"]()
    deferred["completed"] = True

    assert status == "incomplete"
    assert close_count == 1


def test_station_late_report_storage_close_is_non_clean_before_final_capture(
        monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)
    events = []
    storages = []

    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    clock = Clock()

    class Storage:
        def __init__(self, *_args, **_kwargs):
            self.name = "acquisition" if not storages else "report"
            self.close_count = 0
            storages.append(self)

        def close(self):
            self.close_count += 1
            events.append(f"{self.name}-close")
            if self.name == "report":
                clock.now = 131.0

        def query_incidents(self):
            return []

        def storage_status(self):
            return {}

    class Sink:
        def __init__(self, downstream, **_kwargs):
            self.downstream = downstream
            self.close_count = 0

        def close(self, *, deadline):
            self.close_count += 1
            events.append("sink-close")
            self.downstream.close()

        def telemetry_snapshot(self):
            return {"queue": {"outstanding": 0}, "persistence": {},
                    "shutdown_status": "complete", "writer_alive": False,
                    "drain_complete": True}

    observer = __import__("rf_sentinel.observability", fromlist=["AcquisitionObserver"])
    original_capture = observer.AcquisitionObserver.capture_final_sink_snapshot

    def capture_once(self, sink):
        events.append("capture")
        return original_capture(self, sink)

    monkeypatch.setattr("rf_sentinel.application.time.monotonic", clock)
    monkeypatch.setattr("rf_sentinel.storage.SQLiteMeasurementSink", Storage)
    monkeypatch.setattr("rf_sentinel.acquisition.AsyncMeasurementSink", Sink)
    monkeypatch.setattr(observer.AcquisitionObserver,
                        "capture_final_sink_snapshot", capture_once)

    assert run_station(Settings(data_dir=tmp_path)) == 1

    summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    health = __import__("json").loads(
        (tmp_path / "status" / "health.json").read_text())
    report_failures = [item for item in summary["failures"]["components"]
                       if item["component"] == "report-storage"]

    assert events == ["sink-close", "acquisition-close", "report-close", "capture"]
    assert [storage.close_count for storage in storages] == [1, 1]
    assert len(report_failures) == 1
    assert report_failures[0]["code"] == "incomplete"
    assert summary["acquisition"]["shutdown_status"] == "incomplete"
    assert summary["failures"]["status"] == "failed"
    assert (health["application_status"], health["shutdown_status"]) == (
        "failed", "incomplete")


def test_station_acceptance_path_has_no_unowned_or_post_capture_sqlite_close():
    import inspect
    from rf_sentinel.application import _run_station_lifecycle

    source = inspect.getsource(_run_station_lifecycle)
    assert "close_downstream=False" not in source
    assert "acquisition_storage.close(" not in source
    assert source.index("_bounded_close(") < source.index("capture_final_sink_snapshot(")


def test_station_multiple_component_failures_are_bounded_and_non_clean(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)

    class IncompleteSampler:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self, timeout_seconds):
            return False

    class StuckTelegram:
        def stop(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr("rf_sentinel.observability.LinuxResourceSampler", IncompleteSampler)
    monkeypatch.setattr("rf_sentinel.application._start_telegram_polling",
                        lambda *args, **kwargs: StuckTelegram())
    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")

    assert run_station(settings) == 1
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    assert {item["component"] for item in summary["failures"]["components"]} >= {
        "resource-sampler", "telegram"}
    assert len(summary["failures"]["components"]) <= 32
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")


def test_final_snapshot_is_captured_once_and_summary_survives_health_failure(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)
    calls = []
    original_capture = __import__("rf_sentinel.observability", fromlist=["AcquisitionObserver"]).AcquisitionObserver.capture_final_sink_snapshot

    def capture_once(self, sink):
        calls.append("capture")
        return original_capture(self, sink)

    def fail_health_publication(self, snapshot):
        calls.append(("health", snapshot["shutdown_status"]))
        raise OSError("health disk full")

    monkeypatch.setattr("rf_sentinel.observability.AcquisitionObserver.capture_final_sink_snapshot", capture_once)
    monkeypatch.setattr("rf_sentinel.observability.AcquisitionObserver.publish_final_sink_snapshot",
                        fail_health_publication)

    assert run_station(Settings(data_dir=tmp_path)) == 1
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    assert calls == ["capture", ("health", "complete"), ("health", "complete")]
    assert summary["acquisition"]["shutdown_status"] == "complete"
    assert summary["failures"]["status"] != "clean"
    assert any(item["component"] == "health-publication"
               and item["code"] == "publication_failed"
               for item in summary["failures"]["components"])


def test_incomplete_snapshot_survives_health_publication_failure(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)
    snapshot = {"queue": {"outstanding": 2}, "persistence": {"status": "failed"},
                "shutdown_status": "incomplete", "writer_alive": True,
                "drain_complete": False}
    monkeypatch.setattr("rf_sentinel.observability.AcquisitionObserver.capture_final_sink_snapshot",
                        lambda self, sink: snapshot)
    monkeypatch.setattr("rf_sentinel.observability.AcquisitionObserver.publish_final_sink_snapshot",
                        lambda self, value: (_ for _ in ()).throw(OSError("health disk full")))

    assert run_station(Settings(data_dir=tmp_path)) == 1
    summary = __import__("json").loads((tmp_path / "status" / "run-summary.json").read_text())
    assert summary["acquisition"]["shutdown_status"] == "incomplete"
    assert summary["acquisition"]["queue_telemetry"] == snapshot["queue"]
    assert summary["failures"]["status"] != "clean"


def test_summary_failure_does_not_skip_health_publication(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)
    calls = []
    observability = __import__("rf_sentinel.observability", fromlist=["AcquisitionObserver"])
    acquisition = __import__("rf_sentinel.acquisition", fromlist=["AsyncMeasurementSink"])
    original_publish = observability.AcquisitionObserver.publish_final_sink_snapshot
    original_capture = observability.AcquisitionObserver.capture_final_sink_snapshot
    original_close = acquisition.AsyncMeasurementSink.close

    def publish_health(self, snapshot):
        calls.append(("publish", self.state.application_status))
        return original_publish(self, snapshot)

    def capture_once(self, sink):
        calls.append("capture")
        return original_capture(self, sink)

    def close_once(self, *args, **kwargs):
        calls.append("close")
        return original_close(self, *args, **kwargs)

    monkeypatch.setattr(observability.AcquisitionObserver,
                        "publish_final_sink_snapshot", publish_health)
    monkeypatch.setattr(observability.AcquisitionObserver,
                        "capture_final_sink_snapshot", capture_once)
    monkeypatch.setattr(acquisition.AsyncMeasurementSink, "close", close_once)
    monkeypatch.setattr(observability.RunSummaryWriter, "write",
                        lambda self, **kwargs: (_ for _ in ()).throw(OSError("summary disk full")))

    assert run_station(Settings(data_dir=tmp_path)) == 1
    assert calls == ["close", "capture", ("publish", "stopped"),
                     ("publish", "failed")]
    health = __import__("json").loads((tmp_path / "status" / "health.json").read_text())
    stale_summary = __import__("json").loads(
        (tmp_path / "status" / "run-summary.json").read_text())
    assert health["shutdown_status"] == "incomplete"
    assert health["application_status"] == "failed"
    assert stale_summary["application"]["status"] == "running"


def test_both_final_publications_fail_without_blocking_shutdown(monkeypatch, tmp_path):
    _install_fast_station_fakes(monkeypatch)
    observability = __import__("rf_sentinel.observability", fromlist=["AcquisitionObserver"])
    original_publish = observability.AcquisitionObserver.publish_final_sink_snapshot
    publications = []

    def fail_corrective_publication(self, snapshot):
        publications.append(self.state.application_status)
        if len(publications) == 2:
            raise OSError("corrective health write failed")
        return original_publish(self, snapshot)

    monkeypatch.setattr(observability.AcquisitionObserver,
                        "publish_final_sink_snapshot", fail_corrective_publication)
    monkeypatch.setattr(observability.RunSummaryWriter, "write",
                        lambda self, **kwargs: (_ for _ in ()).throw(OSError("summary disk full")))

    assert run_station(Settings(data_dir=tmp_path)) == 1
    assert publications == ["stopped", "failed"]


def test_station_continues_when_start_marker_write_fails(monkeypatch, tmp_path):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]

        def run(self):
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.observability.RunSummaryWriter.write_start_marker",
                        lambda self: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 0


def test_station_component_exception_is_accounted_in_final_summary(monkeypatch, tmp_path):
    all_components_started = Event()
    scheduler_stopped = Event()
    telegram_stopped = Event()
    sampler_stopped = Event()
    captured = {}

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]
            captured["stop"] = self.stop

        def run(self):
            assert all_components_started.wait(2)
            raise RuntimeError("component exploded")

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    class FakeResourceSampler:
        def __init__(self, *args, **kwargs):
            self.stop_requested = Event()
            self.thread = Thread(target=self.run, name="test-resource-sampler")
            captured["sampler"] = self

        def run(self):
            self.stop_requested.wait()
            sampler_stopped.set()

        def start(self):
            self.thread.start()

        def stop(self, timeout_seconds):
            self.stop_requested.set()
            self.thread.join(timeout_seconds)
            return not self.thread.is_alive()

    def fake_scheduler(runner, stop):
        stop.wait()
        scheduler_stopped.set()

    def fake_start_telegram(settings, stop, notifier, **kwargs):
        def run():
            stop.wait()
            telegram_stopped.set()

        thread = Thread(target=run, name="test-telegram-inbound")
        captured["telegram_thread"] = thread
        thread.start()
        all_components_started.set()
        return thread

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", fake_scheduler)
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())
    monkeypatch.setattr("rf_sentinel.observability.LinuxResourceSampler", FakeResourceSampler)
    monkeypatch.setattr("rf_sentinel.application._start_telegram_polling", fake_start_telegram)

    settings = Settings(data_dir=tmp_path, telegram_bot_token="123:test", telegram_chat_id="1")
    assert run_station(settings) == 1
    assert captured["stop"].is_set()
    assert scheduler_stopped.is_set()
    assert telegram_stopped.is_set()
    assert sampler_stopped.is_set()
    assert not captured["telegram_thread"].is_alive()
    assert not captured["sampler"].thread.is_alive()
    assert not any(thread.name in {"station-acquisition", "station-reports"}
                   for thread in enumerate_threads())
    import json
    summary = json.loads((tmp_path / "status" / "run-summary.json").read_text())
    assert summary["application"]["status"] == "failed"
    assert summary["failures"]["status"] == "failed"
    assert len(summary["failures"]["components"]) == 1
    assert summary["failures"]["components"][0]["component"] == "acquisition"
    assert summary["failures"]["components"][0]["message"] == "component exploded"
    health = json.loads((tmp_path / "status" / "health.json").read_text())
    assert (health["application_status"], health["shutdown_status"]) == ("failed", "incomplete")


def test_station_incident_source_failure_does_not_break_final_summary(monkeypatch, tmp_path):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]

        def run(self):
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())
    monkeypatch.setattr("rf_sentinel.storage.SQLiteMeasurementSink.query_incidents",
                        lambda self, *args, **kwargs: (_ for _ in ()).throw(RuntimeError("read")))

    assert run_station(Settings(data_dir=tmp_path)) == 0
    import json
    summary = json.loads((tmp_path / "status" / "run-summary.json").read_text())
    assert summary["failures"]["incident_source"] == "unavailable"
    assert summary["failures"]["incidents"] == []


def test_station_lock_is_released_after_graceful_shutdown(monkeypatch, tmp_path):
    class FakeWorker:
        def __init__(self, *args, **kwargs):
            self.stop = args[4]

        def run(self):
            self.stop.set()

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", lambda *args, **kwargs: object())
    monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", FakeWorker)
    monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", FakeRunner)
    monkeypatch.setattr("rf_sentinel.scheduler.run_report_scheduler", lambda runner, stop: stop.wait())
    monkeypatch.setattr("rf_sentinel.reporting.SQLiteReportEngine", lambda *args: object())

    assert run_station(Settings(data_dir=tmp_path)) == 0
    reusable = _StationProcessLock(tmp_path / "station.lock")
    assert reusable.acquire()
    reusable.release()


def test_second_station_fails_before_components_start(monkeypatch, tmp_path):
    monkeypatch.setattr("rf_sentinel.application.configure_logging", lambda *args: None)
    held = _StationProcessLock(tmp_path / "station.lock")
    assert held.acquire()
    try:
        def must_not_start(*args, **kwargs):
            raise AssertionError("station component started")

        monkeypatch.setattr("rf_sentinel.rtl_power.RTLPowerScanner", must_not_start)
        monkeypatch.setattr("rf_sentinel.acquisition.SpectrumAcquisitionWorker", must_not_start)
        monkeypatch.setattr("rf_sentinel.scheduler.ScheduledReportRunner", must_not_start)
        monkeypatch.setattr("rf_sentinel.application._start_telegram_polling", must_not_start)

        assert run_station(Settings(data_dir=tmp_path)) == 1
    finally:
        held.release()


def test_station_lock_reusable_after_process_termination(tmp_path):
    path = tmp_path / "station.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys; stream = open(sys.argv[1], 'a+'); "
            "fcntl.flock(stream, fcntl.LOCK_EX); print('ready', flush=True); input()",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        child.terminate()
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    reusable = _StationProcessLock(path)
    assert reusable.acquire()
    reusable.release()
