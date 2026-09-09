"""Межа environment configuration без виведення значень secrets."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from rf_sentinel.errors import ConfigurationError


def validate_device(index: int, gain: float | None) -> None:
    if type(index) is not int or not 0 <= index <= 255:
        raise ConfigurationError("Індекс RTL-пристрою має бути цілим числом від 0 до 255")
    if gain is not None and (
        type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 50
    ):
        raise ConfigurationError("Підсилення RTL має бути automatic або числом від 0 до 50")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    report_interval_minutes: int = 30
    rtl_device_index: int = 0
    rtl_gain: float | None = None
    data_dir: Path = field(default=Path("runtime"), repr=False)
    survey_low_hz: int = 24_000_000
    survey_high_hz: int = 1_766_000_000
    survey_bin_hz: int = 500_000
    survey_integration_seconds: int = 60
    survey_duration_seconds: int = 1800
    telegram_attempts: int = 3
    telegram_backoff_seconds: int = 5
    survey_recovery_seconds: int = 60
    timezone: str = "Europe/Kyiv"
    acquisition_low_hz: int = 24_000_000
    acquisition_high_hz: int = 1_766_000_000
    acquisition_bin_hz: int = 500_000
    acquisition_cadence_budget_seconds: float = 60
    acquisition_recovery_seconds: float = 60
    log_max_bytes: int = 5_000_000
    log_backups: int = 3

    def __post_init__(self) -> None:
        validate_device(self.rtl_device_index, self.rtl_gain)
        from rf_sentinel.acquisition import SweepProfile
        SweepProfile(self.acquisition_low_hz, self.acquisition_high_hz, self.acquisition_bin_hz)
        for value, minimum in ((self.acquisition_cadence_budget_seconds, 0.001),
                               (self.acquisition_recovery_seconds, 1)):
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 3600:
                raise ConfigurationError("Некоректний інтервал acquisition")
        if (type(self.log_max_bytes) is not int or not 1024 <= self.log_max_bytes <= 100_000_000
                or type(self.log_backups) is not int or not 1 <= self.log_backups <= 20):
            raise ConfigurationError("Некоректні межі rotation журналу")
        if (
            type(self.report_interval_minutes) is not int
            or not 1 <= self.report_interval_minutes <= 1440
        ):
            raise ConfigurationError("Інтервал звітів має бути від 1 до 1440 хвилин")
        token, chat = self.telegram_bot_token, self.telegram_chat_id
        if bool(token) != bool(chat):
            raise ConfigurationError("Для Telegram потрібні bot token і chat ID")
        if token and not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ConfigurationError("Некоректний формат Telegram bot token")
        if chat and not re.fullmatch(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,31}", chat):
            raise ConfigurationError("Некоректний формат Telegram chat ID")
        if not isinstance(self.data_dir, Path) or "\0" in str(self.data_dir):
            raise ConfigurationError("Некоректний каталог даних")
        if not 1 <= self.telegram_attempts <= 5:
            raise ConfigurationError("Кількість спроб Telegram має бути від 1 до 5")
        if not 0 <= self.telegram_backoff_seconds <= 300:
            raise ConfigurationError("Затримка Telegram має бути від 0 до 300 секунд")
        if not 1 <= self.survey_recovery_seconds <= 3600:
            raise ConfigurationError("Затримка відновлення має бути від 1 до 3600 секунд")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except (KeyError, ValueError):
            raise ConfigurationError("Невідомий часовий пояс") from None
        from rf_sentinel.spectrum import ScanProfile

        ScanProfile(
            self.survey_low_hz,
            self.survey_high_hz,
            self.survey_bin_hz,
            self.survey_integration_seconds,
            self.survey_duration_seconds,
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, acquisition_only: bool = False) -> "Settings":
        source = os.environ if env is None else env

        def read(name: str, default: str) -> str:
            if acquisition_only and not (
                name.startswith(("ACQUISITION_", "RTL_", "LOG_")) or name == "DATA_DIR"
            ):
                return default
            return source.get("RF_SENTINEL_" + name, default).strip()

        # Текст conversion exception може містити secret, помилково записаний у числове поле.
        try:
            acquisition = dict(
                acquisition_low_hz=int(read("ACQUISITION_START_HZ", "24000000")),
                acquisition_high_hz=int(read("ACQUISITION_STOP_HZ", "1766000000")),
                acquisition_bin_hz=int(read("ACQUISITION_BIN_HZ", "500000")),
                acquisition_cadence_budget_seconds=float(
                    read("ACQUISITION_CADENCE_BUDGET_SECONDS", "60")
                ),
                acquisition_recovery_seconds=float(read("ACQUISITION_RECOVERY_SECONDS", "60")),
                log_max_bytes=int(read("LOG_MAX_BYTES", "5000000")),
                log_backups=int(read("LOG_BACKUPS", "3")),
            )
            interval = int(read("REPORT_INTERVAL_MINUTES", "30"))
            device = int(read("RTL_DEVICE_INDEX", "0"))
            gain_text = read("RTL_GAIN", "auto")
            gain = None if gain_text.lower() in ("", "auto") else float(gain_text)
            low_hz = int(read("SURVEY_START_HZ", "24000000"))
            high_hz = int(read("SURVEY_STOP_HZ", "1766000000"))
            bin_hz = int(read("SURVEY_BIN_HZ", "500000"))
            integration = int(read("SURVEY_INTEGRATION_SECONDS", "60"))
            duration = int(read("SURVEY_DURATION_SECONDS", "1800"))
            telegram_attempts = int(read("TELEGRAM_ATTEMPTS", "3"))
            telegram_backoff = int(read("TELEGRAM_BACKOFF_SECONDS", "5"))
            recovery = int(read(
                "SURVEY_RECOVERY_DELAY_SECONDS",
                read("SURVEY_RECOVERY_SECONDS", "60"),
            ))
        except (ValueError, OverflowError):
            raise ConfigurationError("Некоректна числова конфігурація application") from None
        directory = read("DATA_DIR", "runtime")
        if not directory or "\0" in directory:
            raise ConfigurationError("Некоректний каталог даних")
        return cls(
            telegram_bot_token="" if acquisition_only else read("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id="" if acquisition_only else read("TELEGRAM_CHAT_ID", ""),
            report_interval_minutes=interval,
            rtl_device_index=device,
            rtl_gain=gain,
            data_dir=Path(directory),
            survey_low_hz=low_hz,
            survey_high_hz=high_hz,
            survey_bin_hz=bin_hz,
            survey_integration_seconds=integration,
            survey_duration_seconds=duration,
            telegram_attempts=telegram_attempts,
            telegram_backoff_seconds=telegram_backoff,
            survey_recovery_seconds=recovery,
            timezone=read("TIMEZONE", "Europe/Kyiv"),
            **acquisition,
        )
