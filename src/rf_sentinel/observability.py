"""Обмежений operational log і атомарний snapshot для подальшої діагностики."""

import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, is_dataclass
from threading import Event, Thread
from uuid import uuid4
from contextlib import contextmanager
from datetime import UTC, datetime
import platform
import socket
import sys
import tempfile
from collections import OrderedDict

from rf_sentinel.errors import MeasurementSinkError, SAFE_SCAN_ERROR_CODES
from rf_sentinel.health import AcquisitionHealth, HealthOwner, write_health_snapshot
from rf_sentinel.config import is_sensitive_config_field

DISK_SECTOR_BYTES = 512


def _metric() -> dict:
    return {"count": 0, "success": 0, "failure": 0, "retries": 0,
            "total_duration_seconds": 0.0, "max_duration_seconds": 0.0}


class _SpanOutcome:
    __slots__ = ("value",)

    def __init__(self):
        self.value = None


class _SpanToken:
    """Identity and start boundary retained only while a span is active."""

    __slots__ = ("active_key", "start_ts", "active")

    def __init__(self, active_key, start_ts):
        self.active_key = active_key
        self.start_ts = start_ts
        self.active = True


class ObservabilityCoordinator:
    """Bounded, in-process timing and overlap state shared by component boundaries."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = __import__("threading").RLock()
        self._active = {"sweeps": 0, "reports": 0, "telegram_handlers": 0,
                        "telegram_deliveries": 0}
        self._metrics = {
            "report": _metric(), "telegram_polling": _metric(),
            "telegram_handler": _metric(), "telegram_delivery": _metric(),
        }
        self._metrics["telegram_handler"]["partial"] = 0
        self._metrics["telegram_delivery"]["partial"] = 0
        self._report_phases = {name: _metric() for name in
                               ("query_load", "data_build", "render", "package")}
        self._overlap_domains = {
            name: {
                "active_sweeps": 0,
                "active_components": 0,
                "boundary": {
                    "timestamp": None,
                    "sweep_present": False,
                    "component_present": False,
                    "episode_counted": False,
                },
                "duration_seconds": 0.0,
                "count": 0,
                "sweep_tokens": set(),
                "component_tokens": set(),
            }
            for name in ("sweep_report", "sweep_telegram")
        }
        self._failures = OrderedDict()
        self._failure_events = []
        self._max_failure_events = 32

    @staticmethod
    def _domains_for_key(active_key):
        if active_key == "sweeps":
            return ("sweep_report", "sweep_telegram")
        if active_key == "reports":
            return ("sweep_report",)
        if active_key in ("telegram_handlers", "telegram_deliveries"):
            return ("sweep_telegram",)
        return ()

    @staticmethod
    def _record_point_episode(state):
        boundary = state["boundary"]
        if (boundary["sweep_present"] and boundary["component_present"]
                and not boundary["episode_counted"]):
            state["count"] += 1
            boundary["episode_counted"] = True

    @classmethod
    def _advance_domain(cls, state, timestamp):
        """Finalize the open prefix and create one batch for ``timestamp``."""
        boundary = state["boundary"]
        previous = boundary["timestamp"]
        if previous is None:
            boundary["timestamp"] = timestamp
            boundary["sweep_present"] = state["active_sweeps"] > 0
            boundary["component_present"] = state["active_components"] > 0
            boundary["episode_counted"] = False
            cls._record_point_episode(state)
            return
        if timestamp < previous:
            raise ValueError("observability clock moved backwards")
        if timestamp == previous:
            return

        interval_overlap = (state["active_sweeps"] > 0
                            and state["active_components"] > 0)
        if interval_overlap:
            state["duration_seconds"] += timestamp - previous
        boundary["timestamp"] = timestamp
        boundary["sweep_present"] = state["active_sweeps"] > 0
        boundary["component_present"] = state["active_components"] > 0
        # Positive coverage immediately to the left connects the new point to
        # the already-counted episode.  Otherwise this batch may begin one.
        boundary["episode_counted"] = interval_overlap
        cls._record_point_episode(state)

    def _start_active_locked(self, active_key):
        timestamp = self._clock()
        domains = self._domains_for_key(active_key)
        for name in domains:
            self._advance_domain(self._overlap_domains[name], timestamp)

        token = _SpanToken(active_key, timestamp)
        kind = "sweep" if active_key == "sweeps" else "component"
        for name in domains:
            state = self._overlap_domains[name]
            state[f"{kind}_tokens"].add(token)
        for name in domains:
            state = self._overlap_domains[name]
            state[f"active_{kind}s"] += 1
        self._active[active_key] += 1
        for name in domains:
            state = self._overlap_domains[name]
            state["boundary"][f"{kind}_present"] = True
            self._record_point_episode(state)
        return token

    def _end_active_locked(self, token):
        timestamp = self._clock()
        if token is None or not token.active:
            raise RuntimeError("overlap span token is not active")
        active_key = token.active_key
        domains = self._domains_for_key(active_key)
        for name in domains:
            self._advance_domain(self._overlap_domains[name], timestamp)

        kind = "sweep" if active_key == "sweeps" else "component"
        token_key = f"{kind}_tokens"
        active_count_key = f"active_{kind}s"
        for name in domains:
            state = self._overlap_domains[name]
            if token not in state[token_key] or state[active_count_key] <= 0:
                raise RuntimeError("overlap span token is not registered")
        if self._active[active_key] <= 0:
            raise RuntimeError("active span counter underflow")
        for name in domains:
            state = self._overlap_domains[name]
            state[active_count_key] -= 1
        self._active[active_key] -= 1
        for name in domains:
            state = self._overlap_domains[name]
            state[token_key].remove(token)
            # Presence at the current boundary remains true for this endpoint.
            state["boundary"][f"{kind}_present"] = True
            self._record_point_episode(state)
        token.active = False
        return timestamp

    @contextmanager
    def span(self, component: str, *, phases=None):
        outcome = _SpanOutcome()
        active_key = {"report": "reports", "telegram_handler": "telegram_handlers",
                      "telegram_delivery": "telegram_deliveries"}.get(component)
        with self._lock:
            if active_key:
                token = self._start_active_locked(active_key)
                start_ts = token.start_ts
            else:
                token = None
                start_ts = self._clock()
        try:
            yield outcome
        except BaseException as error:
            self.finish(component, start_ts, False, phases, error=error, token=token)
            raise
        else:
            self.finish(component, start_ts, True, phases, outcome=outcome.value, token=token)

    @contextmanager
    def sweep(self):
        with self._lock:
            token = self._start_active_locked("sweeps")
        try:
            yield
        finally:
            with self._lock:
                self._end_active_locked(token)

    @contextmanager
    def phase(self, name):
        started = self._clock()
        success = False
        try:
            yield
            success = True
        finally:
            duration = max(0.0, self._clock() - started)
            with self._lock:
                metric = self._report_phases.get(name)
                if metric is not None:
                    metric["count"] += 1
                    metric["success" if success else "failure"] += 1
                    metric["total_duration_seconds"] += duration
                    metric["max_duration_seconds"] = max(metric["max_duration_seconds"], duration)

    def finish(self, component, started, success, phases=None, *, outcome=None, error=None,
               token=None):
        with self._lock:
            end_ts = (self._end_active_locked(token) if token is not None
                      else self._clock())
            duration = max(0.0, end_ts - started)
            metric = self._metrics[component]
            metric["count"] += 1
            if outcome in ("sent", "success") or (outcome is None and success):
                metric["success"] += 1
            elif outcome == "partial":
                metric.setdefault("partial", 0)
                metric["partial"] += 1
            else:
                metric["failure"] += 1
            metric["total_duration_seconds"] += duration
            metric["max_duration_seconds"] = max(metric["max_duration_seconds"], duration)
            if phases:
                for name, value in phases.items():
                    phase = self._report_phases.get(name)
                    if phase is not None and value is not None:
                        phase["count"] += 1
                        phase["success" if success else "failure"] += 1
                        phase["total_duration_seconds"] += max(0.0, float(value))
                        phase["max_duration_seconds"] = max(phase["max_duration_seconds"], float(value))
            if not success:
                self._failures[component] = self._failures.get(component, 0) + 1
                while len(self._failures) > 16:
                    self._failures.popitem(last=False)
                self._failure_events.append({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "component": component,
                    "category": type(error).__name__ if error is not None else "failure",
                    "code": getattr(error, "code", None) if error is not None else None,
                    "message": str(error) if error is not None else "component failed",
                    "correlation_id": getattr(error, "correlation_id", None)
                    if error is not None else None,
                })
                del self._failure_events[:-self._max_failure_events]

    def record_retry(self, component):
        with self._lock:
            metric = self._metrics.get(component)
            if metric is not None:
                metric["retries"] += 1

    def record_polling(self, duration, success):
        with self._lock:
            metric = self._metrics["telegram_polling"]
            metric["count"] += 1
            metric["success" if success else "failure"] += 1
            metric["total_duration_seconds"] += max(0.0, duration)
            metric["max_duration_seconds"] = max(metric["max_duration_seconds"], duration)

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            for state in self._overlap_domains.values():
                self._advance_domain(state, now)
            overlap = {
                name: {
                    "count": state["count"],
                    "duration_seconds": state["duration_seconds"],
                }
                for name, state in self._overlap_domains.items()
            }
            return {"report": dict(self._metrics["report"], phases={k: dict(v) for k, v in self._report_phases.items()}),
                    "telegram": {k: dict(v) for k, v in self._metrics.items() if k.startswith("telegram_")},
                    "active": dict(self._active), "overlap": overlap,
                    "failures": dict(self._failures),
                    "failure_events": list(self._failure_events)}


class RunSummaryWriter:
    """Write one compact, secret-free experiment summary using replace semantics."""

    SCHEMA = "rf-sentinel.capacity-run-summary.v1"

    def __init__(self, path, *, clock=None, run_id=None, max_incidents=32):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.run_id = run_id or str(uuid4())
        self.max_incidents = max(1, int(max_incidents))
        self._started = self.clock().isoformat()

    @staticmethod
    def _redact(value, key=""):
        if isinstance(value, dict):
            return {str(k): RunSummaryWriter._redact(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [RunSummaryWriter._redact(item, key) for item in list(value)[:32]]
        if any(token in key.lower() for token in ("token", "secret", "password", "credential")):
            return "[REDACTED]"
        if isinstance(value, Path):
            return str(value)
        return value

    @staticmethod
    def _config_items(settings):
        if isinstance(settings, dict):
            return settings.items()
        return vars(settings).items()

    @classmethod
    def _redact_configuration(cls, settings):
        """Export only JSON-safe config while dropping sensitive fields."""
        items = list(cls._config_items(settings))
        sensitive_values = []

        def collect(value, key=""):
            if is_sensitive_config_field(key):
                if isinstance(value, (str, int, float)) and value not in ("", 0):
                    sensitive_values.append(str(value))
                elif isinstance(value, (list, tuple, set, frozenset)):
                    for item in value:
                        collect(item, key)
                elif isinstance(value, dict):
                    for nested_key, nested_value in value.items():
                        collect(nested_value, str(nested_key))
                return
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    collect(nested_value, str(nested_key))
            elif isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    collect(item, key)

        for key, value in items:
            collect(value, str(key))
        sensitive_values = tuple(sorted(set(sensitive_values), key=len, reverse=True))

        def sanitize(value, key=""):
            if is_sensitive_config_field(key):
                return None
            if isinstance(value, dict):
                return {str(nested_key): sanitized
                        for nested_key, nested_value in value.items()
                        if not is_sensitive_config_field(nested_key)
                        for sanitized in (sanitize(nested_value, str(nested_key)),)}
            if isinstance(value, (list, tuple, set, frozenset)):
                return [sanitize(item, key) for item in list(value)[:32]]
            if isinstance(value, Path):
                value = str(value)
            if isinstance(value, str):
                for secret in sensitive_values:
                    if secret and secret in value:
                        value = value.replace(secret, "[REDACTED]")
            return value

        return {str(key): sanitize(value, str(key)) for key, value in items
                if not is_sensitive_config_field(key)}

    @classmethod
    def _configuration_secrets(cls, settings):
        if settings is None:
            return ()
        values = []

        def collect(value, key=""):
            if is_sensitive_config_field(key):
                if isinstance(value, (str, int, float)) and value not in ("", 0):
                    values.append(str(value))
                return
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    collect(nested_value, str(nested_key))
            elif isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    collect(item, key)

        for key, value in cls._config_items(settings):
            collect(value, str(key))
        return tuple(sorted(set(values), key=len, reverse=True))

    @classmethod
    def _redact_failures(cls, value, settings):
        secrets = cls._configuration_secrets(settings)

        def sanitize(item):
            if is_dataclass(item):
                item = asdict(item)
            if isinstance(item, dict) and "safe_message" in item:
                # IncidentRecord is the existing durable source.  Keep its
                # native fields and expose the common failure vocabulary too.
                item = dict(item)
                item.setdefault("category", item.get("classification"))
                item.setdefault("code", None)
                item.setdefault("message", item.get("safe_message"))
            if isinstance(item, dict):
                return {str(key): sanitize(nested) for key, nested in item.items()}
            if isinstance(item, (list, tuple, set, frozenset)):
                return [sanitize(nested) for nested in list(item)[:32]]
            if isinstance(item, Path):
                return str(item)
            if isinstance(item, str):
                for secret in secrets:
                    if secret and secret in item:
                        item = item.replace(secret, "[REDACTED]")
            return item

        return cls._redact(sanitize(value))

    @staticmethod
    def _sink_shutdown_failure(snapshot):
        """Return one bounded, safe failure record for a non-clean sink snapshot."""
        if not isinstance(snapshot, dict):
            return None
        queue = snapshot.get("queue") if isinstance(snapshot.get("queue"), dict) else {}
        persistence = (snapshot.get("persistence")
                       if isinstance(snapshot.get("persistence"), dict) else {})
        reasons = []
        shutdown_status = snapshot.get("shutdown_status")
        if shutdown_status != "complete":
            reasons.append(f"shutdown_status={shutdown_status}")
        if snapshot.get("writer_alive") is True:
            reasons.append("writer_alive=true")
        if snapshot.get("drain_complete") is False:
            reasons.append("drain_complete=false")
        outstanding = queue.get("outstanding")
        if isinstance(outstanding, (int, float)) and outstanding > 0:
            reasons.append(f"outstanding={outstanding}")
        persistence_failed = (
            persistence.get("status") == "failed"
            or (isinstance(persistence.get("failed_persists"), (int, float))
                and persistence["failed_persists"] > 0)
            or persistence.get("storage_error") is not None
        )
        if persistence_failed:
            reasons.append("persistence_failed")
        if not reasons:
            return None
        return {
            "component": "storage",
            "category": "sink_shutdown",
            "code": "incomplete" if not persistence_failed else "persistence_failed",
            "message": "Measurement sink final state is non-clean: " + ", ".join(reasons),
        }

    def build(self, *, settings=None, state=None, coordinator=None, status="running",
              incidents=(), incident_source="available", component_failures=(),
              git_identity=None, sink_snapshot=None):
        health = asdict(state) if state is not None else {}
        resources = health.get("resource_telemetry")
        effective = {}
        if settings is not None:
            effective = self._redact_configuration(settings)
        metrics = coordinator.snapshot() if coordinator is not None else {}
        coordinator_failures = metrics.get("failure_events", [])
        component_failures = list(component_failures)
        sink_failure = self._sink_shutdown_failure(sink_snapshot)
        if sink_failure is not None:
            component_failures.append(sink_failure)
        component_failures = component_failures[-self.max_incidents:]
        incident_records = list(incidents)[-self.max_incidents:]
        sink_non_clean = sink_failure is not None
        failure_status = ("failed" if status == "failed" or component_failures or
                          coordinator_failures or sink_non_clean else "clean")
        final_status = ("failed" if failure_status == "failed" and
                        (status in ("running", "stopped", "completed") or
                         sink_non_clean and sink_failure["code"] == "persistence_failed")
                        else ("partial" if sink_non_clean else status))
        sink_queue = sink_snapshot.get("queue") if isinstance(sink_snapshot, dict) else None
        sink_persistence = sink_snapshot.get("persistence") if isinstance(sink_snapshot, dict) else None
        summary = {
            "schema": self.SCHEMA, "version": 1, "run_id": self.run_id,
            "application": {"started_at": self._started, "ended_at": self.clock().isoformat(),
                             "status": final_status},
            "build": {"git_identity": git_identity},
            "runtime": {"hostname": socket.gethostname(), "platform": platform.platform(),
                        "python": sys.version.split()[0]},
            "configuration": self._redact(effective),
            "acquisition": {key: health.get(key) for key in
                            ("cadence_budget_seconds", "total_sweeps", "failed_sweeps",
                             "cadence_telemetry", "queue_telemetry", "persistence_telemetry")},
            "report": metrics.get("report", {}), "telegram": metrics.get("telegram", {}),
            "overlap": metrics.get("overlap", {}), "resources": {"latest": resources},
            "failures": {
                "status": failure_status,
                "components": self._redact_failures(component_failures, settings),
                "coordinator": self._redact_failures(coordinator_failures[-self.max_incidents:], settings),
                "incidents": self._redact_failures(incident_records, settings),
                "incident_source": incident_source,
            },
        }
        if sink_snapshot is not None:
            # Reuse the exact snapshot published to health; do not sample the
            # sink again while constructing the final run summary.
            summary["acquisition"]["queue_telemetry"] = sink_queue
            summary["acquisition"]["persistence_telemetry"] = sink_persistence
            summary["acquisition"]["shutdown_status"] = sink_snapshot.get("shutdown_status")
        return self._redact(summary)

    def build_start_marker(self):
        """Build the small current-run marker written before station components start."""
        return {
            "schema": self.SCHEMA,
            "version": 1,
            "run_id": self.run_id,
            "application": {
                "started_at": self._started,
                "ended_at": None,
                "status": "running",
            },
        }

    def write_start_marker(self):
        """Atomically publish that this writer's run is the current live run."""
        return self._write_payload(self.build_start_marker())

    def write(self, **kwargs):
        payload = self.build(**kwargs)
        return self._write_payload(payload)

    def _write_payload(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".run-summary-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(self.path)
            return payload
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


class OperationalFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"timestamp": self.formatTime(record), "level": record.levelname,
                           "logger": record.name, "message": record.getMessage(),
                           **getattr(record, "context", {})}, ensure_ascii=False)


