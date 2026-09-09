import traceback
from datetime import UTC, datetime

import pytest

from rf_sentinel.errors import NotificationError
from rf_sentinel.telegram import TelegramNotifier
from rf_sentinel.reporting import ReportPackage


class Connection:
    def __init__(self, status=200, payload=b'{"ok":true,"result":{"message_id":17}}', failure=None):
        self.status = status
        self.payload = payload
        self.failure = failure
        self.closed = False
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise self.failure

    def getresponse(self):
        return self

    def read(self, limit):
        return self.payload[:limit]

    def close(self):
        self.closed = True


def notifier(connection):
    def connect(host, timeout):
        assert host == "api.telegram.org"
        assert timeout == 15
        return connection
    return TelegramNotifier("0:" + "synthetic_dummy", "-1", connection_factory=connect)


def package(tmp_path):
    paths = {name: tmp_path / name for name in
             ("report.json", "report.txt", "waterfall.png", "heatmap.png")}
    paths["report.json"].write_text("{}\n", encoding="utf-8")
    paths["report.txt"].write_text("Український звіт\n", encoding="utf-8")
    paths["waterfall.png"].write_bytes(b"\x89PNG\r\n\x1a\nwaterfall")
    paths["heatmap.png"].write_bytes(b"\x89PNG\r\n\x1a\nheatmap")
    return ReportPackage(tmp_path, *(paths[name] for name in
                                      ("report.json", "report.txt", "waterfall.png", "heatmap.png")),
                         datetime(2026, 1, 1, tzinfo=UTC),
                         datetime(2026, 1, 1, 0, 1, tzinfo=UTC))


def test_message_post():
    connection = Connection()
    assert notifier(connection).send_message("survey complete") == 17
    args, kwargs = connection.calls[0]
    assert args[0] == "POST"
    assert args[1].endswith("/sendMessage")
    assert kwargs["body"] == b"chat_id=-1&text=survey+complete"
    assert connection.closed


def test_photo_upload(tmp_path):
    path = tmp_path / "local-name.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
    connection = Connection()
    assert notifier(connection).send_photo(path, "Карта спектра") == 17
    args, kwargs = connection.calls[0]
    assert args[1].endswith("/sendPhoto")
    assert path.read_bytes() in kwargs["body"]
    assert b'filename="heatmap.png"' in kwargs["body"]
    assert "Карта спектра".encode() in kwargs["body"]
    assert b"local-name" not in kwargs["body"]


def test_report_package_delivery_returns_all_message_ids(tmp_path):
    connection = Connection()
    result = notifier(connection).send_package(package(tmp_path))
    assert result.to_dict() == {
        "status": "sent",
        "message_ids": {"report": 17, "waterfall": 17, "heatmap": 17},
        "error_classification": None,
    }
    requests = [call[0][1] for call in connection.calls]
    assert requests == ["/bot0:synthetic_dummy/sendMessage",
                        "/bot0:synthetic_dummy/sendPhoto",
                        "/bot0:synthetic_dummy/sendPhoto"]
    assert b"waterfall.png" in connection.calls[1][1]["body"]
    assert "waterfall спектра".encode() in connection.calls[1][1]["body"]
    assert "теплова карта спектра".encode() in connection.calls[2][1]["body"]


def test_report_package_partial_delivery_is_machine_readable(tmp_path):
    connections = [Connection(), Connection(status=503, payload=b"private"), Connection()]

    def connect(host, timeout):
        return connections.pop(0)

    result = TelegramNotifier("0:synthetic_dummy", "-1", connection_factory=connect).send_package(
        package(tmp_path))
    assert result.status == "partial"
    assert result.message_ids == {"report": 17, "waterfall": None, "heatmap": 17}
    assert result.error_classification == "telegram"


def test_package_failure_keeps_artifacts_and_hides_secret(tmp_path):
    token = "0:private-secret"
    original = {path.name: path.read_bytes() for path in package(tmp_path).paths}
    connection = Connection(failure=OSError(token))
    result = TelegramNotifier(token, "-1", connection_factory=lambda *args, **kwargs: connection).send_package(
        package(tmp_path))
    assert result.status == "failed"
    assert result.error_classification == "telegram"
    assert {path.name: path.read_bytes() for path in package(tmp_path).paths} == original
    assert token not in repr(result)


@pytest.mark.parametrize("status,payload,failure", [
    (429, b"private", None), (500, b"private", None), (302, b"private", None),
    (200, b'{"ok":false,"description":"private"}', None),
    (200, b"not json private", None), (200, b"[]", None),
    (200, b'{"ok":1}', None), (200, b"x" * (1024 * 1024 + 1), None),
    (200, b"", TimeoutError("private")), (200, b"", OSError("private")),
])
def test_errors_are_safe(status, payload, failure):
    connection = Connection(status, payload, failure)
    with pytest.raises(NotificationError) as error:
        notifier(connection).send_message("report")
    assert "private" not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None
    assert connection.closed


def test_no_token_in_network_exception():
    token = "0:" + "synthetic_dummy"
    with pytest.raises(NotificationError) as error:
        notifier(Connection(failure=OSError(token))).send_message("report")
    assert token not in "".join(traceback.format_exception(error.value))


def test_invalid_message_and_image(tmp_path):
    transport = notifier(Connection())
    for message in ("", "x" * 4097):
        with pytest.raises(NotificationError):
            transport.send_message(message)
    with pytest.raises(NotificationError):
        transport.send_photo(tmp_path / "missing")
