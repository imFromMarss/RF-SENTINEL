import json

import pytest

from rf_sentinel.errors import NotificationError, ScanError, SentinelError
from rf_sentinel.workflow import SurveyWorkflow


class Scanner:
    def __init__(self, result, failure=False):
        self.result = result
        self.failure = failure
        self.calls = 0

    def scan(self, profile, raw_path=None):
        self.calls += 1
        assert profile == self.result.profile
        if raw_path is not None:
            raw_path.write_text("synthetic CSV\n")
        if self.failure:
            raise ScanError("штучна помилка")
        return self.result


class Notifier:
    def __init__(self, fail_at=None):
        self.messages = []
        self.photos = []
        self.fail_at = fail_at
        self.attempts = 0

    def send_message(self, message):
        self.messages.append(message)
        self.attempts += 1
        if self.fail_at == "message":
            raise NotificationError("недоступно")
        return 101

    def send_photo(self, path, caption=""):
        assert path.exists()
        self.photos.append((path, caption))
        self.attempts += 1
        if self.fail_at == "photo":
            raise NotificationError("недоступно")
        return 102


def renderer(result, path):
    path.write_bytes(b"synthetic-heatmap")


def test_each_run_has_distinct_historical_artifacts(scan_result, tmp_path):
    notifier = Notifier()
    workflow = SurveyWorkflow(
        Scanner(scan_result), scan_result.profile, tmp_path, "test-host",
        notifier, renderer, telegram_backoff_seconds=0,
    )
    outcomes = [workflow.run(), workflow.run()]
    assert outcomes[0].artifact_dir != outcomes[1].artifact_dir
    assert len(list((tmp_path / "surveys").iterdir())) == 2
    for outcome in outcomes:
        folder = outcome.artifact_dir
        assert outcome.scan_status == "success"
        assert outcome.notification_status == "sent"
        assert {path.name for path in folder.iterdir()} == {
            "request.json", "spectrum.csv", "spectrum.json", "report.json",
            "report.txt", "heatmap.png", "delivery.json",
        }
        assert json.loads((folder / "report.json").read_text())["peak_power_db"] == -10
        delivery = json.loads((folder / "delivery.json").read_text())
        assert delivery["report_message_id"] == 101
        assert delivery["photo_message_id"] == 102
    assert notifier.photos[0][1].startswith("RF Sentinel — карта спектра за")


def test_telegram_failure_retries_and_keeps_artifacts(scan_result, tmp_path):
    delays = []
    notifier = Notifier("message")
    workflow = SurveyWorkflow(
        Scanner(scan_result), scan_result.profile, tmp_path, "test", notifier,
        renderer, telegram_attempts=3, telegram_backoff_seconds=2,
        sleeper=delays.append,
    )
    outcome = workflow.run()
    assert outcome.scan_status == "success"
    assert outcome.notification_status == "failed"
    assert notifier.attempts == 3
    assert delays == [2, 2]
    assert (outcome.artifact_dir / "heatmap.png").exists()
    assert json.loads((outcome.artifact_dir / "delivery.json").read_text())["status"] == "failed"


def test_scan_failure_is_historical_and_has_no_heatmap(scan_result, tmp_path):
    outcome = SurveyWorkflow(
        Scanner(scan_result, failure=True), scan_result.profile, tmp_path,
        "test", None, renderer,
    ).run()
    assert outcome.scan_status == "scan_failed"
    assert (outcome.artifact_dir / "spectrum.csv").exists()
    assert (outcome.artifact_dir / "report.json").exists()
    assert not (outcome.artifact_dir / "heatmap.png").exists()


def test_render_failure_preserves_raw_and_report(scan_result, tmp_path):
    def fail_render(result, path):
        raise RuntimeError("synthetic-private")

    with pytest.raises(SentinelError) as error:
        SurveyWorkflow(
            Scanner(scan_result), scan_result.profile, tmp_path,
            "test", Notifier(), fail_render,
        ).run()
    assert "synthetic-private" not in str(error.value)
    folders = list((tmp_path / "surveys").iterdir())
    assert len(folders) == 1
    assert (folders[0] / "spectrum.csv").exists()
    assert (folders[0] / "report.json").exists()


def test_filesystem_failure_prevents_notification(scan_result, tmp_path):
    notifier = Notifier()
    path = tmp_path / "not-a-directory"
    path.write_text("occupied")
    workflow = SurveyWorkflow(Scanner(scan_result), scan_result.profile,
                              path, "test", notifier, renderer)
    with pytest.raises(SentinelError):
        workflow.run()
    assert not notifier.messages
