"""Точка складання application зі збереженою identity-only поведінкою."""

import argparse
import logging
from pathlib import Path
import signal
import socket
import sys
from threading import Event

from rf_sentinel.config import Settings
from rf_sentinel.errors import SentinelError


def configure_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handlers = [logging.StreamHandler(), logging.FileHandler(log_dir / "rf-sentinel.log")]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def _shutdown_signal(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rf_sentinel")
    parser.add_argument("mode", nargs="?", choices=("survey", "schedule"))
    # main() без аргументів не читає аргументи pytest або host process.
    args = parser.parse_args([] if argv is None else argv)
    if args.mode is None:
        print("RF Sentinel")
        return 0
    try:
        settings = Settings.from_env()
        from rf_sentinel.rtl_power import RTLPowerScanner
        from rf_sentinel.scheduler import run_continuous
        from rf_sentinel.spectrum import ScanProfile
        from rf_sentinel.telegram import TelegramNotifier
        from rf_sentinel.workflow import SurveyWorkflow

        notifier = None
        if settings.telegram_enabled:
            notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        profile = ScanProfile(
            settings.survey_low_hz, settings.survey_high_hz, settings.survey_bin_hz,
            settings.survey_integration_seconds, settings.survey_duration_seconds,
        )
        workflow = SurveyWorkflow(
            RTLPowerScanner(settings.rtl_device_index, settings.rtl_gain),
            profile, settings.data_dir, f"RF Sentinel / {socket.gethostname()}", notifier,
            timezone=settings.timezone, telegram_attempts=settings.telegram_attempts,
            telegram_backoff_seconds=settings.telegram_backoff_seconds,
        )
        if args.mode == "survey":
            configure_logging(settings.data_dir)
            outcome = workflow.run()
            print(f"Огляд: {outcome.scan_status}; Telegram: {outcome.notification_status}")
            return 0 if outcome.scan_status == "success" else 1
        configure_logging(settings.data_dir)
        logger = logging.getLogger("rf_sentinel.application")
        logger.info("RF Sentinel запущено; конфігурацію перевірено; monitoring активний")
        stop = Event()
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _shutdown_signal)
        try:
            run_continuous(
                workflow.run, settings.survey_recovery_seconds, stop,
                settings.data_dir / "status.json",
            )
        finally:
            signal.signal(signal.SIGTERM, previous)
        return 0
    except KeyboardInterrupt:
        logging.getLogger("rf_sentinel.application").info("Отримано команду завершення")
        logging.shutdown()
        return 130
    except SentinelError:
        print("RF Sentinel: помилка конфігурації або огляду; перевірте локальне середовище",
              file=sys.stderr)
        return 1
