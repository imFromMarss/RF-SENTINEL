import json
import traceback

import pytest

from rf_sentinel.errors import NotificationError
from rf_sentinel.telegram import TelegramNotifier


class Connection:
    def __init__(self, status=200, payload=b'{"ok":true}', failure=None):
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


def test_message_post():
    connection = Connection()
    notifier(connection).send_message("survey complete")
    args, kwargs = connection.calls[0]
    assert args[0] == "POST"
    assert args[1].endswith("/sendMessage")
    assert kwargs["body"] == b"chat_id=-1&text=survey+complete"
    assert connection.closed


def test_photo_upload(tmp_path):
    path = tmp_path / "local-name.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
    connection = Connection()
    notifier(connection).send_photo(path)
    args, kwargs = connection.calls[0]
    assert args[1].endswith("/sendPhoto")
    assert path.read_bytes() in kwargs["body"]
    assert b'filename="waterfall.png"' in kwargs["body"]
    assert b"local-name" not in kwargs["body"]


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