def configure_operational_logging(directory, max_bytes=5_000_000, backups=3, level="INFO"):
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "rf-sentinel.log", maxBytes=max_bytes,
                                  backupCount=backups, encoding="utf-8")
    handler.setFormatter(OperationalFormatter())
    console = logging.StreamHandler()
    console.setFormatter(OperationalFormatter())
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler, console], force=True)


@dataclass(frozen=True)
class ResourceTelemetry:
    sampled_at: str
    process_cpu_percent: float | None = None
    process_rss_bytes: int | None = None
    system_cpu_percent: float | None = None
    system_memory_total_bytes: int | None = None
    system_memory_available_bytes: int | None = None
    disk_read_bytes: int | None = None
    disk_write_bytes: int | None = None
    disk_read_delta_bytes: int | None = None
    disk_write_delta_bytes: int | None = None
    cpu_temperature_celsius: float | None = None
    throttling_current_flags: dict | None = None
    throttling_sticky_flags: dict | None = None


class LinuxResourceSampler:
    """Best-effort /proc and /sys sampler that never owns acquisition control flow."""

    def __init__(self, health_owner: HealthOwner, interval_seconds: float = 15,
                 *, proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys"),
                 sample_fn=None, clock=time.monotonic, logger=None,
                 throttling_command_runner=None, throttling_timeout_seconds: float = 1.0):
        self.health_owner = health_owner
        self.interval_seconds = interval_seconds
        self.proc_root = Path(proc_root)
        self.sys_root = Path(sys_root)
        self._sample_fn = sample_fn or self.sample
        self._clock = clock
        self.logger = logger or logging.getLogger("rf_sentinel.resources")
        self._throttling_command_runner = throttling_command_runner or subprocess.run
        self._throttling_timeout_seconds = throttling_timeout_seconds
        self._stop = Event()
        self._thread = None
        # Keep process and system baselines independent.  Process CPU uses only
        # its own counter and monotonic time; /proc/stat is not part of it.
        self._previous_process = None
        self._previous_system = None
        self._previous_disk = None
        self._resource_aggregates = {}
        self._uses_default_sample = sample_fn is None

    @property
    def resource_aggregates(self) -> dict:
        """Return a bounded copy of this station run's resource aggregates."""
        return {name: dict(values) for name, values in self._resource_aggregates.items()}

    def _record_aggregate(self, snapshot: ResourceTelemetry) -> None:
        average_metrics = {
            "process_cpu_percent": "max",
            "process_rss_bytes": "max",
            "system_cpu_percent": "max",
            "system_memory_available_bytes": "min",
            "cpu_temperature_celsius": "max",
        }
        for field, extremum in average_metrics.items():
            value = getattr(snapshot, field)
            if value is None:
                continue
            aggregate = self._resource_aggregates.setdefault(
                field, {"count": 0, "average": 0.0, extremum: value})
            aggregate["count"] += 1
            aggregate["average"] += (value - aggregate["average"]) / aggregate["count"]
            aggregate[extremum] = (max(aggregate[extremum], value)
                                    if extremum == "max" else min(aggregate[extremum], value))

        for field in ("disk_read_delta_bytes", "disk_write_delta_bytes"):
            value = getattr(snapshot, field)
            if value is None:
                continue
            aggregate = self._resource_aggregates.setdefault(
                field, {"total": 0, "max": value})
            aggregate["total"] += value
            aggregate["max"] = max(aggregate["max"], value)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = Thread(target=self._run, name="resource-telemetry", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout_seconds: float = 5) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0, timeout_seconds))
        return thread is None or not thread.is_alive()

    def _run(self):
        while not self._stop.is_set():
            try:
                snapshot = self._sample_fn()
                if not self._uses_default_sample:
                    self._record_aggregate(snapshot)
                with self.health_owner.lock:
                    self.health_owner.state.resource_telemetry = asdict(snapshot)
                    self.health_owner.state.resource_aggregates = self.resource_aggregates
                    self.health_owner.save()
            except BaseException:
                self.logger.exception("Resource telemetry sample failed; acquisition continues")
            self._stop.wait(self.interval_seconds)

    @staticmethod
    def _read(path):
        return Path(path).read_text(encoding="utf-8")

    def _proc_cpu(self):
        fields = self._read(self.proc_root / "self/stat").rsplit(") ", 1)[1].split()
        return int(fields[11]) + int(fields[12])

    def _system_cpu(self):
        fields = self._read(self.proc_root / "stat").splitlines()[0].split()[1:]
        return sum(int(value) for value in fields[:8]), int(fields[3]) + int(fields[4])

    def _memory(self):
        values = {}
        for line in self._read(self.proc_root / "meminfo").splitlines():
            key, _, value = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(value.strip().split()[0]) * 1024
        return values.get("MemTotal"), values.get("MemAvailable")

    def _diskstats(self):
        """Return diskstats counters for physical whole devices only.

        /proc/diskstats reports whole devices, partitions, and virtual block
        devices in one flat list.  Sysfs is the authority for relationships:
        partitions have a ``partition`` marker, and virtual devices live
        below ``/virtual/block`` and may expose physical devices in ``slaves``.
        """
        records = {}
        for line in self._read(self.proc_root / "diskstats").splitlines():
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) < 14:
                raise ValueError("malformed /proc/diskstats line")
            major, minor = int(fields[0]), int(fields[1])
            read_sectors, write_sectors = int(fields[5]), int(fields[9])
            if read_sectors < 0 or write_sectors < 0:
                raise ValueError("negative /proc/diskstats counter")
            records[fields[2]] = (major, minor, read_sectors, write_sectors)

        sys_dev = self.sys_root / "dev/block"

        def sysfs_path(name):
            record = records[name]
            link = sys_dev / f"{record[0]}:{record[1]}"
            try:
                return Path(os.path.realpath(link))
            except OSError:
                return None

        def is_virtual(name, path):
            # The name fallback keeps synthetic/minimal proc fixtures useful;
            # on Linux the sysfs path is the authoritative classification.
            return (path is not None and "/devices/virtual/block/" in str(path)) or \
                name.startswith(("loop", "ram", "zram", "dm-"))

        def partition_parent(name, path):
            marker = path / "partition" if path is not None else None
            if marker is not None and marker.exists():
                parent = path.parent.name
                return parent if parent in records else None
            # A sysfs block path for a partition is .../block/<whole>/<part>.
            if path is not None and path.parent.name in records and path.parent.name != name:
                return path.parent.name
            return None

        def slaves(name, path):
            if path is None:
                return ()
            directory = path / "slaves"
            try:
                return tuple(item.name for item in directory.iterdir()
                             if item.name in records)
            except OSError:
                return ()

        physical = set()
        virtual_slaves = set()
        for name in records:
            path = sysfs_path(name)
            if is_virtual(name, path):
                virtual_slaves.update(slaves(name, path))
                continue
            parent = partition_parent(name, path)
            physical.add(parent or name)

        # Prefer a whole physical device whenever it is present.  If only
        # partitions are available, retain those partitions rather than
        # silently losing their I/O; never include both forms.
        selected = set()
        for name in physical:
            if name in records:
                selected.add(name)
            else:
                selected.update(partition for partition in records
                                if partition_parent(partition, sysfs_path(partition)) == name)
        for name in virtual_slaves:
            if name in records and not is_virtual(name, sysfs_path(name)):
                parent = partition_parent(name, sysfs_path(name))
                selected.add(parent or name)

        selected = {name for name in selected if name in records}
        return sum(records[name][2] for name in selected) * DISK_SECTOR_BYTES, \
            sum(records[name][3] for name in selected) * DISK_SECTOR_BYTES, frozenset(selected)

    def _disk(self):
        return self._diskstats()

    def _temperature(self):
        thermal_root = self.sys_root / "class/thermal"
        candidates = []
        for zone in sorted(thermal_root.glob("thermal_zone*")):
            type_path = zone / "type"
            temp_path = zone / "temp"
            try:
                zone_type = self._read(type_path).strip().lower()
                raw_temperature = int(self._read(temp_path).strip())
                temperature = raw_temperature / 1000
            except (OSError, UnicodeError, ValueError):
                continue
            if not zone_type or not -100 <= temperature <= 200:
                continue
            # The field is CPU temperature, so an arbitrary thermal zone is
            # not an honest substitute.  These are the Linux names used by
            # CPU/package/SoC thermal drivers, including Raspberry Pi.
            cpu_source = ("cpu" in zone_type or "x86_pkg" in zone_type
                           or zone_type.startswith("soc-"))
            if cpu_source:
                candidates.append((zone_type, temperature))
        if len(candidates) != 1:
            return None
        return candidates[0][1]

    def _throttling(self):
        compatible_paths = (
            self.proc_root / "device-tree/compatible",
            self.sys_root / "firmware/devicetree/base/compatible",
        )
        try:
            compatible = next(self._read(path) for path in compatible_paths
                              if path.exists()).lower()
        except (OSError, UnicodeError, StopIteration):
            return None, None
        if "raspberrypi" not in compatible:
            return None, None

        command = shutil.which("vcgencmd")
        if command is None and self._throttling_command_runner is subprocess.run:
            return None, None
        try:
            result = self._throttling_command_runner(
                [command or "vcgencmd", "get_throttled"],
                capture_output=True, text=True, timeout=self._throttling_timeout_seconds,
                check=False)
            if getattr(result, "returncode", 0) != 0:
                return None, None
            output = result.stdout if hasattr(result, "stdout") else result
            match = re.fullmatch(r"\s*throttled=0x([0-9a-fA-F]+)\s*", output or "")
            if match is None:
                return None, None
            value = int(match.group(1), 16)
        except (OSError, UnicodeError, TypeError, ValueError, subprocess.SubprocessError):
            return None, None

        names = ("under_voltage", "arm_frequency_capped", "throttled",
                 "soft_temperature_limit")
        current = {name: bool(value & (1 << bit)) for bit, name in enumerate(names)}
        sticky = {name: bool(value & (1 << (16 + bit))) for bit, name in enumerate(names)}
        return current, sticky

    def sample(self):
        now = self._clock()
        try:
            process_ticks = self._proc_cpu()
        except (OSError, IndexError, ValueError):
            process_ticks = None
        try:
            total_ticks, idle_ticks = self._system_cpu()
        except (OSError, IndexError, ValueError):
            total_ticks = idle_ticks = None
        process_percent = system_percent = None
        previous_process = self._previous_process
        self._previous_process = ((now, process_ticks) if process_ticks is not None else None)
        if previous_process is not None and process_ticks is not None:
            elapsed = now - previous_process[0]
            process_delta = process_ticks - previous_process[1]
            try:
                ticks_per_second = os.sysconf("SC_CLK_TCK")
            except (OSError, ValueError):
                ticks_per_second = None
            # A value of 100% means one fully busy CPU core; multi-threaded
            # process usage may therefore exceed 100%.  No host-wide CPU
            # normalization is applied.
            if elapsed > 0 and process_delta > 0 and ticks_per_second and ticks_per_second > 0:
                process_percent = round(process_delta / ticks_per_second / elapsed * 100, 2)

        previous_system = self._previous_system
        self._previous_system = ((now, total_ticks, idle_ticks)
                                 if total_ticks is not None and idle_ticks is not None else None)
        if previous_system is not None and total_ticks is not None and idle_ticks is not None:
            total_delta = total_ticks - previous_system[1]
            idle_delta = idle_ticks - previous_system[2]
            if total_delta > 0:
                system_percent = round((total_delta - idle_delta) / total_delta * 100, 2)
        try:
            memory_total, memory_available = self._memory()
        except (OSError, IndexError, ValueError):
            memory_total = memory_available = None
        try:
            disk_read, disk_write, disk_devices = self._disk()
        except (OSError, IndexError, ValueError):
            disk_read = disk_write = disk_devices = None
        disk_read_delta = disk_write_delta = None
        previous_disk = self._previous_disk
        if disk_devices is None:
            self._previous_disk = None
        else:
            self._previous_disk = (disk_devices, disk_read, disk_write)
            if previous_disk is not None and previous_disk[0] == disk_devices:
                read_delta = disk_read - previous_disk[1]
                write_delta = disk_write - previous_disk[2]
                if read_delta >= 0 and write_delta >= 0:
                    disk_read_delta, disk_write_delta = read_delta, write_delta
        try:
            temperature = self._temperature()
        except (OSError, IndexError, ValueError):
            temperature = None
        try:
            throttling_current, throttling_sticky = self._throttling()
        except (OSError, UnicodeError, TypeError, ValueError):
            throttling_current = throttling_sticky = None
        try:
            rss = self._rss()
        except (OSError, IndexError, ValueError):
            rss = None
        snapshot = ResourceTelemetry(
            sampled_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            process_cpu_percent=process_percent,
            process_rss_bytes=rss,
            system_cpu_percent=system_percent,
            system_memory_total_bytes=memory_total,
            system_memory_available_bytes=memory_available,
            disk_read_bytes=disk_read,
            disk_write_bytes=disk_write,
            disk_read_delta_bytes=disk_read_delta,
            disk_write_delta_bytes=disk_write_delta,
            cpu_temperature_celsius=temperature,
            throttling_current_flags=throttling_current,
            throttling_sticky_flags=throttling_sticky,
        )
        self._record_aggregate(snapshot)
        return snapshot

    def _rss(self):
        fields = self._read(self.proc_root / "self/statm").split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")


