"""Сценарій огляду: прийом → власний каталог артефактів → сповіщення."""

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from rf_sentinel.errors import NotificationError, ScanError, SentinelError
from rf_sentinel.reporting import duration_text, failed_report, make_report, render_heatmap
from rf_sentinel.spectrum import ScanProfile, SpectrumScanner
from rf_sentinel.telegram import Notifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SurveyOutcome:
    scan_status: str
    notification_status: str
    artifact_dir: Path | None = None
    started_at: str | None = None


class SurveyWorkflow:
    def __init__(self, scanner: SpectrumScanner, profile: ScanProfile, data_dir: Path,
                 identity: str, notifier: Notifier | None = None,
                 renderer=None, timezone: str = "Europe/Kyiv",
                 telegram_attempts: int = 3, telegram_backoff_seconds: float = 5,
                 sleeper=time.sleep, send_failure_reports: bool = True):
        self.scanner = scanner
        self.profile = profile
        self.data_dir = data_dir
        self.identity = identity
        self.notifier = notifier
        self.timezone = timezone
        self.renderer = renderer or (lambda result, path: render_heatmap(result, path, timezone))
        self.telegram_attempts = telegram_attempts
        self.telegram_backoff_seconds = telegram_backoff_seconds
        self.sleeper = sleeper
        self.send_failure_reports = send_failure_reports

    def _deliver(self, operation, label: str):
        for attempt in range(1, self.telegram_attempts + 1):
            try:
                message_id = operation()
                logger.info("Telegram: %s доставлено", label)
                return message_id
            except NotificationError:
                if attempt == self.telegram_attempts:
                    raise
                logger.warning(
                    "Telegram: помилка доставки %s, повтор %d/%d через %.1f с",
                    label, attempt + 1, self.telegram_attempts,
                    self.telegram_backoff_seconds,
                )
                self.sleeper(self.telegram_backoff_seconds)

    def notify_status(self, message: str) -> str:
        if self.notifier is None:
            return "disabled"
        try:
            self._deliver(lambda: self.notifier.send_message(message), "стан monitoring")
            return "sent"
        except NotificationError:
            logger.error("Telegram: не вдалося доставити повідомлення про стан")
            return "failed"

    def run(self) -> SurveyOutcome:
        started = datetime.now(UTC)
        clock = time.monotonic()
        result = None
        name = started.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
        folder = self.data_dir / "surveys" / name
        logger.info(
            "Огляд розпочато: %.3f–%.3f МГц, тривалість %d с",
            self.profile.low_hz / 1e6, self.profile.high_hz / 1e6,
            self.profile.duration_seconds,
        )
        try:
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "request.json").write_text(
                json.dumps(asdict(self.profile), indent=2) + "\n", encoding="utf-8"
            )
            try:
                result = self.scanner.scan(self.profile, raw_path=folder / "spectrum.csv")
                report = make_report(result, self.identity, self.timezone)
                logger.info("Прийом завершено: %d проходів", len(result.spectrum.frames))
            except ScanError as error:
                report = failed_report(self.profile, self.identity, started,
                                       time.monotonic() - clock, self.timezone)
                diagnostics = {
                    "status": "scan_failed",
                    "reason": error.reason,
                    "subprocess_returncode": error.returncode,
                    "stderr_file": (
                        "rtl_power.stderr.txt"
                        if (folder / "rtl_power.stderr.txt").exists() else None
                    ),
                }
                (folder / "scan-diagnostics.json").write_text(
                    json.dumps(diagnostics, indent=2) + "\n", encoding="utf-8"
                )
                logger.error("Огляд завершився помилкою: reason=%s returncode=%s",
                             error.reason, error.returncode)
            (folder / "report.json").write_text(
                json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            (folder / "report.txt").write_text(report.to_text() + "\n", encoding="utf-8")
            if result is not None:
                (folder / "spectrum.json").write_text(
                    json.dumps(asdict(result), default=lambda value: value.isoformat()) + "\n",
                    encoding="utf-8",
                )
                self.renderer(result, folder / "heatmap.png")
                logger.info("Артефакти сформовано: %s", folder.name)
        except (OSError, ValueError, RuntimeError):
            raise SentinelError("Не вдалося зберегти або візуалізувати результати огляду") from None
        notification_status = "disabled"
        delivery = {"status": notification_status, "report_message_id": None, "photo_message_id": None}
        if self.notifier is not None and (result is not None or self.send_failure_reports):
            try:
                delivery["report_message_id"] = self._deliver(
                    lambda: self.notifier.send_message(report.to_text()), "звіт"
                )
                if result is not None:
                    caption = "RF Sentinel — карта спектра за " + duration_text(result.duration_seconds)
                    delivery["photo_message_id"] = self._deliver(
                        lambda: self.notifier.send_photo(folder / "heatmap.png", caption=caption),
                        "карту спектра",
                    )
                notification_status = "sent"
            except NotificationError:
                notification_status = "failed"
                logger.error("Telegram: вичерпано спроби доставки; локальні артефакти збережено")
        elif self.notifier is not None:
            notification_status = "suppressed"
        delivery["status"] = notification_status
        try:
            (folder / "delivery.json").write_text(json.dumps(delivery, indent=2) + "\n", encoding="utf-8")
        except OSError:
            raise SentinelError("Не вдалося зберегти підтвердження доставки") from None
        return SurveyOutcome(report.status, notification_status, folder, started.isoformat())
