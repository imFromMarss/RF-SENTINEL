"""Canonical operational health state and atomic snapshot writer."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from threading import RLock


@dataclass
class AcquisitionHealth:
    backend: str
    configured_start_hz: int
    configured_stop_hz: int
    configured_bin_width_hz: int
    cadence_budget_seconds: float
    recovery_delay_seconds: float
    application_status: str = "starting"
    started_at: str | None = None
    last_sweep_started_at: str | None = None
    last_sweep_completed_at: str | None = None
    last_successful_sweep_at: str | None = None
    last_sweep_duration_seconds: float | None = None
    last_sweep_cadence_seconds: float | None = None
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
    last_survey_started_at: str | None = None
    last_successful_survey_at: str | None = None
    last_telegram_success_at: str | None = None
    consecutive_survey_failures: int = 0
    consecutive_telegram_failures: int = 0
    total_completed_surveys: int = 0
    total_failed_surveys: int = 0
    current_artifact_dir: str | None = None
    last_persisted_sweep_at: str | None = None
    persisted_sweeps: int = 0
    failed_persists: int = 0
    sqlite_db_size_bytes: int | None = None
    storage_error: str | None = None
    report_status: str = "unknown"
    total_completed_reports: int = 0
    total_failed_reports: int = 0
    consecutive_report_failures: int = 0
    last_successful_report_at: str | None = None
    last_report_error_reason: str | None = None

    @classmethod
    def started(cls) -> "AcquisitionHealth":
        return cls("survey", 0, 0, 0, 0, 0, started_at=datetime.now(UTC).isoformat())

    @property
    def current_state(self) -> str:
        return self.application_status

    @current_state.setter
    def current_state(self, value: str) -> None:
        self.application_status = value

    def record(self, outcome) -> None:
        now = datetime.now(UTC).isoformat()
        self.last_survey_started_at = outcome.started_at
        self.current_artifact_dir = (
            outcome.artifact_dir.name if outcome.artifact_dir is not None else None
        )
        if outcome.scan_status == "success":
            self.total_completed_surveys += 1
            self.consecutive_survey_failures = 0
            self.last_successful_survey_at = now
            self.application_status = "running"
        else:
            self.total_failed_surveys += 1
            self.consecutive_survey_failures += 1
            self.application_status = "recovering"
        self._record_notification(outcome.notification_status)

    def record_notification(self, status: str) -> None:
        self._record_notification(status)

    def _record_notification(self, status: str) -> None:
        if status == "sent":
            self.consecutive_telegram_failures = 0
            self.last_telegram_success_at = datetime.now(UTC).isoformat()
        elif status == "failed":
            self.consecutive_telegram_failures += 1


class HealthOwner:
    """Own the one in-process health state and its one snapshot writer."""

    def __init__(self, path: Path, state: AcquisitionHealth):
        self.path = Path(path)
        self.state = state
        self.lock = RLock()

    def save(self) -> None:
        with self.lock:
            write_health_snapshot(self.path, self.state)


def write_health_snapshot(path: Path, state: AcquisitionHealth) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(state), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_health_snapshot(path: Path) -> AcquisitionHealth:
    """Load the canonical health model, tolerating snapshots from older builds."""
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError
        fields = set(AcquisitionHealth.__dataclass_fields__)
        return AcquisitionHealth(**{key: value for key, value in values.items() if key in fields})
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return AcquisitionHealth.started()
