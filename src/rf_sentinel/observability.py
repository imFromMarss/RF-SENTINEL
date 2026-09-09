"""Обмежений operational log і атомарний snapshot для подальшої діагностики."""

import json
import logging
from logging.handlers import RotatingFileHandler
from uuid import uuid4

from rf_sentinel.errors import MeasurementSinkError
from rf_sentinel.health import AcquisitionHealth, HealthOwner, write_health_snapshot


class OperationalFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"timestamp": self.formatTime(record), "level": record.levelname,
                           "logger": record.name, "message": record.getMessage(),
                           **getattr(record, "context", {})}, ensure_ascii=False)


def configure_operational_logging(directory, max_bytes=5_000_000, backups=3):
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "rf-sentinel.log", maxBytes=max_bytes,
                                  backupCount=backups, encoding="utf-8")
    handler.setFormatter(OperationalFormatter())
    console = logging.StreamHandler()
    console.setFormatter(OperationalFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler, console], force=True)


class AcquisitionObserver:
    def __init__(self, path, profile, cadence_budget_seconds, recovery_seconds, storage=None,
                 health_owner: HealthOwner | None = None):
        self.path = path
        self.health_owner = health_owner
        self.state = (health_owner.state if health_owner is not None else
                      AcquisitionHealth("rtl_power", profile.low_hz, profile.high_hz,
                                        profile.bin_hz, cadence_budget_seconds, recovery_seconds))
        self.logger = logging.getLogger("rf_sentinel.acquisition")
        self.storage = storage
        self._correlation_id = None

    def _sync_storage(self):
        if self.storage is None:
            return
        status = self.storage.storage_status()
        for name, value in status.items():
            setattr(self.state, name, value)

    def _incident(self, *, component, classification, message, result):
        if self.storage is None:
            return
        try:
            self.storage.record_incident(
                component=component, classification=classification, safe_message=message,
                correlation_id=self._correlation_id, recovery_result=result,
            )
        except MeasurementSinkError:
            pass

    def event(self, event, message, **context):
        level = logging.ERROR if event == "sweep_failed" else logging.INFO
        self.logger.log(level, message, extra={"context": {"event": event, **context}})

    def save(self):
        if self.health_owner is not None:
            with self.health_owner.lock:
                self._sync_storage()
                self.health_owner.save()
        else:
            self._sync_storage()
            write_health_snapshot(self.path, self.state)

    def start(self, now):
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self.state.started_at = now.isoformat()
            self.save()
        self.event("startup", "RF Sentinel запущено")
        self.event("configuration", "Конфігурацію перевірено",
                   start_hz=self.state.configured_start_hz,
                   stop_hz=self.state.configured_stop_hz,
                   bin_width_hz=self.state.configured_bin_width_hz,
                   cadence_budget_seconds=self.state.cadence_budget_seconds,
                   recovery_seconds=self.state.recovery_delay_seconds)
        self.event("backend_initialized", "SDR backend підготовлено; пристрій відкриється під час проходу",
                   backend=self.state.backend)

    def sweep_started(self, now, cadence_seconds=None):
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self._correlation_id = str(uuid4())
            self.state.application_status = "acquiring"
            self.state.last_sweep_started_at = now.isoformat()
            self.state.last_sweep_cadence_seconds = cadence_seconds
            self.save()
        self.event("sweep_started", "Розпочато прохід спектра")

    def failure(self, duration, recovery, error):
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self._failure_locked(duration, recovery, error)

    def _failure_locked(self, duration, recovery, error):
        safe_reasons = {"timeout", "output_too_large", "stderr_too_large", "subprocess_exit",
                        "tuner_pll", "executable_missing", "io_error", "incomplete_coverage",
                        "parser_malformed", "frame_count", "bin_width", "device_busy"}
        self.state.last_error_reason = error.reason if getattr(error, "reason", None) in safe_reasons else "unknown"
        self.state.last_subprocess_returncode = error.returncode if type(error.returncode) is int else None
        self.state.application_status = "recovering"
        self.state.total_sweeps += 1
        self.state.failed_sweeps += 1
        self.state.consecutive_sweep_failures += 1
        self.state.last_sweep_duration_seconds = duration
        self.state.last_error_summary = "Помилка прийому SDR"
        component = "persistence" if isinstance(error, MeasurementSinkError) else "acquisition"
        classification = "persistence_error" if component == "persistence" else self.state.last_error_reason
        message = "Не вдалося зберегти sweep" if component == "persistence" else "Помилка прийому SDR"
        self._incident(component=component, classification=classification,
                       message=message, result="recovery_scheduled")
        self.save()
        self.event("sweep_failed", "Помилка прийому SDR", duration_s=duration,
                   consecutive_failures=self.state.consecutive_sweep_failures,
                   reason=self.state.last_error_reason, returncode=self.state.last_subprocess_returncode)
        self.event("recovery_scheduled", "Заплановано відновлення прийому", delay_s=recovery)

    def completed(self, sweep):
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self._completed_locked(sweep)

    def _completed_locked(self, sweep):
        recovered = self.state.consecutive_sweep_failures > 0
        self.state.application_status = "running"
        self.state.total_sweeps += 1
        self.state.consecutive_sweep_failures = 0
        self.state.last_sweep_completed_at = sweep.finished_at.isoformat()
        self.state.last_successful_sweep_at = sweep.finished_at.isoformat()
        self.state.last_sweep_duration_seconds = sweep.duration_seconds
        self.state.actual_bin_width_hz = sweep.bin_width_hz
        self.state.actual_start_hz = sweep.start_hz
        self.state.actual_stop_hz = sweep.stop_hz
        self.state.bin_count = len(sweep.powers)
        self.state.last_error_summary = None
        self.state.last_error_reason = None
        self.state.last_subprocess_returncode = None
        if recovered:
            self._incident(component="acquisition", classification="recovery",
                           message="Прийом спектра відновлено", result="recovered")
        self.save()
        self.event("sweep_completed", "Прохід спектра завершено",
                   duration_s=sweep.duration_seconds, bins=len(sweep.powers),
                   start_hz=sweep.start_hz, stop_hz=sweep.stop_hz,
                   bin_width_hz=sweep.bin_width_hz, backend=sweep.backend,
                   receiver=sweep.receiver, tuner=sweep.tuner)
        if recovered:
            self.event("recovery_success", "Прийом спектра відновлено")

    def shutdown(self, failed=False):
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self.state.application_status = "failed" if failed else "stopped"
            if failed:
                self.state.last_error_summary = "Помилка application або приймання даних sink"
            self.save()
        self.event("shutdown", "Моніторинг спектра завершено", status=self.state.application_status)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