class AcquisitionObserver:
    def __init__(self, path, profile, cadence_budget_seconds, recovery_seconds, storage=None,
                 health_owner: HealthOwner | None = None, coordinator=None):
        self.path = path
        self.health_owner = health_owner
        self.state = (health_owner.state if health_owner is not None else
                      AcquisitionHealth("rtl_power", profile.low_hz, profile.high_hz,
                                        profile.bin_hz, cadence_budget_seconds, recovery_seconds))
        self.logger = logging.getLogger("rf_sentinel.acquisition")
        self.storage = storage
        self._correlation_id = None
        self.coordinator = coordinator

    @property
    def current_correlation_id(self):
        """Correlation shared by the terminal sweep row and incident."""
        return self._correlation_id

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

    def capture_final_sink_snapshot(self, sink):
        """Capture one coherent, detached post-close sink snapshot."""
        snapshot_reader = getattr(sink, "telemetry_snapshot", None)
        if snapshot_reader is None:
            return None
        # Detach the captured value from the sink before either artifact is
        # published. Both consumers therefore see the same stable state.
        return copy.deepcopy(snapshot_reader())

    def publish_final_sink_snapshot(self, snapshot):
        """Publish an already captured sink snapshot as final health state."""
        if snapshot is None:
            return None
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self.state.queue_telemetry = snapshot["queue"]
            self.state.persistence_telemetry = snapshot["persistence"]
            # The sink snapshot describes storage finalization, while health is
            # the canonical application outcome.  A different managed
            # component may already have made the overall shutdown non-clean.
            self.state.shutdown_status = (
                "incomplete" if self.state.application_status == "failed"
                else snapshot["shutdown_status"]
            )
            self._sync_storage()
            self.save()
        return snapshot

    def persist_final_sink_snapshot(self, sink):
        """Capture and publish the sink snapshot for standalone callers."""
        return self.publish_final_sink_snapshot(self.capture_final_sink_snapshot(sink))

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
        if self.coordinator is not None:
            self._sweep_span = self.coordinator.sweep()
            self._sweep_span.__enter__()
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self._correlation_id = str(uuid4())
            self.state.application_status = "acquiring"
            self.state.last_sweep_started_at = now.isoformat()
            self.state.last_sweep_cadence_seconds = cadence_seconds
            if cadence_seconds is not None:
                jitter = cadence_seconds - self.state.cadence_budget_seconds
                missed = max(0, int(cadence_seconds // self.state.cadence_budget_seconds) - 1)
                telemetry = self.state.cadence_telemetry or {
                    "observed_count": 0, "overrun_count": 0, "deferred_count": 0,
                    "missed_slots": 0, "last_cadence_seconds": None,
                    "last_jitter_seconds": None, "max_cadence_seconds": 0.0,
                    "max_abs_jitter_seconds": 0.0,
                }
                telemetry.setdefault("observed_count", 0)
                telemetry.setdefault("overrun_count", 0)
                telemetry.setdefault("deferred_count", 0)
                telemetry.setdefault("missed_slots", 0)
                telemetry.setdefault("max_cadence_seconds", 0.0)
                telemetry.setdefault("max_abs_jitter_seconds", 0.0)
                telemetry["observed_count"] += 1
                telemetry["overrun_count"] += int(cadence_seconds > self.state.cadence_budget_seconds)
                telemetry["deferred_count"] += int(cadence_seconds > self.state.cadence_budget_seconds)
                telemetry["missed_slots"] += missed
                telemetry["last_cadence_seconds"] = cadence_seconds
                telemetry["last_jitter_seconds"] = jitter
                telemetry["max_cadence_seconds"] = max(telemetry["max_cadence_seconds"], cadence_seconds)
                telemetry["max_abs_jitter_seconds"] = max(telemetry["max_abs_jitter_seconds"], abs(jitter))
                self.state.cadence_telemetry = telemetry
            self.save()
        self.event("sweep_started", "Розпочато прохід спектра")

    def failure(self, duration, recovery, error):
        self._close_sweep_span()
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self._failure_locked(duration, recovery, error)

    def _failure_locked(self, duration, recovery, error):
        self.state.last_error_reason = (
            error.reason if getattr(error, "reason", None) in SAFE_SCAN_ERROR_CODES else "unknown")
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

    def completed(self, sweep, *, timing=None, queue=None, persistence=None):
        self._close_sweep_span()
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self._completed_locked(sweep, timing=timing, queue=queue, persistence=persistence)
            if timing:
                duration = timing.get("source_duration_seconds")
                telemetry = self.state.cadence_telemetry or {}
                telemetry["last_source_duration_seconds"] = duration
                telemetry["max_source_duration_seconds"] = max(
                    telemetry.get("max_source_duration_seconds", 0.0), duration)
                telemetry["total_source_duration_seconds"] = (
                    telemetry.get("total_source_duration_seconds", 0.0) + duration)
                self.state.cadence_telemetry = telemetry
            if queue is not None:
                self.state.queue_telemetry = queue
            if persistence is not None:
                self.state.persistence_telemetry = persistence
            self.save()

    def _completed_locked(self, sweep, *, timing=None, queue=None, persistence=None):
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
                   duration_s=sweep.duration_seconds,
                   source_duration_s=(timing or {}).get("source_duration_seconds"),
                   cadence_s=self.state.last_sweep_cadence_seconds,
                   cadence_jitter_s=((self.state.cadence_telemetry or {}).get("last_jitter_seconds")),
                   queue=(queue or self.state.queue_telemetry),
                   persistence=(persistence or self.state.persistence_telemetry),
                   bins=len(sweep.powers),
                   start_hz=sweep.start_hz, stop_hz=sweep.stop_hz,
                   bin_width_hz=sweep.bin_width_hz, backend=sweep.backend,
                   receiver=sweep.receiver, tuner=sweep.tuner)
        if recovered:
            self.event("recovery_success", "Прийом спектра відновлено")

    def shutdown(self, failed=False):
        self._close_sweep_span()
        lock = self.health_owner.lock if self.health_owner is not None else _NullLock()
        with lock:
            self.state.application_status = "failed" if failed else "stopped"
            if failed:
                self.state.last_error_summary = "Помилка application або приймання даних sink"
            self.save()
        self.event("shutdown", "Моніторинг спектра завершено", status=self.state.application_status)

    def _close_sweep_span(self):
        span = getattr(self, "_sweep_span", None)
        if span is not None:
            self._sweep_span = None
            span.__exit__(None, None, None)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
