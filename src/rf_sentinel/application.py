"""Точка складання application зі збереженою identity-only поведінкою."""

import argparse
from dataclasses import dataclass
import fcntl
import logging
from pathlib import Path
import signal
import socket
import sys
import time
from threading import Event, Lock, Thread

from rf_sentinel.config import Settings
from rf_sentinel.errors import SentinelError


def configure_logging(data_dir: Path, max_bytes: int = 5_000_000, backups: int = 3,
                      level: str = "INFO") -> None:
    from rf_sentinel.observability import configure_operational_logging
    configure_operational_logging(data_dir / "logs", max_bytes, backups, level)


def _shutdown_signal(signum, frame) -> None:
    raise KeyboardInterrupt


def _bounded_close(resource, *, deadline: float, thread_name: str,
                   survivor_registry: list | None = None):
    """Close one station-owned resource without waiting past an absolute deadline."""
    outcome = {"started_at": time.monotonic()}

    def close_resource():
        try:
            resource.close()
        except BaseException as error:
            outcome["error"] = error
        finally:
            # Only the helper can state when downstream close actually ended.
            # Caller observation after join is not a completion timestamp.
            outcome["completed_at"] = time.monotonic()

    close_thread = Thread(target=close_resource, name=thread_name, daemon=True)
    close_thread.start()
    close_thread.join(max(0.0, deadline - time.monotonic()))
    if close_thread.is_alive():
        if survivor_registry is not None:
            survivor_registry.append(close_thread)
        return "incomplete", TimeoutError(f"{thread_name} did not close before deadline")
    completed_at = outcome.get("completed_at")
    if completed_at is None or completed_at > deadline:
        return "incomplete", TimeoutError(f"{thread_name} completed after deadline")
    if "error" in outcome:
        return "failed", outcome["error"]
    return "complete", None


def _start_telegram_polling(settings: Settings, stop: Event, notifier, *, on_failure=None):
    """Attach inbound Telegram to a long-running application mode."""
    if not settings.telegram_enabled:
        return None
    from rf_sentinel.telegram import TelegramPollingRuntime, TelegramReportHandler

    coordinator = getattr(notifier, "coordinator", None)
    handler = TelegramReportHandler.from_settings(settings, notifier, coordinator=coordinator)
    runtime = TelegramPollingRuntime(
        settings.telegram_bot_token, handler, stop,
        attempts=settings.telegram_attempts,
        backoff_seconds=settings.telegram_backoff_seconds,
        coordinator=coordinator,
    )
    def run_managed():
        try:
            runtime.run()
        except BaseException as error:
            if on_failure is None:
                raise
            on_failure(error)
            return
        if not stop.is_set() and on_failure is not None:
            on_failure(None)

    thread = Thread(target=run_managed, name="telegram-inbound", daemon=True)
    thread.start()
    return thread


def _stop_telegram_polling(thread, stop: Event, *, deadline: float | None = None) -> None:
    stop.set()
    if thread is not None:
        stop_runtime = getattr(thread, "stop", None)
        if stop_runtime is not None:
            stop_runtime()
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        thread.join(timeout=timeout)


