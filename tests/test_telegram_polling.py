import json
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

from rf_sentinel.reporting import ReportData, ReportGap
from rf_sentinel.telegram import (
    LAST_HOUR_REPORT_BUTTON,
    DeliveryResult,
    TelegramPollingRuntime,
    TelegramReportHandler,
)


NOW = datetime(2026, 9, 9, 12, 34, 56, tzinfo=UTC)


def empty_report():
    return ReportData(
        NOW - timedelta(hours=1), NOW, 0, 0, 0, 0, 0.0, None, (), None, None, (),
        (ReportGap(NOW - timedelta(hours=1), NOW, "window"),),
    )


def button_update(update_id=1, *, chat_id="100", user_id="200"):
    return {"update_id": update_id, "message": {
        "chat": {"id": chat_id}, "from": {"id": user_id},
        "text": LAST_HOUR_REPORT_BUTTON,
    }}


class FakeResponse:
    status = 200

    def __init__(self, updates):
        self.payload = json.dumps({"ok": True, "result": updates}).encode()

    def read(self, limit):
        return self.payload


class FakeConnection:
    def __init__(self, updates, calls):
        self.updates = updates
        self.calls = calls

    def request(self, method, path, body, headers):
        self.calls.append((method, path, body, headers))

    def getresponse(self):
        return FakeResponse(self.updates)

    def close(self):
        pass


class ConnectionQueue:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, host, timeout):
        response = self.responses.pop(0) if self.responses else []
        if isinstance(response, BaseException):
            raise response
        return FakeConnection(response, self.calls)


class PackageNotifier:
    def __init__(self):
        self.packages = []

    def send_package(self, package):
        self.packages.append(package)
        return DeliveryResult("sent", {"report": 1, "waterfall": 2, "heatmap": 3})


class SpyEngine:
    def __init__(self):
        self.windows = []

    def build(self, start, end):
        self.windows.append((start, end))
        return empty_report()


def runtime(tmp_path, connections, *, engine=None, stop=None, sleeper=None):
    stop = stop or Event()
    engine = engine or SpyEngine()
    notifier = PackageNotifier()
    handler = TelegramReportHandler(
        engine, notifier, tmp_path, allowed_chat_ids=("100",),
        allowed_user_ids=("200",), timezone="UTC", clock=lambda: NOW,
    )
    return TelegramPollingRuntime(
        "123:synthetic", handler, stop, connection_factory=connections,
        long_poll_timeout=0, attempts=3, backoff_seconds=0, sleeper=sleeper,
    ), engine, notifier, stop


def test_authorized_button_end_to_end_through_polling_path(tmp_path):
    connections = ConnectionQueue([[button_update()]])
    polling, engine, notifier, _ = runtime(tmp_path, connections)

    assert polling.poll_once() == 1
    assert engine.windows == [(NOW - timedelta(hours=1), NOW)]
    assert len(notifier.packages) == 1
    assert all(path.exists() for path in notifier.packages[0].paths)
    assert b"getUpdates" in connections.calls[0][1].encode()


def test_unauthorized_update_does_not_generate_report(tmp_path):
    class MustNotRun:
        def build(self, start, end):
            raise AssertionError("report generation must not run")

    connections = ConnectionQueue([[button_update(chat_id="999", user_id="888")]])
    polling, _, notifier, _ = runtime(tmp_path, connections, engine=MustNotRun())

    assert polling.poll_once() == 1
    assert notifier.packages == []


def test_offset_advances_and_deduplicates_updates(tmp_path):
    connections = ConnectionQueue([
        [button_update(10), {"update_id": 11, "message": {"text": "ignored"}}],
        [button_update(11), button_update(12)],
    ])
    polling, engine, notifier, _ = runtime(tmp_path, connections)

    assert polling.poll_once() == 2
    assert polling.offset == 12
    assert polling.poll_once() == 1
    assert polling.offset == 13
    assert len(engine.windows) == len(notifier.packages) == 2
    assert b"offset=12" in connections.calls[1][2]


def test_network_failure_retries_and_does_not_end_runtime(tmp_path):
    connections = ConnectionQueue([OSError("secret token must not log"), [button_update()]])
    waits = []
    polling, _, notifier, _ = runtime(tmp_path, connections, sleeper=waits.append)

    assert polling.poll_once() == 1
    assert len(connections.calls) == 1
    assert waits == [0]
    assert len(notifier.packages) == 1


def test_graceful_shutdown_interrupts_polling_loop(tmp_path):
    stop = Event()
    connections = ConnectionQueue([[]])
    polling, _, _, _ = runtime(tmp_path, connections, stop=stop)

    thread = Thread(target=polling.run)
    thread.start()
    stop.set()
    thread.join(timeout=1)

    assert not thread.is_alive()


def test_polling_path_has_no_acquisition_dependency(tmp_path):
    connections = ConnectionQueue([[button_update()]])
    polling, _, _, _ = runtime(tmp_path, connections)
    import rf_sentinel.acquisition as acquisition

    original = acquisition.run_continuous_loop
    acquisition.run_continuous_loop = lambda *args: (_ for _ in ()).throw(
        AssertionError("acquisition must not run"))
    try:
        assert polling.poll_once() == 1
    finally:
        acquisition.run_continuous_loop = original
