"""Точка складання application зі збереженою identity-only поведінкою."""

import argparse
import logging
from pathlib import Path
import signal
import socket
import sys
from threading import Event, Thread

from rf_sentinel.config import Settings
from rf_sentinel.errors import SentinelError


def configure_logging(data_dir: Path, max_bytes: int = 5_000_000, backups: int = 3) -> None:
    from rf_sentinel.observability import configure_operational_logging
    configure_operational_logging(data_dir / "logs", max_bytes, backups)


def _shutdown_signal(signum, frame) -> None:
    raise KeyboardInterrupt


def _start_telegram_polling(settings: Settings, stop: Event, notifier):
    """Attach inbound Telegram to a long-running application mode."""
    if not settings.telegram_enabled:
        return None
    from rf_sentinel.telegram import TelegramPollingRuntime, TelegramReportHandler

    handler = TelegramReportHandler.from_settings(settings, notifier)
    runtime = TelegramPollingRuntime(
        settings.telegram_bot_token, handler, stop,
        attempts=settings.telegram_attempts,
        backoff_seconds=settings.telegram_backoff_seconds,
    )
    thread = Thread(target=runtime.run, name="telegram-inbound", daemon=True)
    thread.start()
    return thread


def _stop_telegram_polling(thread, stop: Event) -> None:
    stop.set()
    if thread is not None:
        thread.join(timeout=20)


class _DisabledNotifier:
    def send_package(self, package):
        from rf_sentinel.telegram import DeliveryResult
        return DeliveryResult("failed", {}, "telegram_disabled")

    def send_message(self, text):
        return None

    def send_photo(self, path, caption=""):
        return None