class _StationProcessLock:
    """Own the station-wide Linux lock for the complete process lifecycle."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+")
        try:
            fcntl.flock(self._stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._stream.close()
            self._stream = None
            return False
        return True

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            fcntl.flock(self._stream, fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


_retained_station_locks: set[_StationProcessLock] = set()
_retained_station_locks_guard = Lock()


@dataclass(frozen=True)
class _StationLifecycleResult:
    exit_code: int
    critical_survivors: tuple[object, ...] = ()


def _retain_station_lock(lock: _StationProcessLock, survivors: tuple[object, ...]) -> None:
    """Keep station ownership until surviving critical runtimes actually end."""
    logger = logging.getLogger("rf_sentinel.application")
    with _retained_station_locks_guard:
        # Process-owned retention is independent of the guardian thread's
        # lifetime.  The OS closes the descriptor if the process terminates.
        _retained_station_locks.add(lock)

    def release_after_survivors():
        try:
            for survivor in survivors:
                while True:
                    try:
                        alive = survivor.is_alive()
                    except BaseException:
                        logger.exception(
                            "Station lock guardian could not probe critical survivor")
                        time.sleep(1.0)
                        continue
                    if not alive:
                        break
                    # The guardian is daemonized, so this never delays process
                    # termination; finite joins also keep this helper compatible
                    # with runtimes whose join contract requires a timeout.
                    try:
                        survivor.join(timeout=1.0)
                    except BaseException:
                        logger.exception(
                            "Station lock guardian could not join critical survivor")
                        time.sleep(1.0)
                        continue
                    # Some protocol implementations return immediately while
                    # still alive; avoid spinning between probes.
                    time.sleep(0.1)
        except BaseException:
            # The process-owned registry keeps the descriptor strongly held if
            # the guardian itself exits before proving terminal state.
            logger.exception("Station lock guardian stopped before terminal confirmation")
            return
        try:
            lock.release()
        except BaseException:
            logger.exception("Station lock guardian failed to release station lock")
            return
        with _retained_station_locks_guard:
            _retained_station_locks.discard(lock)

    try:
        Thread(target=release_after_survivors,
               name="station-lock-guardian", daemon=True).start()
    except BaseException:
        # Thread creation/start failure is also fail-closed: the registry stays
        # the durable process-lifetime owner of the open lock descriptor.
        logger.exception("Station lock guardian failed to start")


def run_station(settings: Settings) -> int:
    """Acquire the station lock, then run the unified station lifecycle."""
    configure_logging(settings.data_dir, settings.log_max_bytes, settings.log_backups,
                      settings.log_level)
    lock = _StationProcessLock(settings.data_dir / "station.lock")
    if not lock.acquire():
        logging.getLogger("rf_sentinel.application").error(
            "RF Sentinel station is already running; refusing second instance")
        return 1
    release_lock = True
    critical_survivors = []
    try:
        result = _run_station_lifecycle(
            settings, critical_survivors=critical_survivors)
        if result.critical_survivors:
            _retain_station_lock(lock, result.critical_survivors)
            release_lock = False
        return result.exit_code
    except BaseException:
        if critical_survivors:
            _retain_station_lock(lock, tuple(dict.fromkeys(critical_survivors)))
            release_lock = False
        raise
    finally:
        if release_lock:
            lock.release()


def _run_station_lifecycle(
        settings: Settings, *, critical_survivors: list | None = None,
) -> _StationLifecycleResult:
    """Run acquisition, scheduled reports, and inbound Telegram in one process."""
    from rf_sentinel.acquisition import AsyncMeasurementSink, SpectrumAcquisitionWorker, SweepProfile
    from rf_sentinel.health import AcquisitionHealth, HealthOwner
    from rf_sentinel.observability import (AcquisitionObserver, LinuxResourceSampler,
                                           ObservabilityCoordinator, RunSummaryWriter)
    from rf_sentinel.reporting import SQLiteReportEngine
    from rf_sentinel.rtl_power import RTLPowerScanner
    from rf_sentinel.scheduler import ScheduledReportRunner, run_report_scheduler
    from rf_sentinel.storage import SQLiteMeasurementSink
    from rf_sentinel.telegram import TelegramNotifier

    stop = Event()
    coordinator = ObservabilityCoordinator()
    summary = RunSummaryWriter(settings.data_dir / "status" / "run-summary.json")
    try:
        summary.write_start_marker()
    except (OSError, ValueError, TypeError):
        logging.getLogger("rf_sentinel.application").exception(
            "Run summary startup marker write failed; station continues")
    health_path = settings.data_dir / "status" / "health.json"
    profile = SweepProfile(settings.acquisition_low_hz, settings.acquisition_high_hz,
                           settings.acquisition_bin_hz)
    state = AcquisitionHealth("rtl_power", profile.low_hz, profile.high_hz, profile.bin_hz,
                              settings.acquisition_cadence_budget_seconds,
                              settings.acquisition_recovery_seconds)
    health = HealthOwner(health_path, state)
    component_failures = []
    component_failure_lock = Lock()
    final_sink_snapshot = None
    exit_code = 0
    acquisition_storage = report_storage = sink = None
    critical_survivors = [] if critical_survivors is None else critical_survivors

    def record_component_failure(label, error, *, category=None, code=None, message=None,
                                 only_if_stop_unset=False):
        with component_failure_lock:
            if only_if_stop_unset and stop.is_set():
                return False
            component_failures.append({
                "timestamp": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
                "component": label,
                "category": category or type(error).__name__,
                "code": code if code is not None else getattr(error, "code", None),
                "message": message if message is not None else str(error),
                "correlation_id": getattr(error, "correlation_id", None),
            })
            state.application_status = "failed"
            stop.set()
            return True

    def writer_terminal_failure(error):
        if record_component_failure(
                "measurement-writer", error, category="persistence",
                code="writer_failure", message="Measurement writer persistence failed",
                only_if_stop_unset=True):
            logging.getLogger("rf_sentinel.application").error(
                "Station measurement writer stopped after persistence failure")

    def run_component(label, operation):
        try:
            operation()
        except BaseException as error:
            record_component_failure(label, error)
            logging.getLogger("rf_sentinel.application").exception(
                "Station component stopped unexpectedly: %s", label)
        else:
            message = f"{label} runtime exited before coordinated stop"
            if record_component_failure(
                    label, RuntimeError(message), category="runtime",
                    code="premature_exit", message=message,
                    only_if_stop_unset=True):
                logging.getLogger("rf_sentinel.application").error(message)

    def telegram_runtime_failure(error):
        if error is None:
            record_component_failure(
                "telegram", RuntimeError("Telegram runtime exited before coordinated stop"),
                category="runtime", code="premature_exit",
                message="Telegram runtime exited before coordinated stop",
            )
            logging.getLogger("rf_sentinel.application").error(
                "Telegram runtime exited before coordinated stop")
        else:
            record_component_failure("telegram", error)
            logging.getLogger("rf_sentinel.application").error(
                "Telegram runtime stopped unexpectedly: %s", type(error).__name__)

    try:
        acquisition_storage = SQLiteMeasurementSink(
            settings.sweeps_path, incident_retention=settings.incident_retention)
        report_storage = SQLiteMeasurementSink(
            settings.sweeps_path, incident_retention=settings.incident_retention)
        # Construction is complete before this station-owned daemon writer is
        # started. Bounded cleanup still drains it; daemon status is the hard
        # process-termination fallback after the shared deadline.
        sink = AsyncMeasurementSink(
            acquisition_storage, thread_name="station-measurement-writer",
            writer_daemon=True, start_immediately=False,
            on_terminal_failure=writer_terminal_failure,
        )
        observer = AcquisitionObserver(
            health_path, profile, settings.acquisition_cadence_budget_seconds,
            settings.acquisition_recovery_seconds, storage=acquisition_storage,
            health_owner=health, coordinator=coordinator)
        scanner = RTLPowerScanner(settings.rtl_device_index, settings.rtl_gain, stop=stop)
        worker = SpectrumAcquisitionWorker(
            scanner, sink,
            profile, observer, stop, settings.acquisition_cadence_budget_seconds,
            settings.acquisition_recovery_seconds,
            sink_lifecycle_owner="station",
        )
        notifier = (TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id,
                                     coordinator=coordinator)
                    if settings.telegram_enabled else None)
        report_engine = SQLiteReportEngine(settings.sweeps_path)
        try:
            report_engine.coordinator = coordinator
        except AttributeError:
            pass
        runner = ScheduledReportRunner(
            report_engine, notifier, settings.data_dir,
            timezone=settings.timezone, delivery_attempts=settings.telegram_attempts,
            storage=report_storage, health_path=health_path, state=state, health_owner=health,
            coordinator=coordinator,
        )
        resource_sampler = LinuxResourceSampler(
            health, settings.resource_sampling_interval_seconds)
        acquisition_thread = Thread(
            target=run_component, args=("acquisition", worker.run),
            name="station-acquisition", daemon=True)
        report_thread = Thread(
            target=run_component,
            args=("report-scheduler", lambda: run_report_scheduler(runner, stop)),
            name="station-reports", daemon=True,
        )
    except BaseException:
        stop.set()
        startup_deadline = time.monotonic() + 30
        if sink is not None:
            try:
                sink.close(deadline=startup_deadline)
            except BaseException:
                logging.getLogger("rf_sentinel.application").exception(
                    "Station acquisition storage cleanup failed during startup")
            finally:
                try:
                    critical_survivors.extend(
                        getattr(sink, "lifecycle_survivors", ()))
                except BaseException:
                    logging.getLogger("rf_sentinel.application").exception(
                        "Station startup survivor accounting failed")
        elif acquisition_storage is not None:
            _bounded_close(acquisition_storage, deadline=startup_deadline,
                           thread_name="station-startup-acquisition-storage-close",
                           survivor_registry=critical_survivors)
        if report_storage is not None:
            _bounded_close(report_storage, deadline=startup_deadline,
                           thread_name="station-startup-report-storage-close",
                           survivor_registry=critical_survivors)
        raise

    inbound_thread = None
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda *_args: stop.set())
        health.save()
        logging.getLogger("rf_sentinel.application").info(
            "RF Sentinel station started: acquisition, reports, and Telegram lifecycle shared")
        start_sink = getattr(sink, "start", None)
        if start_sink is not None:
            start_sink()
        acquisition_thread.start()
        report_thread.start()
        resource_sampler.start()
        if settings.telegram_enabled:
            inbound_thread = _start_telegram_polling(
                settings, stop, notifier, on_failure=telegram_runtime_failure)
        while not stop.wait(0.2):
            if not acquisition_thread.is_alive() and not report_thread.is_alive():
                stop.set()
    finally:
        shutdown_deadline = time.monotonic() + 30

        def shutdown_failure(component, reason, code="incomplete"):
            component_failures.append({
                "timestamp": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
                "component": component,
                "category": "shutdown",
                "code": code,
                "message": reason,
            })
            state.application_status = "failed"
            state.shutdown_status = "incomplete"

        def remaining_shutdown_budget():
            return max(0.0, shutdown_deadline - time.monotonic())

        stop.set()
        stop_active_process = getattr(scanner, "stop_active_process", None)
        if stop_active_process is not None:
            try:
                stop_active_process(deadline=shutdown_deadline)
            except BaseException as error:
                shutdown_failure(
                    "rtl_power", f"Active rtl_power cleanup failed: {type(error).__name__}",
                    code="shutdown_error")
                logging.getLogger("rf_sentinel.application").exception(
                    "Station supervisor failed to reap active rtl_power")
        scanner_is_alive = getattr(scanner, "is_alive", None)
        if scanner_is_alive is not None:
            try:
                scanner_active = scanner_is_alive()
            except BaseException as error:
                scanner_active = True
                shutdown_failure(
                    "rtl_power", f"Active rtl_power state check failed: {type(error).__name__}",
                    code="shutdown_error")
            if scanner_active:
                critical_survivors.append(scanner)
                if not any(item["component"] == "rtl_power" for item in component_failures):
                    shutdown_failure(
                        "rtl_power", "Active rtl_power remained unresolved after cleanup")
        try:
            sampler_stopped = resource_sampler.stop(timeout_seconds=remaining_shutdown_budget())
        except BaseException as error:
            sampler_stopped = False
            shutdown_failure("resource-sampler", f"Resource telemetry sampler failed to stop: {type(error).__name__}")
        if not sampler_stopped:
            if not any(item["component"] == "resource-sampler" for item in component_failures):
                shutdown_failure("resource-sampler", "Resource telemetry sampler did not stop before shared shutdown deadline")
            sampler_thread = getattr(resource_sampler, "_thread", None)
            if sampler_thread is not None:
                critical_survivors.append(sampler_thread)
        try:
            acquisition_thread.join(timeout=remaining_shutdown_budget())
        except BaseException as error:
            shutdown_failure(
                "acquisition", f"Acquisition worker join failed: {type(error).__name__}",
                code="shutdown_error")
        try:
            acquisition_stopped = not acquisition_thread.is_alive()
        except BaseException as error:
            acquisition_stopped = False
            shutdown_failure(
                "acquisition", f"Acquisition worker alive check failed: {type(error).__name__}",
                code="shutdown_error")
        # Close the narrow race where acquisition passed its stop check before
        # the first supervisor lookup and registered rtl_power during join.
        if scanner_is_alive is not None:
            try:
                late_scanner_active = scanner_is_alive()
            except BaseException:
                late_scanner_active = True
            if late_scanner_active and stop_active_process is not None:
                try:
                    stop_active_process(deadline=shutdown_deadline)
                    late_scanner_active = scanner_is_alive()
                except BaseException as error:
                    if not any(item["component"] == "rtl_power"
                               for item in component_failures):
                        shutdown_failure(
                            "rtl_power",
                            f"Late rtl_power cleanup failed: {type(error).__name__}",
                            code="shutdown_error")
            if late_scanner_active:
                critical_survivors.append(scanner)
        if not acquisition_stopped:
            critical_survivors.append(acquisition_thread)
            if not any(item["component"] == "acquisition" and
                       item["category"] == "shutdown" for item in component_failures):
                shutdown_failure(
                    "acquisition", "Acquisition worker remained alive after shared shutdown deadline")
        try:
            report_thread.join(timeout=remaining_shutdown_budget())
        except BaseException as error:
            shutdown_failure(
                "report-scheduler", f"Report scheduler join failed: {type(error).__name__}",
                code="shutdown_error")
        try:
            report_stopped = not report_thread.is_alive()
        except BaseException as error:
            report_stopped = False
            shutdown_failure(
                "report-scheduler", f"Report scheduler alive check failed: {type(error).__name__}",
                code="shutdown_error")
        if not report_stopped and not any(
                item["component"] == "report-scheduler" and
                item["category"] == "shutdown" for item in component_failures):
            shutdown_failure("report-scheduler", "Report scheduler remained alive after shared shutdown deadline")
        if not report_stopped:
            critical_survivors.append(report_thread)
        try:
            _stop_telegram_polling(inbound_thread, stop, deadline=shutdown_deadline)
        except BaseException as error:
            shutdown_failure(
                "telegram", f"Telegram runtime stop/join failed: {type(error).__name__}",
                code="shutdown_error")
            logging.getLogger("rf_sentinel.application").exception(
                "Telegram runtime stop/join failed")
        try:
            telegram_alive = inbound_thread is not None and inbound_thread.is_alive()
        except BaseException as error:
            # An unreadable runtime state is unresolved, not proof of shutdown.
            # Register it with the existing guardian so station ownership is
            # retained until a later terminal-state check succeeds.
            telegram_alive = True
            shutdown_failure(
                "telegram", f"Telegram runtime alive check failed: {type(error).__name__}",
                code="shutdown_error")
            logging.getLogger("rf_sentinel.application").exception(
                "Telegram runtime alive check failed")
        if telegram_alive:
            shutdown_failure(
                "telegram", "Telegram polling thread remained alive after shared shutdown deadline")
            critical_survivors.append(inbound_thread)
        # Closing marks the sink terminal before draining. A late acquisition
        # producer is rejected instead of leaving the writer open indefinitely.
        try:
            sink.close(deadline=shutdown_deadline)
        except BaseException as error:
            shutdown_failure(
                "storage", f"Measurement sink failed to stop cleanly: {type(error).__name__}",
                code="shutdown_error")
            logging.getLogger("rf_sentinel.application").exception(
                "Station measurement sink close failed")
        incidents = []
        incident_source = "available"
        try:
            # The existing incident table is already retention-bounded.  Read
            # its chronological view and let the summary keep only the newest
            # bounded tail.
            incidents = report_storage.query_incidents()
        except BaseException:
            incident_source = "unavailable"
            logging.getLogger("rf_sentinel.application").exception(
                "Run summary incident history read failed; shutdown continues")
        try:
            report_close_status, report_close_error = _bounded_close(
                report_storage, deadline=shutdown_deadline,
                thread_name="station-report-storage-close",
                survivor_registry=critical_survivors)
        except BaseException as error:
            shutdown_failure(
                "report-storage", f"Station report SQLite close failed: {type(error).__name__}",
                code="shutdown_error")
            logging.getLogger("rf_sentinel.application").exception(
                "Station report SQLite bounded close failed")
        else:
            if report_close_status != "complete":
                error_name = type(report_close_error).__name__
                reason = ("Station report SQLite close did not finish before shared shutdown "
                          "deadline" if report_close_status == "incomplete" else
                          f"Station report SQLite close failed: {error_name}")
                shutdown_failure(
                    "report-storage", reason,
                    code=("incomplete" if report_close_status == "incomplete"
                          else "shutdown_error"))
                if report_close_status == "failed":
                    logging.getLogger("rf_sentinel.application").error(
                        "Station report SQLite close failed: %s", error_name)
        for sig, handler in previous.items():
            signal.signal(sig, handler)

        # All owned storage close outcomes are known before the one final
        # capture.  Health and run-summary consume this same detached value.
        storage_shutdown_incomplete = any(
            item["component"] in {"storage", "report-storage"}
            and item["category"] == "shutdown"
            for item in component_failures
        )
        state.application_status = "failed" if component_failures else "stopped"
        state.shutdown_status = "incomplete" if component_failures else "complete"
        try:
            final_sink_snapshot = observer.capture_final_sink_snapshot(sink)
            if final_sink_snapshot is not None and storage_shutdown_incomplete:
                final_sink_snapshot["shutdown_complete"] = False
                final_sink_snapshot["shutdown_status"] = "incomplete"
        except BaseException as error:
            shutdown_failure(
                "storage", f"Final measurement sink snapshot failed: {type(error).__name__}")
            logging.getLogger("rf_sentinel.application").exception(
                "Final measurement sink snapshot capture failed")
        if final_sink_snapshot is None and not any(
                item["component"] == "storage" and item["category"] == "shutdown"
                for item in component_failures):
            shutdown_failure("storage", "Final measurement sink snapshot unavailable")

        try:
            sink_survivors = getattr(sink, "lifecycle_survivors", ())
            critical_survivors.extend(sink_survivors)
        except BaseException:
            shutdown_failure("storage", "Measurement sink survivor state unavailable")

        sink_failure = summary._sink_shutdown_failure(final_sink_snapshot)
        final_non_clean = bool(component_failures or sink_failure is not None)
        state.application_status = "failed" if final_non_clean else "stopped"
        state.shutdown_status = "incomplete" if final_non_clean else "complete"
        if final_sink_snapshot is not None:
            try:
                observer.publish_final_sink_snapshot(final_sink_snapshot)
            except BaseException as error:
                shutdown_failure("health-publication",
                                 f"Final health snapshot publication failed: {type(error).__name__}",
                                 code="publication_failed")
                logging.getLogger("rf_sentinel.application").exception(
                    "Final measurement sink health snapshot publication failed")
                try:
                    # The publication failure itself changes the canonical
                    # outcome. Reuse the same detached sink snapshot to make
                    # that non-clean result visible when the writer recovers.
                    observer.publish_final_sink_snapshot(final_sink_snapshot)
                except BaseException:
                    logging.getLogger("rf_sentinel.application").exception(
                        "Corrective health publication after health failure failed")
        else:
            try:
                observer.save()
            except BaseException as error:
                shutdown_failure("health-publication",
                                 f"Final health snapshot publication failed: {type(error).__name__}",
                                 code="publication_failed")
                logging.getLogger("rf_sentinel.application").exception(
                    "Final health publication without sink snapshot failed")
        try:
            final_non_clean = bool(component_failures or sink_failure is not None)
            exit_code = 1 if final_non_clean else 0
            summary.write(settings=settings, state=state, coordinator=coordinator,
                          status=("failed" if final_non_clean else "stopped"),
                          component_failures=component_failures,
                          incidents=incidents, incident_source=incident_source,
                          sink_snapshot=final_sink_snapshot)
        except (OSError, ValueError, TypeError):
            exit_code = 1
            state.application_status = "failed"
            logging.getLogger("rf_sentinel.application").exception(
                "Run summary write failed; shutdown continues")
            try:
                # Health was already published before the summary attempt.
                # Correct it with the same detached sink snapshot; never
                # resample or re-close the sink on this failure path.
                if final_sink_snapshot is not None:
                    observer.publish_final_sink_snapshot(final_sink_snapshot)
                else:
                    observer.save()
            except BaseException:
                logging.getLogger("rf_sentinel.application").exception(
                    "Corrective health publication after run summary failure failed")
    return _StationLifecycleResult(exit_code, tuple(dict.fromkeys(critical_survivors)))


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

            configure_logging(settings.data_dir, settings.log_max_bytes, settings.log_backups,
                              settings.log_level)
            from rf_sentinel.storage import SQLiteMeasurementSink
            notifier = (TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
                        if settings.telegram_enabled else None)
            storage = SQLiteMeasurementSink(settings.sweeps_path,
                                            incident_retention=settings.incident_retention)
            runner = ScheduledReportRunner(
                SQLiteReportEngine(settings.sweeps_path), notifier, settings.data_dir,
                timezone=settings.timezone, delivery_attempts=settings.telegram_attempts,
                storage=storage,
            )
            stop = Event()
            inbound_thread = (_start_telegram_polling(settings, stop, notifier)
                              if settings.telegram_enabled else None)
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
            configure_logging(settings.data_dir, level=settings.log_level)
            outcome = workflow.run()
            print(f"Огляд: {outcome.scan_status}; Telegram: {outcome.notification_status}")
            return 0 if outcome.scan_status == "success" else 1
        configure_logging(settings.data_dir, level=settings.log_level)
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
    from rf_sentinel.health import AcquisitionHealth, HealthOwner
    from rf_sentinel.observability import (AcquisitionObserver, LinuxResourceSampler,
                                           configure_operational_logging)
    from rf_sentinel.rtl_power import RTLPowerScanner
    from rf_sentinel.storage import SQLiteMeasurementSink

    configure_operational_logging(settings.data_dir / "logs", settings.log_max_bytes,
                                  settings.log_backups, settings.log_level)
    profile = SweepProfile(settings.acquisition_low_hz, settings.acquisition_high_hz,
                           settings.acquisition_bin_hz)
    stop = Event()
    previous = {}
    storage = None
    sink = None
    health = None
    resource_sampler = None
    worker = None
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _shutdown_signal)
        storage = SQLiteMeasurementSink(settings.sweeps_path,
                                        incident_retention=settings.incident_retention)
        sink = AsyncMeasurementSink(storage, close_downstream=False)
        health = HealthOwner(
            settings.data_dir / "status" / "health.json",
            AcquisitionHealth("rtl_power", profile.low_hz, profile.high_hz, profile.bin_hz,
                              settings.acquisition_cadence_budget_seconds,
                              settings.acquisition_recovery_seconds),
        )
        observer = AcquisitionObserver(settings.data_dir / "status" / "health.json", profile,
                                       settings.acquisition_cadence_budget_seconds,
                                       settings.acquisition_recovery_seconds, storage=storage,
                                       health_owner=health)
        resource_sampler = LinuxResourceSampler(
            health, settings.resource_sampling_interval_seconds)
        health.save()
        resource_sampler.start()
        worker = SpectrumAcquisitionWorker(
            RTLPowerScanner(settings.rtl_device_index, settings.rtl_gain), sink,
            profile, observer, stop, settings.acquisition_cadence_budget_seconds,
            settings.acquisition_recovery_seconds,
        )
        worker.run()
        return 0
    except OSError:
        logging.getLogger(__name__).error("Помилка запису operational artifacts")
        return 1
    finally:
        if resource_sampler is not None:
            resource_sampler.stop(timeout_seconds=5)
        if sink is not None:
            try:
                deadline = getattr(worker, "shutdown_deadline", None)
                if deadline is None:
                    sink.close(timeout=10)
                else:
                    sink.close(deadline=deadline)
            except BaseException:
                logging.getLogger(__name__).exception(
                    "Acquisition measurement sink close failed")
        final_sink_snapshot = None
        if health is not None and sink is not None:
            try:
                final_sink_snapshot = observer.capture_final_sink_snapshot(sink)
            except BaseException:
                logging.getLogger(__name__).exception(
                    "Final acquisition measurement sink snapshot capture failed")
            if final_sink_snapshot is not None:
                try:
                    observer.publish_final_sink_snapshot(final_sink_snapshot)
                except BaseException:
                    logging.getLogger(__name__).exception(
                        "Final acquisition measurement sink health snapshot publication failed")
        if storage is not None:
            try:
                storage.close()
            except BaseException:
                logging.getLogger(__name__).exception(
                    "Acquisition SQLite close failed")
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        logging.shutdown()
