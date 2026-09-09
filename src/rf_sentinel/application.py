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
    from rf_sentinel.observability import configure_operational_logging
    configure_operational_logging(data_dir / "logs")


def _shutdown_signal(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rf_sentinel")
    parser.add_argument("mode", nargs="?", choices=("survey", "schedule", "acquire"))
    # main() без аргументів не читає аргументи pytest або host process.
    args = parser.parse_args([] if argv is None else argv)
    if args.mode is None:
        print("RF Sentinel")
        return 0
    try:
        settings = Settings.from_env(acquisition_only=True) if args.mode == "acquire" else Settings.from_env()
        if args.mode == "acquire":
            return run_acquisition(settings)
        from rf_sentinel.rtl_power import RTLPowerSurveyAdapter
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
            RTLPowerSurveyAdapter(settings.rtl_device_index, settings.rtl_gain),
            profile, settings.data_dir, f"RF Sentinel / {socket.gethostname()}", notifier,
            timezone=settings.timezone, telegram_attempts=settings.telegram_attempts,
            telegram_backoff_seconds=settings.telegram_backoff_seconds,
            send_failure_reports=args.mode == "survey",
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
                settings.data_dir / "status" / "health.json",
                notify_status=workflow.notify_status,
            )
        finally:
            signal.signal(signal.SIGTERM, previous)
        return 0
    except KeyboardInterrupt:
        logging.getLogger("rf_sentinel.application").info("Отримано команду завершення")
        logging.shutdown()
        return 130
    except (SentinelError, OSError):
        print("RF Sentinel: помилка конфігурації або огляду; перевірте локальне середовище",
              file=sys.stderr)
        return 1


def run_acquisition(settings: Settings) -> int:
    """Складає незалежний acquisition без імпорту reporting і Telegram."""
    from rf_sentinel.acquisition import SpectrumAcquisitionWorker, SweepProfile
    from rf_sentinel.observability import AcquisitionObserver, configure_operational_logging
    from rf_sentinel.rtl_power import RTLPowerScanner
    from rf_sentinel.storage import SQLiteMeasurementSink

    configure_operational_logging(settings.data_dir / "logs", settings.log_max_bytes,
                                  settings.log_backups)
    profile = SweepProfile(settings.acquisition_low_hz, settings.acquisition_high_hz,
                           settings.acquisition_bin_hz)
    stop = Event()
    previous = {}
    sink = None
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _shutdown_signal)
        sink = SQLiteMeasurementSink(settings.data_dir / "sweeps.sqlite3",
                                     incident_retention=settings.incident_retention)
        observer = AcquisitionObserver(settings.data_dir / "status" / "health.json", profile,
                                       settings.acquisition_cadence_budget_seconds,
                                       settings.acquisition_recovery_seconds, storage=sink)
        SpectrumAcquisitionWorker(
            RTLPowerScanner(settings.rtl_device_index, settings.rtl_gain), sink,
            profile, observer, stop, settings.acquisition_cadence_budget_seconds,
            settings.acquisition_recovery_seconds,
        ).run()
        return 0
    except OSError:
        logging.getLogger(__name__).error("Помилка запису operational artifacts")
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        logging.shutdown()
