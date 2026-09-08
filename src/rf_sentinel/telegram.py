"""Вихідний транспорт Bot API без доступу до сканера чи керування application."""

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
    def send_message(self, text: str) -> int | None: ...

    def send_photo(self, path: Path, caption: str = "") -> int | None: ...


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, connection_factory=None):
        # Перевірка діє і для викликів поза точкою складання application.
        from rf_sentinel.config import Settings

        Settings(telegram_bot_token=token, telegram_chat_id=chat_id)
        if not token:
            raise NotificationError("Потрібні облікові дані Telegram")
        self._token = token
        self._chat_id = chat_id
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def _post(self, method: str, body: bytes, content_type: str) -> int | None:
        connection = None
        failure = None
        message_id = None
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
                failure = "Помилка HTTP-запиту Telegram"
            else:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    failure = "Відповідь Telegram перевищила ліміт розміру"
                else:
                    result = json.loads(payload)
                    if not isinstance(result, dict) or result.get("ok") is not True:
                        failure = "Telegram API відхилив сповіщення"
                    else:
                        message_id = result.get("result", {}).get("message_id")
        except (OSError, http.client.HTTPException, ValueError):
            failure = "Помилка транспорту Telegram або некоректний JSON"
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    failure = failure or "Помилка транспорту Telegram"
        # Поза обробником: без початкового exception, URL чи тіла відповіді.
        if failure:
            raise NotificationError(failure)
        return message_id

    def send_message(self, text: str) -> int | None:
        if not 1 <= len(text) <= 4096:
            raise NotificationError("Повідомлення Telegram має містити від 1 до 4096 символів")
        body = urlencode({"chat_id": self._chat_id, "text": text}).encode()
        return self._post("sendMessage", body, "application/x-www-form-urlencoded")

    def send_photo(self, path: Path, caption: str = "") -> int | None:
        try:
            with path.open("rb") as source:
                photo = source.read(MAX_PHOTO_BYTES + 1)
        except OSError:
            raise NotificationError("Не вдалося прочитати зображення для сповіщення") from None
        if not photo.startswith(b"\x89PNG\r\n\x1a\n") or len(photo) > MAX_PHOTO_BYTES:
            raise NotificationError("Зображення має бути PNG в межах дозволеного розміру")
        if len(caption) > 1024:
            raise NotificationError("Підпис зображення має містити до 1024 символів")
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n'
            f'{self._chat_id}\r\n--{boundary}\r\n'
            'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f'{caption}\r\n--{boundary}\r\n'
            'Content-Disposition: form-data; name="photo"; filename="heatmap.png"\r\n'
            'Content-Type: image/png\r\n\r\n'
        ).encode() + photo + f"\r\n--{boundary}--\r\n".encode()
        return self._post("sendPhoto", body, f"multipart/form-data; boundary={boundary}")
