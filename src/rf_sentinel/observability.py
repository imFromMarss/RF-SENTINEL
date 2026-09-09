"""Обмежений operational log і атомарний snapshot для подальшої діагностики."""

from dataclasses import dataclass
import json
import logging
from logging.handlers import RotatingFileHandler

from rf_sentinel.scheduler import write_status


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


@dataclass
class AcquisitionHealth:
    backend: str
    configured_start_hz: int
    configured_stop_hz: int
    configured_bin_width_hz: int
    target_cadence_seconds: float
    recovery_delay_seconds: float
    application_status: str = "starting"
    started_at: str | None = None
    last_sweep_started_at: str | None = None
    last_sweep_completed_at: str | None = None
    last_successful_sweep_at: str | None = None
    last_sweep_duration_seconds: float | None = None
    actual_bin_width_hz: float | None = None
    actual_start_hz: float | None = None
    actual_stop_hz: float | None = None
    bin_count: int = 0
    total_sweeps: int = 0
    failed_sweeps: int = 0
    consecutive_sweep_failures: int = 0
    last_error_summary: str | None = None
    last_error_reason: str | None = None
    last_subprocess_returncode: int | None = None


class AcquisitionObserver:
    def __init__(self, path, profile, target_seconds, recovery_seconds):
        self.path = path
        self.state = AcquisitionHealth("rtl_power", profile.low_hz, profile.high_hz,
                                       profile.bin_hz, target_seconds, recovery_seconds)
        self.logger = logging.getLogger("rf_sentinel.acquisition")

    def event(self, event, message, **context):
        level = logging.ERROR if event == "sweep_failed" else logging.INFO
        self.logger.log(level, message, extra={"context": {"event": event, **context}})

    def save(self):
        write_status(self.path, self.state)

    def start(self, now):
        self.state.started_at = now.isoformat()
        self.save()
        self.event("startup", "RF Sentinel запущено")
        self.event("configuration", "Конфігурацію перевірено",
                   start_hz=self.state.configured_start_hz,
                   stop_hz=self.state.configured_stop_hz,
                   bin_width_hz=self.state.configured_bin_width_hz,
                   target_seconds=self.state.target_cadence_seconds,
                   recovery_seconds=self.state.recovery_delay_seconds)
        self.event("backend_initialized", "SDR backend підготовлено; пристрій відкриється під час проходу",
                   backend=self.state.backend)

    def sweep_started(self, now):
        self.state.application_status = "acquiring"
        self.state.last_sweep_started_at = now.isoformat()
        self.save()
        self.event("sweep_started", "Розпочато прохід спектра")

    def failure(self, duration, recovery, error):
        safe_reasons = {"timeout", "output_too_large", "stderr_too_large", "subprocess_exit",
                        "tuner_pll", "executable_missing", "io_error", "incomplete_coverage",
                        "parser_malformed", "frame_count", "bin_width", "device_busy"}
        self.state.last_error_reason = error.reason if error.reason in safe_reasons else "unknown"
        self.state.last_subprocess_returncode = error.returncode if type(error.returncode) is int else None
        self.state.application_status = "recovering"
        self.state.total_sweeps += 1
        self.state.failed_sweeps += 1
        self.state.consecutive_sweep_failures += 1
        self.state.last_sweep_duration_seconds = duration
        self.state.last_error_summary = "Помилка прийому SDR"
        self.save()
        self.event("sweep_failed", "Помилка прийому SDR", duration_s=duration,
                   consecutive_failures=self.state.consecutive_sweep_failures,
                   reason=self.state.last_error_reason, returncode=self.state.last_subprocess_returncode)
        self.event("recovery_scheduled", "Заплановано відновлення прийому", delay_s=recovery)

    def completed(self, sweep):
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
        self.save()
        self.event("sweep_completed", "Прохід спектра завершено",
                   duration_s=sweep.duration_seconds, bins=len(sweep.powers),
                   start_hz=sweep.start_hz, stop_hz=sweep.stop_hz,
                   bin_width_hz=sweep.bin_width_hz, backend=sweep.backend,
                   receiver=sweep.receiver, tuner=sweep.tuner)
        if recovered:
            self.event("recovery_success", "Прийом спектра відновлено")

    def shutdown(self, failed=False):
        self.state.application_status = "failed" if failed else "stopped"
        if failed:
            self.state.last_error_summary = "Помилка application або приймання даних sink"
        self.save()
        self.event("shutdown", "Моніторинг спектра завершено", status=self.state.application_status)
