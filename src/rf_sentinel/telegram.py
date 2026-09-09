"""Вихідний транспорт Bot API без доступу до сканера чи керування application."""

import http.client
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Callable, Protocol
from urllib.parse import urlencode

from rf_sentinel.errors import NotificationError

if TYPE_CHECKING:
    from rf_sentinel.reporting import ReportPackage

logger = logging.getLogger(__name__)

LAST_HOUR_REPORT_BUTTON = "📊 Звіт за останню годину"

MAX_PHOTO_BYTES = 9 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_UPDATE_BATCH = 100


class Notifier(Protocol):
    def send_message(self, text: str) -> int | None: ...

    def send_photo(self, path: Path, caption: str = "") -> int | None: ...


@dataclass(frozen=True)
class DeliveryResult:
    """Safe, serializable outcome of delivering one report package."""

    status: str
    message_ids: dict[str, int | None]
    error_classification: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message_ids": dict(self.message_ids),
            "error_classification": self.error_classification,
        }


@dataclass(frozen=True)
class InboundReportResult:
    """Safe outcome of handling one inbound Telegram update."""

    status: str
    delivery: DeliveryResult | None = None


def report_reply_keyboard() -> dict:
    """Return the single supported report action for a Telegram reply keyboard."""
    return {"keyboard": [[{"text": LAST_HOUR_REPORT_BUTTON}]], "resize_keyboard": True}


class TelegramReportHandler:
    """Handle the one authorized, read-only report action.

    This boundary consumes already-received Telegram updates. It does not poll,
    schedule, scan, or otherwise call an acquisition component.
    """

    def __init__(self, report_engine, notifier, data_dir: str | Path,
                 *, allowed_chat_ids: tuple[str, ...] = (),
                 allowed_user_ids: tuple[str, ...] = (),
                 timezone: str = "Europe/Kyiv", clock=None):
        self.report_engine = report_engine
        self.notifier = notifier
        self.data_dir = Path(data_dir)
        self.allowed_chat_ids = frozenset(str(value) for value in allowed_chat_ids)
        self.allowed_user_ids = frozenset(str(value) for value in allowed_user_ids)
        self.timezone = timezone
        self.clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_settings(cls, settings, notifier, *, clock=None):
        from rf_sentinel.reporting import SQLiteReportEngine

        allowed_chats = settings.telegram_allowed_chat_ids or (
            (settings.telegram_chat_id,) if settings.telegram_chat_id else ()
        )
        return cls(
            SQLiteReportEngine(settings.sweeps_path), notifier, settings.data_dir,
            allowed_chat_ids=allowed_chats,
            allowed_user_ids=settings.telegram_allowed_user_ids,
            timezone=settings.timezone, clock=clock,
        )

    @staticmethod
    def _sender(update: dict) -> tuple[str | None, str | None, str | None]:
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return None, None, None
        chat = message.get("chat")
        sender = message.get("from")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        user_id = sender.get("id") if isinstance(sender, dict) else None
        return message.get("text"), None if chat_id is None else str(chat_id), \
            None if user_id is None else str(user_id)

    def _authorized(self, chat_id: str | None, user_id: str | None) -> bool:
        return (
            chat_id is not None and chat_id in self.allowed_chat_ids
            and (not self.allowed_user_ids or user_id in self.allowed_user_ids)
        )

    def handle_update(self, update: dict) -> InboundReportResult:
        text, chat_id, user_id = self._sender(update)
        if text != LAST_HOUR_REPORT_BUTTON:
            return InboundReportResult("ignored")
        if not self._authorized(chat_id, user_id):
            # Deliberately omit IDs, update content, and exception details.
            logger.warning("Telegram: unauthorized report request rejected")
            return InboundReportResult("unauthorized")

        from rf_sentinel.reporting import generate_report_package, last_hour_window

        end = self.clock()
        start, end = last_hour_window(end)
        try:
            report = self.report_engine.build(start, end)
            destination = self.data_dir / "reports" / "last-hour" / end.strftime(
                "%Y%m%dT%H%M%S.%fZ")
            package = generate_report_package(report, destination, self.timezone)
            delivery = self.notifier.send_package(package)
            return InboundReportResult("delivered", delivery)
        except Exception as error:
            # The boundary exposes no paths, credentials, IDs, or raw exceptions.
            logger.error("Telegram: last-hour report request failed (%s)",
                         type(error).__name__)
            return InboundReportResult("failed")


