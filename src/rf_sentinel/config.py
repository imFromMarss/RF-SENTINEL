"""Environment boundary; never echo environment values or secret representations."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from rf_sentinel.errors import ConfigurationError


def validate_device(index: int, gain: float | None) -> None:
    if type(index) is not int or not 0 <= index <= 255:
        raise ConfigurationError("RTL device index must be an integer from 0 to 255")
    if gain is not None and (
        type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 50
    ):
        raise ConfigurationError("RTL gain must be auto or a finite value from 0 to 50")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    report_interval_minutes: int = 30
    rtl_device_index: int = 0
    rtl_gain: float | None = None
    data_dir: Path = field(default=Path("runtime"), repr=False)

    def __post_init__(self) -> None:
        validate_device(self.rtl_device_index, self.rtl_gain)
        if (
            type(self.report_interval_minutes) is not int
            or not 1 <= self.report_interval_minutes <= 1440
        ):
            raise ConfigurationError("Report interval must be 1 to 1440 minutes")
        token, chat = self.telegram_bot_token, self.telegram_chat_id
        if bool(token) != bool(chat):
            raise ConfigurationError("Telegram requires both bot token and chat ID")
        if token and not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ConfigurationError("Invalid Telegram bot token format")
        if chat and not re.fullmatch(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,31}", chat):
            raise ConfigurationError("Invalid Telegram chat ID format")
        if not isinstance(self.data_dir, Path) or "\0" in str(self.data_dir):
            raise ConfigurationError("Invalid data directory")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env

        def read(name: str, default: str) -> str:
            return source.get("RF_SENTINEL_" + name, default).strip()

        # Conversion exceptions can contain secrets supplied to the wrong variable.
        try:
            interval = int(read("REPORT_INTERVAL_MINUTES", "30"))
            device = int(read("RTL_DEVICE_INDEX", "0"))
            gain_text = read("RTL_GAIN", "auto")
            gain = None if gain_text.lower() in ("", "auto") else float(gain_text)
        except (ValueError, OverflowError):
            raise ConfigurationError("Invalid numeric application configuration") from None
        directory = read("DATA_DIR", "runtime")
        if not directory or "\0" in directory:
            raise ConfigurationError("Invalid data directory")
        return cls(
            telegram_bot_token=read("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=read("TELEGRAM_CHAT_ID", ""),
            report_interval_minutes=interval,
            rtl_device_index=device,
            rtl_gain=gain,
            data_dir=Path(directory),
        )
