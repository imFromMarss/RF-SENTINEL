import json
from threading import Event

import pytest

from rf_sentinel.errors import NotificationError, ScanError, SentinelError
from rf_sentinel.scheduler import run_schedule
from rf_sentinel.workflow import SurveyOutcome, SurveyWorkflow


class Scanner:
    def __init__(self, result, failure=False):
        self.result = result
        self.failure = failure
        self.calls = 0

    def scan(self, profile):
        self.calls += 1
        assert profile == self.result.profile
        if self.failure:
            raise ScanError("scan failed")
        return self.result


class Notifier:
    def __init__(self, fail_at=None):
        self.messages = []
        self.photos = []
        self.fail_at = fail_at

    def send_message(self, message):
        self.messages.append(message)
        if self.fail_at == "message":
            raise NotificationError("unavailable")

    def send_photo(self, path):
        assert path.exists()
        self.photos.append(path)
        if self.fail_at == "photo":
            raise NotificationError("unavailable")


def renderer(result, path):
    path.write_bytes(b"synthetic-waterfall")


@pytest.mark.parametrize("fail_at", [None, "message", "photo", "disabled"])
def test_workflow_persists_then_notifies(scan_result, tmp_path, fail_at):
    transport = None if fail_at == "disabled" else Notifier(fail_at)
    scanner = Scanner(scan_result)
    workflow = SurveyWorkflow(scanner, scan_result.profile, tmp_path,
                              "test-host", transport, renderer)
    for _ in range(2):
        outcome = workflow.run()
        assert outcome.scan_status == "success"
    assert scanner.calls == 2
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "report.json", "report.txt", "spectrum.json", "waterfall.png",
    ]
    assert json.loads((tmp_path / "report.json").read_text())["peak_power_db"] == -10
    assert json.loads((tmp_path / "spectrum.json").read_text())["backend"] == "fake"
    expected = "disabled" if transport is None else ("sent" if fail_at is None else "failed")
    assert outcome.notification_status == expected
    if transport is not None:
        assert len(transport.messages) == 2
        assert len(transport.photos) == (0 if fail_at == "message" else 2)


def test_scan_failure_removes_stale_artifacts(scan_result, tmp_path):
    scanner, notifier = Scanner(scan_result), Notifier()
    workflow = SurveyWorkflow(scanner, scan_result.profile, tmp_path, "test", notifier, renderer)
    workflow.run()
    scanner.failure = True
    outcome = workflow.run()
    assert outcome.scan_status == "scan_failed"
    assert not (tmp_path / "waterfall.png").exists()
    assert not (tmp_path / "spectrum.json").exists()
    assert len(notifier.photos) == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["peak_power_db"] is None
    assert report["status"] == "scan_failed"


def test_filesystem_failure_prevents_notification(scan_result, tmp_path):
    notifier = Notifier()
    path = tmp_path / "not-a-directory"
    path.write_text("occupied")
    workflow = SurveyWorkflow(Scanner(scan_result), scan_result.profile,
                              path, "test", notifier, renderer)
    with pytest.raises(SentinelError):
        workflow.run()
    assert not notifier.messages


def test_scheduler_retries_and_stops_without_waiting(caplog):
    class Stop:
        def __init__(self):
            self.delays = []
        def is_set(self):
            return len(self.delays) == 2
        def wait(self, seconds):
            self.delays.append(seconds)
    stop = Stop()
    calls = []
    def run():
        calls.append(1)
        if len(calls) == 1:
            raise SentinelError("synthetic-sensitive-error")
        return SurveyOutcome("success", "disabled")
    run_schedule(run, 1800, stop)
    assert len(calls) == 2
    assert stop.delays == [1800, 1800]
    assert "synthetic-sensitive-error" not in caplog.text


def test_scheduler_does_not_run_when_stopped():
    stop = Event()
    stop.set()
    run_schedule(lambda: pytest.fail("must not run"), 1, stop)


def test_render_failure_preserves_source_and_removes_old_image(scan_result, tmp_path):
    (tmp_path / "waterfall.png").write_bytes(b"stale")
    def fail_render(result, path):
        raise RuntimeError("synthetic-private")
    notifier = Notifier()
    workflow = SurveyWorkflow(Scanner(scan_result), scan_result.profile, tmp_path,
                              "test", notifier, fail_render)
    with pytest.raises(SentinelError) as error:
        workflow.run()
    assert "synthetic-private" not in str(error.value)
    assert (tmp_path / "spectrum.json").exists()
    assert (tmp_path / "report.json").exists()
    assert not (tmp_path / "waterfall.png").exists()
    assert not notifier.messages