def run_station(settings: Settings) -> int:
    """Run acquisition, scheduled reports, and inbound Telegram in one process."""
    from rf_sentinel.acquisition import AsyncMeasurementSink, SpectrumAcquisitionWorker, SweepProfile
    from rf_sentinel.health import AcquisitionHealth, HealthOwner
    from rf_sentinel.observability import AcquisitionObserver
    from rf_sentinel.reporting import SQLiteReportEngine
    from rf_sentinel.rtl_power import RTLPowerScanner
    from rf_sentinel.scheduler import ScheduledReportRunner, run_report_scheduler
    from rf_sentinel.storage import SQLiteMeasurementSink
    from rf_sentinel.telegram import TelegramNotifier

    configure_logging(settings.data_dir, settings.log_max_bytes, settings.log_backups)
    stop = Event()
    health_path = settings.data_dir / "status" / "health.json"
    profile = SweepProfile(settings.acquisition_low_hz, settings.acquisition_high_hz,
                           settings.acquisition_bin_hz)
    state = AcquisitionHealth("rtl_power", profile.low_hz, profile.high_hz, profile.bin_hz,
                              settings.acquisition_cadence_budget_seconds,
                              settings.acquisition_recovery_seconds)
    health = HealthOwner(health_path, state)
    storage = SQLiteMeasurementSink(settings.sweeps_path,
                                    incident_retention=settings.incident_retention)
    sink = AsyncMeasurementSink(storage, close_downstream=False,
                                thread_name="station-measurement-writer")
    observer = AcquisitionObserver(health_path, profile,
                                   settings.acquisition_cadence_budget_seconds,
                                   settings.acquisition_recovery_seconds, storage=storage,
                                   health_owner=health)
    worker = SpectrumAcquisitionWorker(
        RTLPowerScanner(settings.rtl_device_index, settings.rtl_gain, stop=stop), sink,
        profile, observer, stop, settings.acquisition_cadence_budget_seconds,
        settings.acquisition_recovery_seconds,
    )
    notifier = (TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
                if settings.telegram_enabled else _DisabledNotifier())
    runner = ScheduledReportRunner(
        SQLiteReportEngine(settings.sweeps_path), notifier, settings.data_dir,
        timezone=settings.timezone, delivery_attempts=settings.telegram_attempts,
        storage=storage, health_path=health_path, state=state, health_owner=health,
    )

    def run_component(label, operation):
        try:
            operation()
        except BaseException:
            logging.getLogger("rf_sentinel.application").exception(
                "Station component stopped unexpectedly: %s", label)

    acquisition_thread = Thread(target=run_component, args=("acquisition", worker.run),
                                 name="station-acquisition")
    report_thread = Thread(
        target=run_component,
        args=("report-scheduler", lambda: run_report_scheduler(runner, stop)),
        name="station-reports",
    )
    inbound_thread = None
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_args: stop.set())
        health.save()
        logging.getLogger("rf_sentinel.application").info(
            "RF Sentinel station started: acquisition, reports, and Telegram lifecycle shared")
        acquisition_thread.start()
        report_thread.start()
        if settings.telegram_enabled:
            inbound_thread = _start_telegram_polling(settings, stop, notifier)
        while not stop.wait(0.2):
            if not acquisition_thread.is_alive() and not report_thread.is_alive():
                stop.set()
    finally:
        stop.set()
        acquisition_thread.join(timeout=30)
        report_thread.join(timeout=10)
        _stop_telegram_polling(inbound_thread, stop)
        if acquisition_thread.is_alive() or report_thread.is_alive():
            logging.getLogger("rf_sentinel.application").error(
                "Station shutdown exceeded component deadline")
        try:
            sink.close(timeout=10)
        except BaseException:
            logging.getLogger("rf_sentinel.application").exception(
                "Station measurement sink close failed")
        try:
            storage.close()
        except BaseException:
            logging.getLogger("rf_sentinel.application").exception(
                "Station SQLite close failed")
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rf_sentinel")
    parser.add_argument("mode", nargs="?", choices=("survey", "schedule", "report-schedule", "acquire", "station"))
    # main() без аргументів не читає аргументи pytest або host process.
    args = parser.parse_args([] if argv is None else argv)
    if args.mode is None:
        print("RF Sentinel")
        return 0
    try:
        settings = Settings.from_env(acquisition_only=True) if args.mode == "acquire" else Settings.from_env()
        if args.mode == "acquire":
            return run_acquisition(settings)
        if args.mode == "station":
            return run_station(settings)
        if args.mode == "report-schedule":
            from rf_sentinel.reporting import SQLiteReportEngine
            from rf_sentinel.scheduler import ScheduledReportRunner, run_report_scheduler
            from rf_sentinel.telegram import TelegramNotifier

            configure_logging(settings.data_dir, settings.log_max_bytes, settings.log_backups)
            from rf_sentinel.storage import SQLiteMeasurementSink
            notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id) \
                if settings.telegram_enabled else None
            if notifier is None:
                logger = logging.getLogger("rf_sentinel.application")
                logger.error("Report scheduler requires Telegram configuration")
                return 1
            storage = SQLiteMeasurementSink(settings.sweeps_path,
                                            incident_retention=settings.incident_retention)
            runner = ScheduledReportRunner(
                SQLiteReportEngine(settings.sweeps_path), notifier, settings.data_dir,
                timezone=settings.timezone, delivery_attempts=settings.telegram_attempts,
                storage=storage,
            )
            stop = Event()
            inbound_thread = _start_telegram_polling(settings, stop, notifier)
            previous = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _shutdown_signal)
            try:
                run_report_scheduler(runner, stop)
            finally:
                _stop_telegram_polling(inbound_thread, stop)
                signal.signal(signal.SIGTERM, previous)
                storage.close()
            return 0
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
        inbound_thread = _start_telegram_polling(settings, stop, notifier)
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _shutdown_signal)
        try:
            run_continuous(
                workflow.run, settings.survey_recovery_seconds, stop,
                settings.data_dir / "status" / "health.json",
                notify_status=workflow.notify_status,
            )
        finally:
            _stop_telegram_polling(inbound_thread, stop)
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
    from rf_sentinel.acquisition import AsyncMeasurementSink, SpectrumAcquisitionWorker, SweepProfile
    from rf_sentinel.observability import AcquisitionObserver, configure_operational_logging
    from rf_sentinel.rtl_power import RTLPowerScanner
    from rf_sentinel.storage import SQLiteMeasurementSink

    configure_operational_logging(settings.data_dir / "logs", settings.log_max_bytes,
                                  settings.log_backups)
    profile = SweepProfile(settings.acquisition_low_hz, settings.acquisition_high_hz,
                           settings.acquisition_bin_hz)
    stop = Event()
    previous = {}
    storage = None
    sink = None
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _shutdown_signal)
        storage = SQLiteMeasurementSink(settings.sweeps_path,
                                        incident_retention=settings.incident_retention)
        sink = AsyncMeasurementSink(storage)
        observer = AcquisitionObserver(settings.data_dir / "status" / "health.json", profile,
                                       settings.acquisition_cadence_budget_seconds,
                                       settings.acquisition_recovery_seconds, storage=storage)
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
