"""Outbound-only Bot API transport. No scanner or application control imports."""

import http.client
import json
import uuid
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from rf_sentinel.errors import NotificationError

MAX_PHOTO_BYTES = 9 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class Notifier(Protocol):
    def send_message(self, text: str) -> None: ...

    def send_photo(self, path: Path) -> None: ...


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, connection_factory=None):
        # Validation is also enforced for callers outside the composition root.
        from rf_sentinel.config import Settings

        Settings(telegram_bot_token=token, telegram_chat_id=chat_id)
        if not token:
            raise NotificationError("Telegram credentials are required")
        self._token = token
        self._chat_id = chat_id
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def _post(self, method: str, body: bytes, content_type: str) -> None:
        connection = None
        failure = None
        try:
            connection = self._connection_factory("api.telegram.org", timeout=15)
            connection.request(
                "POST",
                f"/bot{self._token}/{method}",
                body=body,
                headers={"Content-Type": content_type},
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                failure = "Telegram HTTP request failed"
            else:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    failure = "Telegram response exceeded size limit"
                else:
                    result = json.loads(payload)
                    if not isinstance(result, dict) or result.get("ok") is not True:
                        failure = "Telegram API rejected notification"
        except (OSError, http.client.HTTPException, ValueError):
            failure = "Telegram transport failed or returned invalid JSON"
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    failure = failure or "Telegram transport failed"
        # Raise outside the handler: no original exception, URL or response body.
        if failure:
            raise NotificationError(failure)

    def send_message(self, text: str) -> None:
        if not 1 <= len(text) <= 4096:
            raise NotificationError("Telegram message must contain 1 to 4096 characters")
        body = urlencode({"chat_id": self._chat_id, "text": text}).encode()
        self._post("sendMessage", body, "application/x-www-form-urlencoded")

    def send_photo(self, path: Path) -> None:
        try:
            with path.open("rb") as source:
                photo = source.read(MAX_PHOTO_BYTES + 1)
        except OSError:
            raise NotificationError("Cannot read notification image") from None
        if not photo.startswith(b"\x89PNG\r\n\x1a\n") or len(photo) > MAX_PHOTO_BYTES:
            raise NotificationError("Notification image must be a PNG within size limit")
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n'
            f'{self._chat_id}\r\n--{boundary}\r\n'
            'Content-Disposition: form-data; name="photo"; filename="waterfall.png"\r\n'
            'Content-Type: image/png\r\n\r\n'
        ).encode() + photo + f"\r\n--{boundary}--\r\n".encode()
        self._post("sendPhoto", body, f"multipart/form-data; boundary={boundary}")
