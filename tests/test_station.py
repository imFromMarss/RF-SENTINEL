from threading import Event

from rf_sentinel.application import run_station
from rf_sentinel.config import Settings


def test_station_composes_one_acquisition_and_report_runtime(monkeypatch, tmp_path):
    started = Event()
    report_ready = Event()
    captured = {}

    class FakeScanner:
        def __init__(self, *args, **kwargs):
            captured["scanner"] = self

    class FakeWorker:
        def __init__(self, scanner, sink, profile, observer, stop, *args):
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
            self.sink.close()

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
        def __init__(self, *args):
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
