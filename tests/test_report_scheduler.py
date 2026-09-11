from datetime import UTC, datetime
from pathlib import Path

from rf_sentinel.reporting import (ReportData, ReportGap,
                                   generate_report_package as real_generate_report_package)
from rf_sentinel.scheduler import ScheduledReportRunner
from rf_sentinel.telegram import DeliveryResult


class FakePackage:
    def __init__(self, start, end):
        self.window_start = start
        self.window_end = end


class Engine:
    def __init__(self, fail=False):
        self.windows = []
        self.fail = fail

    def build(self, start, end):
        self.windows.append((start, end))
        if self.fail:
            raise RuntimeError("synthetic report failure")
        return object()


class Notifier:
    def __init__(self, status="sent"):
        self.packages = []
        self.status = status

    def send_package(self, package):
        self.packages.append(package)
        return DeliveryResult(self.status, {"report": 1, "waterfall": 2, "heatmap": 3})


def _empty_report(start, end):
    return ReportData(
        start, end, 0, 0, 0, 0, 0.0, None, (), None, None, (),
        (ReportGap(start, end, "window"),),
    )


def test_disabled_telegram_still_generates_and_persists_package(monkeypatch, tmp_path):
    engine = Engine()
    engine.build = lambda start, end: (engine.windows.append((start, end)) or
                                        _empty_report(start, end))
    generated = []

    def generate(report, destination, timezone):
        package = real_generate_report_package(report, destination, timezone)
        generated.append(package)
        return package

    monkeypatch.setattr("rf_sentinel.scheduler.generate_report_package", generate)
    runner = ScheduledReportRunner(engine, None, tmp_path, timezone="UTC")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)

    assert runner.run_pending(now) == ("hourly",)
    assert runner.run_pending(now) == ()
    assert len(generated) == 1
    assert all(path.exists() for path in generated[0].paths)
    assert runner.state.total_completed_reports == 1
    assert runner.state.total_failed_reports == 0


def test_delivery_failure_leaves_generated_package_persisted(monkeypatch, tmp_path):
    engine = Engine()
    engine.build = lambda start, end: (engine.windows.append((start, end)) or
                                        _empty_report(start, end))
    generated = []

    def generate(report, destination, timezone):
        package = real_generate_report_package(report, destination, timezone)
        generated.append(package)
        return package

    notifier = Notifier(status="failed")
    monkeypatch.setattr("rf_sentinel.scheduler.generate_report_package", generate)
    runner = ScheduledReportRunner(engine, notifier, tmp_path, timezone="UTC")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)

    assert runner.run_pending(now) == ()
    assert len(generated) == 1
    assert all(path.exists() for path in generated[0].paths)
    assert len(notifier.packages) == 1
    assert runner.state.last_report_error_reason == "telegram_delivery"


def test_hourly_boundary_and_duplicate_protection(monkeypatch, tmp_path):
    engine = Engine()
    notifier = Notifier()
    monkeypatch.setattr(
        "rf_sentinel.scheduler.generate_report_package",
        lambda report, destination, timezone: FakePackage(
            datetime(2026, 9, 9, 10, tzinfo=UTC), datetime(2026, 9, 9, 11, tzinfo=UTC)),
    )
    runner = ScheduledReportRunner(engine, notifier, tmp_path, timezone="UTC")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)

    assert runner.run_pending(now) == ("hourly",)
    assert runner.run_pending(now) == ()
    assert engine.windows == [(datetime(2026, 9, 9, 10, tzinfo=UTC), now)]
    assert len(notifier.packages) == 1

    restarted = ScheduledReportRunner(engine, notifier, tmp_path, timezone="UTC")
    assert restarted.run_pending(now) == ()
    assert len(notifier.packages) == 1


def test_midnight_delivers_hourly_and_daily_as_separate_windows(monkeypatch, tmp_path):
    engine = Engine()
    notifier = Notifier()
    monkeypatch.setattr(
        "rf_sentinel.scheduler.generate_report_package",
        lambda report, destination, timezone: FakePackage(None, None),
    )
    runner = ScheduledReportRunner(engine, notifier, tmp_path, timezone="UTC")
    now = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)

    assert runner.run_pending(now) == ("hourly", "daily")
    assert engine.windows == [
        (datetime(2026, 9, 8, 23, tzinfo=UTC), now),
        (datetime(2026, 9, 8, tzinfo=UTC), now),
    ]


def test_calendar_windows_use_configured_timezone(monkeypatch, tmp_path):
    engine = Engine()
    notifier = Notifier()
    monkeypatch.setattr("rf_sentinel.scheduler.generate_report_package",
                        lambda report, destination, timezone: FakePackage(None, None))
    runner = ScheduledReportRunner(engine, notifier, tmp_path, timezone="Europe/Kyiv")
    # 21:00 UTC is midnight in Kyiv during the summer transition.
    now = datetime(2026, 9, 8, 21, 0, tzinfo=UTC)

    assert runner.run_pending(now) == ("hourly", "daily")
    assert engine.windows == [
        (datetime(2026, 9, 8, 20, tzinfo=UTC), now),
        (datetime(2026, 9, 7, 21, tzinfo=UTC), datetime(2026, 9, 8, 21, tzinfo=UTC)),
    ]


def test_report_failure_does_not_stop_runner_or_mark_window(monkeypatch, tmp_path):
    engine = Engine(fail=True)
    notifier = Notifier()
    monkeypatch.setattr("rf_sentinel.scheduler.generate_report_package",
                        lambda report, destination, timezone: FakePackage(None, None))
    runner = ScheduledReportRunner(engine, notifier, tmp_path, timezone="UTC")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)

    assert runner.run_pending(now) == ()
    assert runner.run_pending(now) == ()
    assert len(engine.windows) == 2


def test_telegram_failure_does_not_mark_window_or_call_acquisition(monkeypatch, tmp_path):
    engine = Engine()
    notifier = Notifier(status="failed")
    acquisition_calls = []
    monkeypatch.setattr("rf_sentinel.scheduler.generate_report_package",
                        lambda report, destination, timezone: FakePackage(None, None))
    runner = ScheduledReportRunner(engine, notifier, tmp_path, timezone="UTC")
    monkeypatch.setattr("rf_sentinel.scheduler.run_continuous_loop",
                        lambda *args, **kwargs: acquisition_calls.append(True))
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)

    assert runner.run_pending(now) == ()
    assert runner.run_pending(now) == ()
    assert acquisition_calls == []
    assert len(notifier.packages) == 2
