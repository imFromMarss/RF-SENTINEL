from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import rf_sentinel.reporting as reporting
from rf_sentinel.observability import ObservabilityCoordinator
from rf_sentinel.reporting import ReportData, ReportGap, SQLiteReportEngine
from rf_sentinel.scheduler import ScheduledReportRunner
from rf_sentinel.storage import SQLiteMeasurementSink
from rf_sentinel.telegram import DeliveryResult, TelegramPollingRuntime, TelegramReportHandler


START = datetime(2026, 9, 9, 12, tzinfo=UTC)


def empty_report():
    return ReportData(
        START - timedelta(hours=1), START, 0, 0, 0, 0, 0.0, None, (), None, None, (),
        (ReportGap(START - timedelta(hours=1), START, "window"),),
    )


class PhaseTrace:
    def __init__(self):
        self.events = []

    @contextmanager
    def phase(self, name):
        self.events.append(("start", name))
        try:
            yield
        finally:
            self.events.append(("end", name))

    @contextmanager
    def span(self, name):
        self.events.append(("start", name))
        try:
            yield
        finally:
            self.events.append(("end", name))


def test_query_and_data_build_are_disjoint_phases(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store = SQLiteMeasurementSink(path)
    store.close()
    trace = PhaseTrace()

    SQLiteReportEngine(path, coordinator=trace).build(START - timedelta(hours=1), START)

    assert trace.events == [
        ("start", "query_load"), ("end", "query_load"),
        ("start", "data_build"), ("end", "data_build"),
    ]


def test_render_completes_before_package_phase(tmp_path, monkeypatch):
    trace = PhaseTrace()
    render_calls = []

    def fake_render(report, waterfall, heatmap, timezone):
        render_calls.append(tuple(trace.events))
        Path(waterfall).write_bytes(b"waterfall")
        Path(heatmap).write_bytes(b"heatmap")
        return Path(waterfall), Path(heatmap)

    monkeypatch.setattr(reporting, "render_report_images", fake_render)
    reporting.generate_report_package(empty_report(), tmp_path / "package", "UTC",
                                      coordinator=trace)

    assert trace.events == [
        ("start", "render"), ("end", "render"),
        ("start", "package"), ("end", "package"),
    ]
    assert render_calls == [(("start", "render"),)]


class ScheduledEngine:
    def build(self, start, end):
        return empty_report()


class StatusNotifier:
    def __init__(self, status):
        self.status = status

    def send_package(self, package):
        return DeliveryResult(self.status, {"report": 1, "waterfall": 2, "heatmap": 3})


def test_scheduled_report_has_generation_span_and_separate_delivery_failure(
        tmp_path, monkeypatch):
    coordinator = ObservabilityCoordinator(clock=lambda: 0.0)
    real_generate = reporting.generate_report_package
    monkeypatch.setattr(
        "rf_sentinel.scheduler.generate_report_package",
        lambda report, destination, timezone, *, coordinator=None:
        real_generate(report, destination, timezone, coordinator=coordinator),
    )
    runner = ScheduledReportRunner(
        ScheduledEngine(), StatusNotifier("failed"), tmp_path, timezone="UTC",
        coordinator=coordinator, delivery_attempts=1, sleeper=lambda _: None,
    )

    assert runner.run_pending(START) == ()

    snapshot = coordinator.snapshot()
    assert snapshot["report"]["count"] == 1
    assert snapshot["report"]["success"] == 1
    assert snapshot["report"]["failure"] == 0
    assert runner.state.last_report_error_reason == "telegram_delivery"


def test_scheduled_report_phase_order_is_generation_then_package(tmp_path, monkeypatch):
    path = tmp_path / "sweeps.sqlite3"
    store = SQLiteMeasurementSink(path)
    store.close()
    trace = PhaseTrace()
    real_generate = reporting.generate_report_package
    monkeypatch.setattr(
        "rf_sentinel.scheduler.generate_report_package",
        lambda report, destination, timezone, *, coordinator=None:
        real_generate(report, destination, timezone, coordinator=coordinator),
    )
    runner = ScheduledReportRunner(
        SQLiteReportEngine(path, coordinator=trace), StatusNotifier("failed"), tmp_path,
        timezone="UTC", coordinator=trace, delivery_attempts=1, sleeper=lambda _: None,
    )

    assert runner.run_pending(START) == ()

    assert trace.events == [
        ("start", "report"),
        ("start", "query_load"), ("end", "query_load"),
        ("start", "data_build"), ("end", "data_build"),
        ("start", "render"), ("end", "render"),
        ("start", "package"), ("end", "package"),
        ("end", "report"),
    ]


def test_inbound_authorized_report_has_report_span_and_delivery_is_separate(tmp_path):
    coordinator = ObservabilityCoordinator(clock=lambda: 0.0)

    class Engine:
        def build(self, start, end):
            return empty_report()

    class Notifier:
        def send_package(self, package):
            context = coordinator.span("telegram_delivery")
            with context as telemetry:
                result = DeliveryResult("failed", {
                    "report": None, "waterfall": None, "heatmap": None,
                })
                telemetry.value = result.status
                return result

    handler = TelegramReportHandler(
        Engine(), Notifier(), tmp_path, allowed_chat_ids=("100",), allowed_user_ids=("200",),
        timezone="UTC", clock=lambda: START, coordinator=coordinator,
    )
    update = {"message": {"chat": {"id": "100"}, "from": {"id": "200"},
                           "text": "📊 Звіт за останню годину"}}

    result = handler.handle_update(update)

    assert result.status == "failed"
    snapshot = coordinator.snapshot()
    assert snapshot["report"]["count"] == 1
    assert snapshot["report"]["success"] == 1
    assert snapshot["report"]["failure"] == 0
    assert snapshot["telegram"]["telegram_delivery"]["failure"] == 1


def test_generation_exception_closes_inbound_report_span(tmp_path):
    coordinator = ObservabilityCoordinator(clock=lambda: 0.0)

    class FailingEngine:
        def build(self, start, end):
            raise RuntimeError("synthetic generation failure")

    class MustNotDeliver:
        def send_package(self, package):
            raise AssertionError("delivery must not run")

    handler = TelegramReportHandler(
        FailingEngine(), MustNotDeliver(), tmp_path, allowed_chat_ids=("100",),
        allowed_user_ids=("200",), timezone="UTC", clock=lambda: START,
        coordinator=coordinator,
    )
    update = {"message": {"chat": {"id": "100"}, "from": {"id": "200"},
                           "text": "📊 Звіт за останню годину"}}

    assert handler.handle_update(update).status == "failed"

    snapshot = coordinator.snapshot()
    assert snapshot["report"]["count"] == 1
    assert snapshot["report"]["success"] == 0
    assert snapshot["report"]["failure"] == 1
    assert snapshot["active"]["reports"] == 0


def test_polling_duration_stops_before_handler_execution():
    now = [0.0]
    coordinator = ObservabilityCoordinator(clock=lambda: now[0])

    class Connection:
        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self

        status = 200

        def read(self, limit):
            return b'{"ok":true,"result":[{"update_id":1}]}'

        def close(self):
            pass

    def connect(host, timeout):
        now[0] = 5.0
        return Connection()

    class Handler:
        notifier = object()

        def handle_update(self, update):
            now[0] = 20.0

    runtime = TelegramPollingRuntime(
        "123:synthetic", Handler(), Event(), connection_factory=connect,
        long_poll_timeout=0, attempts=1, sleeper=lambda _: None,
        coordinator=coordinator, clock=lambda: now[0],
    )

    assert runtime.poll_once() == 1
    metric = coordinator.snapshot()["telegram"]["telegram_polling"]
    assert metric["count"] == 1
    assert metric["success"] == 1
    assert metric["total_duration_seconds"] == 5.0