class TelegramPollingRuntime:
    """Run the inbound Telegram boundary without owning monitoring resources.

    Telegram transport failures are isolated to this loop.  The update offset
    is advanced after each syntactically accepted update, including an
    unauthorized or unsupported update, so Telegram does not redeliver it.
    """

    def __init__(self, token: str, handler: TelegramReportHandler, stop: Event,
                 *, connection_factory=None, http_timeout: float = 15,
                 long_poll_timeout: int = 5, attempts: int = 3,
                 backoff_seconds: float = 5,
                 sleeper: Callable[[float], object] | None = None):
        if not token:
            raise NotificationError("Потрібні облікові дані Telegram")
        if not 0 < http_timeout <= 60:
            raise ValueError("http_timeout must be between 0 and 60 seconds")
        if not 0 <= long_poll_timeout <= 50:
            raise ValueError("long_poll_timeout must be between 0 and 50 seconds")
        if not 1 <= attempts <= 5:
            raise ValueError("attempts must be between 1 and 5")
        if not 0 <= backoff_seconds <= 300:
            raise ValueError("backoff_seconds must be between 0 and 300 seconds")
        self._token = token
        self.handler = handler
        self.stop = stop
        self._connection_factory = connection_factory or http.client.HTTPSConnection
        self.http_timeout = http_timeout
        self.long_poll_timeout = long_poll_timeout
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.sleeper = sleeper or self.stop.wait
        self.offset: int | None = None
        self._transport_get_updates = getattr(handler.notifier, "get_updates", None)

    def _get_updates(self) -> list[dict]:
        if self._transport_get_updates is not None:
            return self._transport_get_updates(self.offset, self.long_poll_timeout, MAX_UPDATE_BATCH)
        connection = None
        try:
            connection = self._connection_factory("api.telegram.org", timeout=self.http_timeout)
            parameters = {"timeout": self.long_poll_timeout, "limit": MAX_UPDATE_BATCH}
            if self.offset is not None:
                parameters["offset"] = self.offset
            connection.request(
                "POST", f"/bot{self._token}/getUpdates",
                body=urlencode(parameters).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise NotificationError("Помилка HTTP-запиту Telegram")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise NotificationError("Відповідь Telegram перевищила ліміт розміру")
            value = json.loads(payload)
            updates = value.get("result") if isinstance(value, dict) and value.get("ok") is True else None
            if not isinstance(updates, list) or len(updates) > MAX_UPDATE_BATCH:
                raise NotificationError("Некоректна відповідь Telegram")
            return [update for update in updates if isinstance(update, dict)]
        except (OSError, http.client.HTTPException, ValueError, TypeError) as error:
            raise NotificationError("Помилка транспорту Telegram або некоректний JSON") from error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    pass

    def poll_once(self) -> int:
        """Fetch and dispatch one batch; return the number of accepted updates."""
        updates = None
        for attempt in range(1, self.attempts + 1):
            if self.stop.is_set():
                return 0
            try:
                updates = self._get_updates()
                break
            except NotificationError:
                if attempt == self.attempts:
                    logger.warning("Telegram inbound polling failed; monitoring continues")
                    return 0
                logger.warning("Telegram inbound polling failed; retry %d/%d",
                               attempt + 1, self.attempts)
                self.sleeper(self.backoff_seconds)
        accepted = 0
        for update in updates or ():
            update_id = update.get("update_id")
            if type(update_id) is not int or (self.offset is not None and update_id < self.offset):
                continue
            try:
                self.handler.handle_update(update)
            except Exception as error:
                # A malformed handler dependency must not poison the polling loop.
                logger.error("Telegram inbound update handling failed (%s)", type(error).__name__)
            finally:
                self.offset = update_id + 1
                accepted += 1
        return accepted

    def run(self) -> None:
        """Poll until stopped; failures never terminate the monitoring process."""
        while not self.stop.is_set():
            self.poll_once()


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

    def get_updates(self, offset: int | None, timeout: int, limit: int) -> list[dict]:
        """Fetch inbound updates through the same Bot API transport boundary."""
        connection = None
        try:
            connection = self._connection_factory("api.telegram.org", timeout=min(15, timeout + 10))
            parameters = {"timeout": timeout, "limit": limit}
            if offset is not None:
                parameters["offset"] = offset
            connection.request(
                "POST", f"/bot{self._token}/getUpdates",
                body=urlencode(parameters).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise NotificationError("Помилка HTTP-запиту Telegram")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise NotificationError("Відповідь Telegram перевищила ліміт розміру")
            value = json.loads(payload)
            updates = value.get("result") if isinstance(value, dict) and value.get("ok") is True else None
            if not isinstance(updates, list) or len(updates) > limit:
                raise NotificationError("Некоректна відповідь Telegram")
            return [update for update in updates if isinstance(update, dict)]
        except (OSError, http.client.HTTPException, ValueError, TypeError) as error:
            raise NotificationError("Помилка транспорту Telegram або некоректний JSON") from error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    pass

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

    def _send_photo(self, path: Path, caption: str, filename: str) -> int | None:
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
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            'Content-Type: image/png\r\n\r\n'
        ).encode() + photo + f"\r\n--{boundary}--\r\n".encode()
        return self._post("sendPhoto", body, f"multipart/form-data; boundary={boundary}")

    def send_photo(self, path: Path, caption: str = "") -> int | None:
        # Preserve the original primitive's stable multipart filename.
        return self._send_photo(path, caption, "heatmap.png")

    def send_package(self, package: "ReportPackage") -> DeliveryResult:
        """Deliver a ready package without changing any local artifact.

        Each item is independent; retry/backoff remains at the existing
        workflow boundary around this single delivery operation.
        """
        message_ids: dict[str, int | None] = {
            "report": None, "waterfall": None, "heatmap": None,
        }
        failures: list[str] = []
        operations = (
            ("report", lambda: self.send_message(package.report_txt.read_text(encoding="utf-8"))),
            ("waterfall", lambda: self._send_photo(
                package.waterfall, "RF Sentinel — waterfall спектра", "waterfall.png")),
            ("heatmap", lambda: self._send_photo(
                package.heatmap, "RF Sentinel — теплова карта спектра", "heatmap.png")),
        )
        for name, operation in operations:
            try:
                message_ids[name] = operation()
            except NotificationError:
                failures.append(name)
            except (OSError, UnicodeError):
                # Never expose paths, credentials, or underlying exception text.
                failures.append(f"{name}:artifact")

        if not failures:
            return DeliveryResult("sent", message_ids)
        status = "partial" if len(failures) < len(operations) else "failed"
        classification = "artifact" if all(item.endswith(":artifact") for item in failures) else "telegram"
        return DeliveryResult(status, message_ids, classification)

    # Keep both names discoverable at the transport boundary.
    deliver = send_package
    deliver_package = send_package
    send_report_package = send_package
