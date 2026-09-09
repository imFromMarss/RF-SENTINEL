from datetime import UTC, datetime, timedelta

from rf_sentinel.reporting import ReportData
from rf_sentinel.storage import SQLiteMeasurementSink
from rf_sentinel.telegram import (
    LAST_HOUR_REPORT_BUTTON,
    DeliveryResult,
    TelegramReportHandler,
    report_reply_keyboard,
)


NOW = datetime(2026, 9, 9, 12, 34, 56, tzinfo=UTC)


class PackageNotifier:
    def __init__(self):
        self.packages = []

    def send_package(self, package):
        self.packages.append(package)
        return DeliveryResult("sent", {"report": 1, "waterfall": 2, "heatmap": 3})


class SpyEngine:
    def __init__(self, report):
        self.report = report
        self.windows = []

    def build(self, start, end):
        self.windows.append((start, end))
        return self.report


def update(*, chat_id="100", user_id="200"):
    return {"message": {"chat": {"id": chat_id}, "from": {"id": user_id},
                         "text": LAST_HOUR_REPORT_BUTTON}}


def empty_report():
    from rf_sentinel.reporting import ReportGap

    return ReportData(
        NOW - timedelta(hours=1), NOW, 0, 0, 0, 0, 0.0, None, (), None, None, (),
        (ReportGap(NOW - timedelta(hours=1), NOW, "window"),),
    )


def test_authorized_button_builds_last_hour_package_without_acquisition(tmp_path):
    engine = SpyEngine(empty_report())
    notifier = PackageNotifier()
    handler = TelegramReportHandler(
        engine, notifier, tmp_path, allowed_chat_ids=("100",),
        allowed_user_ids=("200",), timezone="UTC", clock=lambda: NOW,
    )

    result = handler.handle_update(update())

    assert result.status == "delivered"
    assert engine.windows == [(NOW - timedelta(hours=1), NOW)]
    assert len(notifier.packages) == 1
    package = notifier.packages[0]
    assert all(path.exists() for path in package.paths)
    assert "Проходи: 0 успішних" in package.report_txt.read_text(encoding="utf-8")


def test_unauthorized_button_is_rejected_before_report_or_acquisition(tmp_path, caplog):
    class MustNotRun:
        def build(self, start, end):
            raise AssertionError("report engine must not run")

    secret = "secret-user-or-chat"
    handler = TelegramReportHandler(
        MustNotRun(), PackageNotifier(), tmp_path,
        allowed_chat_ids=("100",), allowed_user_ids=("200",), clock=lambda: NOW,
    )

    result = handler.handle_update(update(chat_id=secret, user_id=secret))

    assert result.status == "unauthorized"
    assert secret not in caplog.text
    assert "unauthorized report request rejected" in caplog.text


def test_button_is_the_only_exposed_keyboard_action():
    keyboard = report_reply_keyboard()
    assert keyboard["keyboard"] == [[{"text": LAST_HOUR_REPORT_BUTTON}]]


def test_empty_real_storage_still_produces_report_package(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    sink = SQLiteMeasurementSink(path)
    sink.close()
    notifier = PackageNotifier()
    from rf_sentinel.reporting import SQLiteReportEngine

    handler = TelegramReportHandler(
        SQLiteReportEngine(path), notifier, tmp_path, allowed_chat_ids=("100",),
        timezone="UTC", clock=lambda: NOW,
    )

    assert handler.handle_update(update(user_id="unconfigured")).status == "delivered"
    assert notifier.packages[0].window_start == NOW - timedelta(hours=1)
