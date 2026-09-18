"""Незалежний producer спектра з послідовним прийомом і обмеженим recovery."""

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
import inspect
import math
from threading import Condition, Thread
import time
from typing import Callable, Literal, Protocol, TypeVar
from uuid import uuid4

from rf_sentinel.errors import (ConfigurationError, MeasurementPersistenceError,
                                MeasurementQueueFullError, MeasurementSinkError, ScanError)


@dataclass(frozen=True)
class SweepProfile:
    low_hz: int = 24_000_000
    high_hz: int = 1_766_000_000
    bin_hz: int = 500_000
    integration_seconds: int = 1
    duration_seconds: int = 1

    def __post_init__(self):
        if (any(type(v) is not int for v in (self.low_hz, self.high_hz, self.bin_hz))
                or not 24_000_000 <= self.low_hz < self.high_hz <= 1_766_000_000
                or not 10_000 <= self.bin_hz <= 2_800_000
                or self.integration_seconds != 1 or self.duration_seconds != 1):
            raise ConfigurationError("Некоректний профіль одноразового проходу тюнера")


SweepStatus = Literal["success", "partial", "failed"]
CoverageStatus = Literal["complete", "partial", "none"]
QualityStatus = Literal["valid", "degraded", "unavailable"]


@dataclass(frozen=True)
class SweepProfileMetadata:
    """Serializable profile shape shared by requested and applied settings."""

    low_hz: float
    high_hz: float
    bin_hz: float
    integration_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class DeviceIdentity:
    """Physical receiver identity; unknown values remain explicit, not omitted."""

    device_id: str = "unknown"
    model: str = "unknown"
    tuner: str = "unknown"


@dataclass(frozen=True)
class SweepCoverage:
    status: CoverageStatus
    expected_bins: int
    observed_bins: int
    fraction: float


@dataclass(frozen=True)
class SweepQuality:
    status: QualityStatus
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ErrorClassification:
    """Stable error category for persistence; raw exception text is not canonical."""

    category: str
    code: str


@dataclass(frozen=True)
class SpectrumSweep:
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    start_hz: float
    stop_hz: float
    frequencies_hz: tuple[float, ...]
    bin_width_hz: float
    powers: tuple[float, ...]
    backend: str
    receiver: str = "невідомо"
    tuner: str = "невідомо"
    status: SweepStatus = "success"
    schema_version: str = "spectrum-sweep.v1"
    sweep_id: str = field(default_factory=lambda: str(uuid4()))
    sequence: int = 1
    correlation_id: str | None = None
    requested_profile: SweepProfileMetadata | None = None
    actual_profile: SweepProfileMetadata | None = None
    device: DeviceIdentity = field(default_factory=DeviceIdentity)
    tool_version: str = "unknown"
    coverage: SweepCoverage | None = None
    quality: SweepQuality | None = None
    error_classification: ErrorClassification | None = None

    def __post_init__(self):
        if self.coverage is None:
            observed = len(self.powers)
            status: CoverageStatus = "complete" if self.status == "success" else (
                "none" if self.status == "failed" else "partial")
            expected = observed if status == "complete" else max(observed, 1)
            object.__setattr__(self, "coverage", SweepCoverage(
                status, expected, observed, observed / expected if expected else 0.0))
        if self.quality is None:
            quality: QualityStatus = ("valid" if self.status == "success" else
                                      "unavailable" if self.status == "failed" else "degraded")
            object.__setattr__(self, "quality", SweepQuality(quality))
        if self.actual_profile is None and 0 < self.start_hz < self.stop_hz:
            object.__setattr__(self, "actual_profile", SweepProfileMetadata(
                self.start_hz, self.stop_hz, self.bin_width_hz,
                self.duration_seconds, self.duration_seconds))
        if self.requested_profile is None and self.actual_profile is not None:
            object.__setattr__(self, "requested_profile", self.actual_profile)
        values = (self.duration_seconds, self.start_hz, self.stop_hz,
                  self.bin_width_hz, *self.frequencies_hz, *self.powers)
        if (self.schema_version != "spectrum-sweep.v1" or not self.sweep_id
                or type(self.sequence) is not int or self.sequence < 1
                or not self.tool_version or not all(math.isfinite(v) for v in values)
                or self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None
                or self.finished_at < self.started_at or self.duration_seconds < 0
                or not self.backend or self.status not in ("success", "partial", "failed")
                or (self.status == "success" and (
                    not 0 < self.start_hz < self.stop_hz or self.bin_width_hz <= 0
                    or not self.powers or len(self.powers) != len(self.frequencies_hz)
                    or any(not self.start_hz < f < self.stop_hz for f in self.frequencies_hz)
                    or any(b <= a for a, b in zip(self.frequencies_hz, self.frequencies_hz[1:]))
                    or abs(self.stop_hz - self.start_hz - len(self.powers) * self.bin_width_hz) > 2
                    or any(abs(f - (self.start_hz + (i + 0.5) * self.bin_width_hz)) > 2
                           for i, f in enumerate(self.frequencies_hz))))
                or self.status == "failed" and (self.powers or self.frequencies_hz)
                or self.coverage is None or self.quality is None
                or self.requested_profile is None
                or self.coverage.expected_bins < 0 or self.coverage.observed_bins < 0
                or not 0 <= self.coverage.fraction <= 1
                or (self.status == "success" and self.coverage.status != "complete")
                or (self.status == "failed" and self.coverage.status != "none")
                or (self.status == "success" and self.quality.status != "valid")
                or (self.status == "failed" and self.quality.status != "unavailable")
                or (self.status == "failed" and self.error_classification is None)):
            raise ValueError("Некоректний завершений прохід спектра")

    @property
    def outcome(self) -> SweepStatus:
        """Canonical name for the terminal status; ``status`` remains API-compatible."""
        return self.status


MeasurementStatus = Literal["accepted", "persisted", "rejected", "failed"]


@dataclass
class MeasurementReceipt:
    """Outcome of one hand-off; ``accepted`` is not a durability claim."""

    sweep_id: str
    status: MeasurementStatus
    error: BaseException | None = None


class MeasurementSink(Protocol):
    """Boundary for completed frames; a result makes hand-off explicit."""

    def store_sweep(self, sweep: SpectrumSweep) -> MeasurementReceipt | None: ...


class SweepSource(Protocol):
    def acquire(self, profile: SweepProfile) -> SpectrumSweep: ...


class LatestSweepSink:
    """Обмежене RAM-сховище для перевірки boundary; історія не зберігається."""

    def __init__(self):
        self.latest: SpectrumSweep | None = None

    def store_sweep(self, sweep: SpectrumSweep) -> MeasurementReceipt:
        self.latest = sweep
        return MeasurementReceipt(sweep.sweep_id, "persisted")


class AsyncMeasurementSink:
    """Bounded in-process hand-off to a serialized persistence consumer.

    A successful ``store_sweep`` means only that this process owns the queued
    item.  The writer publishes the downstream terminal outcome to each receipt
    as part of completing the operation, so counters, outstanding work, and the
    receipt cannot describe different completion states.  Queued items are
    deliberately non-durable and are rejected on close after a writer failure
    or process restart.
    """

    def __init__(self, downstream: MeasurementSink, *, max_queue: int = 32,
                 enqueue_timeout: float = 0.1, thread_name: str = "measurement-writer",
                 close_downstream: bool = True, monotonic=time.monotonic,
                 deadline_monotonic=time.monotonic):
        if type(max_queue) is not int or max_queue < 1:
            raise ConfigurationError("Розмір черги MeasurementSink має бути додатним")
        if not math.isfinite(enqueue_timeout) or enqueue_timeout < 0:
            raise ConfigurationError("Timeout черги MeasurementSink некоректний")
        self._downstream = downstream
        self._close_downstream = close_downstream
        self._queue: deque[tuple[SpectrumSweep, MeasurementReceipt]] = deque()
        self._max_queue = max_queue
        self._enqueue_timeout = enqueue_timeout
        self._monotonic = monotonic
        self._deadline_monotonic = deadline_monotonic
        self._condition = Condition()
        self._queued = 0
        self._in_flight = 0
        self._pending = 0
        self._active_enqueues = 0
        self._closing = False
        self._closed = False
        self._failure: BaseException | None = None
        self._result_failure: BaseException | None = None
        self._close_failure: BaseException | None = None
        self._downstream_close_thread: Thread | None = None
        self._downstream_close_status = ("not_started" if close_downstream
                                         else "not_owned")
        self._downstream_close_failure: BaseException | None = None
        self._writer = Thread(target=self._write_loop, name=thread_name, daemon=False)
        self.accepted_count = self.persisted_count = 0
        self.rejected_count = self.failed_count = 0
        self.enqueue_count = 0
        self.last_enqueue_duration_seconds = None
        self.max_enqueue_duration_seconds = 0.0
        self.total_enqueue_duration_seconds = 0.0
        self.backpressure_wait_count = 0
        self.last_backpressure_wait_seconds = None
        self.max_backpressure_wait_seconds = 0.0
        self.total_backpressure_wait_seconds = 0.0
        self.high_water_mark = 0
        self._writer.start()

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def pending_count(self) -> int:
        with self._condition:
            return self._pending

    @property
    def queue_telemetry(self) -> dict:
        with self._condition:
            return self._queue_telemetry_locked()

    def _queue_telemetry_locked(self) -> dict:
        return {
                "current_depth": self._queued,
                "queued_depth": self._queued,
                "in_flight": self._in_flight,
                "outstanding": self._pending,
                "pending_depth": self._pending,
                "high_water_mark": self.high_water_mark,
                "accepted_count": self.accepted_count,
                "persisted_count": self.persisted_count,
                "enqueue_count": self.enqueue_count,
                "rejected_count": self.rejected_count,
                "failed_count": self.failed_count,
                "last_enqueue_duration_seconds": self.last_enqueue_duration_seconds,
                "max_enqueue_duration_seconds": self.max_enqueue_duration_seconds,
                "total_enqueue_duration_seconds": self.total_enqueue_duration_seconds,
                "backpressure_wait_count": self.backpressure_wait_count,
                "last_backpressure_wait_seconds": self.last_backpressure_wait_seconds,
                "max_backpressure_wait_seconds": self.max_backpressure_wait_seconds,
                "total_backpressure_wait_seconds": self.total_backpressure_wait_seconds,
            }

    def telemetry_snapshot(self) -> dict:
        """Return one synchronized queue/downstream persistence snapshot."""
        with self._condition:
            queue = self._queue_telemetry_locked()
            persistence = getattr(self._downstream, "persistence_telemetry", None)
            writer_alive = self._writer.is_alive()
            downstream_closed = self._downstream_close_status in ("complete", "not_owned")
            shutdown_complete = (self._closed and self._failure is None
                                 and self._close_failure is None and not writer_alive
                                 and downstream_closed)
            return {
                "queue": queue,
                "persistence": persistence if isinstance(persistence, dict) else {},
                "shutdown_complete": shutdown_complete,
                "shutdown_status": "complete" if shutdown_complete else "incomplete",
                "writer_alive": writer_alive,
                "drain_complete": queue["outstanding"] == 0,
                "downstream_close_status": self._downstream_close_status,
                "downstream_close_active": (
                    self._downstream_close_thread is not None
                    and self._downstream_close_thread.is_alive()),
                "downstream_close_error": (
                    None if self._downstream_close_failure is None
                    else type(self._downstream_close_failure).__name__),
            }

    def store_sweep(self, sweep: SpectrumSweep) -> MeasurementReceipt:
        enqueue_started = self._monotonic()
        receipt = MeasurementReceipt(sweep.sweep_id, "accepted")
        with self._condition:
            if self._closing or self._closed:
                receipt.status = "rejected"
                receipt.error = MeasurementSinkError("MeasurementSink is closed")
                self.rejected_count += 1
                return receipt
            if self._failure is not None:
                receipt.status = "failed"
                receipt.error = self._failure
                self.failed_count += 1
                return receipt
            self._active_enqueues += 1
            backpressure_started = None
            backpressure_duration = None
            if self._queued >= self._max_queue and self._enqueue_timeout > 0:
                backpressure_started = self._monotonic()
                deadline = backpressure_started + self._enqueue_timeout
                while self._queued >= self._max_queue:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                backpressure_duration = max(0.0, self._monotonic() - backpressure_started)

            enqueue_finished = self._monotonic()
            enqueue_duration = max(0.0, enqueue_finished - enqueue_started)
            self.enqueue_count += 1
            self.last_enqueue_duration_seconds = enqueue_duration
            self.max_enqueue_duration_seconds = max(
                self.max_enqueue_duration_seconds, enqueue_duration)
            self.total_enqueue_duration_seconds += enqueue_duration
            if backpressure_duration is not None:
                self.backpressure_wait_count += 1
                self.last_backpressure_wait_seconds = backpressure_duration
                self.max_backpressure_wait_seconds = max(
                    self.max_backpressure_wait_seconds, backpressure_duration)
                self.total_backpressure_wait_seconds += backpressure_duration

            if self._queued >= self._max_queue:
                self._active_enqueues -= 1
                self.rejected_count += 1
                receipt.status = "rejected"
                receipt.error = MeasurementQueueFullError("MeasurementSink queue is full")
                self._condition.notify_all()
                return receipt

            self._queue.append((sweep, receipt))
            self._queued += 1
            self._pending += 1
            self._active_enqueues -= 1
            self.accepted_count += 1
            self.high_water_mark = max(self.high_water_mark, self._queued)
            self._condition.notify_all()
        return receipt

    def _write_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    if ((self._closing or self._failure is not None)
                            and self._pending == 0 and self._active_enqueues == 0):
                        return
                    self._condition.wait()
                sweep, receipt = self._queue.popleft()
                self._queued -= 1
                self._in_flight += 1
                self._condition.notify_all()
            status: MeasurementStatus
            error: BaseException | None
            persistence_exception = False
            try:
                if self._failure is not None:
                    status, error = "failed", self._failure
                else:
                    persist = getattr(self._downstream, "store_sweep", self._downstream)
                    result = persist(sweep)
                    status, error = self._map_downstream_result(result)
            except BaseException:
                persistence_exception = True
                status, error = "failed", None
            with self._condition:
                if persistence_exception:
                    if self._failure is None:
                        self._failure = MeasurementPersistenceError(
                            "MeasurementSink persistence failed")
                    error = self._failure
                receipt.status = status
                receipt.error = error
                if status == "persisted":
                    self.persisted_count += 1
                elif status == "rejected":
                    # ``rejected_count`` is reserved for admission rejection.
                    # A downstream rejection is an admitted persist failure,
                    # while its receipt retains the more precise status.
                    self.failed_count += 1
                    if self._result_failure is None:
                        self._result_failure = MeasurementPersistenceError(
                            "MeasurementSink downstream rejected persistence")
                else:
                    self.failed_count += 1
                    if self._failure is None and self._result_failure is None:
                        self._result_failure = MeasurementPersistenceError(
                            "MeasurementSink downstream persistence failed")
                self._in_flight -= 1
                self._pending -= 1
                should_exit = ((self._closing or self._failure is not None)
                               and self._pending == 0 and self._active_enqueues == 0)
                self._condition.notify_all()
            if should_exit:
                return

    @staticmethod
    def _map_downstream_result(
            result: MeasurementReceipt | None,
    ) -> tuple[MeasurementStatus, BaseException | None]:
        if result is None:
            return "persisted", None
        if not isinstance(result, MeasurementReceipt):
            # Plain callback sinks historically return arbitrary helper values
            # (for example ``Event.wait`` returns bool).  Only the public
            # MeasurementReceipt contract carries a downstream outcome.
            return "persisted", None
        if result.status == "persisted":
            return "persisted", None
        if result.status == "rejected":
            return ("rejected", result.error or MeasurementPersistenceError(
                "MeasurementSink downstream rejected persistence"))
        if result.status == "failed":
            return ("failed", result.error or MeasurementPersistenceError(
                "MeasurementSink downstream persistence failed"))
        return ("failed", result.error or MeasurementPersistenceError(
            "MeasurementSink downstream did not confirm persistence"))

    def flush(self, timeout: float | None = None) -> None:
        """Wait for all accepted items; raises if any item was not persisted."""
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("flush timeout must be finite and non-negative")
        deadline = None if timeout is None else self._deadline_monotonic() + timeout
        self._flush_until(deadline)

    def _flush_until(self, deadline: float | None) -> None:
        """Flush against an existing absolute deadline without rebasing it."""
        with self._condition:
            while self._pending or self._active_enqueues:
                remaining = None if deadline is None else deadline - self._deadline_monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("MeasurementSink flush timed out")
                self._condition.wait(remaining)
            if self._failure is not None:
                raise self._failure
            if self._result_failure is not None:
                raise self._result_failure

    @staticmethod
    def _downstream_close_arguments(close, deadline: float, remaining: float) -> dict:
        """Use a downstream deadline API when it explicitly exposes one."""
        try:
            parameters = inspect.signature(close).parameters
        except (TypeError, ValueError):
            return {}
        deadline_parameter = parameters.get("deadline")
        if deadline_parameter is not None and deadline_parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY):
            return {"deadline": deadline}
        timeout_parameter = parameters.get("timeout")
        if timeout_parameter is not None and timeout_parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY):
            return {"timeout": remaining}
        return {}

    def _run_downstream_close(self, close, deadline: float) -> None:
        try:
            remaining = max(0.0, deadline - self._deadline_monotonic())
            close(**self._downstream_close_arguments(close, deadline, remaining))
        except BaseException as exc:
            failure = MeasurementPersistenceError("MeasurementSink close failed")
            failure.__cause__ = exc
            with self._condition:
                self._downstream_close_failure = failure
                self._downstream_close_status = "failed"
                self._condition.notify_all()
        else:
            with self._condition:
                self._downstream_close_status = "complete"
                self._condition.notify_all()

    def _start_downstream_close(self, close, deadline: float) -> Thread:
        with self._condition:
            if self._downstream_close_thread is None:
                self._downstream_close_status = "incomplete"
                # This is a bounded-call boundary for an arbitrary downstream
                # close, not the persistence writer.  It is daemonized because
                # Python cannot cancel a stuck call and station process exit
                # must not be held hostage after the shared deadline.
                self._downstream_close_thread = Thread(
                    target=self._run_downstream_close,
                    args=(close, deadline),
                    name=f"{self._writer.name}-downstream-close",
                    daemon=True,
                )
                self._downstream_close_thread.start()
            return self._downstream_close_thread

    def close(self, timeout: float | None = None, *, deadline: float | None = None) -> None:
        """Drain and stop the non-daemon writer within one absolute deadline."""
        if timeout is not None and deadline is not None:
            raise ValueError("Specify either timeout or deadline, not both")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("MeasurementSink close timeout must be finite and non-negative")
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("MeasurementSink close deadline must be finite")
        close_deadline = (self._deadline_monotonic() + timeout
                          if timeout is not None else
                          deadline if deadline is not None else
                          self._deadline_monotonic() + 10.0)
        with self._condition:
            if self._closed:
                return
            self._closing = True
            self._close_failure = None
            self._condition.notify_all()
        error = None
        try:
            self._flush_until(close_deadline)
        except BaseException as exc:
            error = exc
        remaining = max(0.0, close_deadline - self._deadline_monotonic())
        self._writer.join(remaining)
        if self._writer.is_alive():
            error = error or TimeoutError("MeasurementSink writer did not stop")
            with self._condition:
                self._close_failure = error
            raise error
        with self._condition:
            while self._queue:
                _, receipt = self._queue.popleft()
                self._queued -= 1
                self._pending -= 1
                receipt.status = "rejected"
                receipt.error = error or self._failure or MeasurementSinkError("Sink closed")
                self.rejected_count += 1
            if error is not None:
                self._close_failure = error
        downstream_close = (getattr(self._downstream, "close", None)
                            if self._close_downstream else None)
        if downstream_close is not None:
            close_thread = self._start_downstream_close(downstream_close, close_deadline)
            remaining = max(0.0, close_deadline - self._deadline_monotonic())
            close_thread.join(remaining)
            deadline_expired = self._deadline_monotonic() > close_deadline
            with self._condition:
                downstream_status = self._downstream_close_status
                downstream_failure = self._downstream_close_failure
            if close_thread.is_alive() or deadline_expired:
                timeout_error = TimeoutError("MeasurementSink downstream close timed out")
                error = error or timeout_error
                with self._condition:
                    self._close_failure = error
                raise error
            if downstream_status == "failed":
                error = error or downstream_failure
        elif self._close_downstream:
            with self._condition:
                self._downstream_close_status = "complete"
        with self._condition:
            self._closed = True
            self._close_failure = error
        if error is not None:
            raise error


T = TypeVar("T")


def run_continuous_loop(run_cycle: Callable[[], T], recovery_seconds: float, stop,
                        on_start: Callable[[], None], on_result: Callable[[T], bool],
                        on_failure: Callable[[BaseException], bool | None],
                        on_shutdown: Callable[[bool], None],
                        success_wait: Callable[[T], float] | None = None,
                        retry_exceptions: tuple[type[BaseException], ...] = (ScanError,)) -> None:
    """The one serialized loop used by acquisition and application workflows."""
    if not math.isfinite(recovery_seconds) or not 1 <= recovery_seconds <= 3600:
        raise ConfigurationError("Некоректна затримка відновлення")
    failed = False
    try:
        on_start()
        while not stop.is_set():
            try:
                result = run_cycle()
            except retry_exceptions as error:
                recover = on_failure(error)
                if recover is False:
                    break
                if stop.wait(recovery_seconds):
                    break
                continue
            successful = on_result(result)
            delay = success_wait(result) if successful and success_wait is not None else (
                0 if successful else recovery_seconds
            )
            if delay and stop.wait(delay):
                break
    except KeyboardInterrupt:
        if hasattr(stop, "set"):
            stop.set()
        raise
    except BaseException:
        failed = True
        raise
    finally:
        on_shutdown(failed)


class SpectrumAcquisitionWorker:
    def __init__(self, source: SweepSource, sink: MeasurementSink, profile: SweepProfile,
                 observer, stop, cadence_budget_seconds=60.0, recovery_seconds=60.0,
                 monotonic=time.monotonic, now=lambda: datetime.now(UTC),
                 shutdown_timeout_seconds=10.0,
                 sink_lifecycle_owner: Literal["worker", "station"] = "worker"):
        if (not math.isfinite(cadence_budget_seconds) or not 0 < cadence_budget_seconds <= 3600
                or not math.isfinite(recovery_seconds) or not 1 <= recovery_seconds <= 3600
                or not math.isfinite(shutdown_timeout_seconds) or shutdown_timeout_seconds < 0
                or sink_lifecycle_owner not in ("worker", "station")):
            raise ConfigurationError("Некоректний інтервал проходу або відновлення")
        self.source, self.sink, self.profile = source, sink, profile
        self.observer, self.stop = observer, stop
        self.cadence_budget_seconds, self.recovery_seconds = cadence_budget_seconds, recovery_seconds
        self.monotonic, self.now = monotonic, now
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.shutdown_deadline = None
        self.sink_lifecycle_owner = sink_lifecycle_owner

    def set_shutdown_deadline(self, deadline: float) -> None:
        if not math.isfinite(deadline):
            raise ValueError("Shutdown deadline must be finite")
        self.shutdown_deadline = (deadline if self.shutdown_deadline is None
                                  else min(self.shutdown_deadline, deadline))

    def run(self):
        previous_started = None
        started = None

        def acquire():
            return self.source.acquire(self.profile)

        def complete(sweep):
            receipt = self.sink.store_sweep(sweep)
            if receipt is not None and receipt.status in ("rejected", "failed"):
                if isinstance(receipt.error, BaseException):
                    raise receipt.error
                raise MeasurementSinkError(f"MeasurementSink {receipt.status}")
            self.observer.completed(sweep, timing=self._timing,
                                    queue=getattr(self.sink, "queue_telemetry", None),
                                    persistence=getattr(getattr(self.sink, "_downstream", None),
                                                        "persistence_telemetry", None))
            return True

        def failure(error):
            # The source reports this exact reason when the coordinated stop
            # interrupts an active acquisition.  A stop flag alone is not
            # sufficient: an unrelated acquisition error must remain a real
            # failure even if shutdown races with its delivery.
            if isinstance(error, ScanError) and error.reason == "stopped" and self.stop.is_set():
                return False
            self.observer.failure(self.monotonic() - started, self.recovery_seconds, error)
            return True

        def wait_after_success(_sweep):
            return max(0, self.cadence_budget_seconds - (self.monotonic() - started))

        def cycle():
            nonlocal started, previous_started
            started = self.monotonic()
            if self.stop.is_set():
                raise ScanError("Завершення прийому", reason="stopped")
            cadence = None if previous_started is None else started - previous_started
            previous_started = started
            self.observer.sweep_started(self.now(), cadence)
            source_started = self.monotonic()
            try:
                sweep = acquire()
            finally:
                self._timing = {"source_duration_seconds": max(
                    0.0, self.monotonic() - source_started)}
            return sweep

        def shutdown(failed):
            lifecycle_error = None
            if self.sink_lifecycle_owner == "worker":
                try:
                    close = getattr(self.sink, "close", None)
                    if close is not None:
                        if isinstance(self.sink, AsyncMeasurementSink):
                            deadline = (self.shutdown_deadline
                                        if self.shutdown_deadline is not None else
                                        self.monotonic() + self.shutdown_timeout_seconds)
                            self.shutdown_deadline = deadline
                            close(deadline=deadline)
                        else:
                            close()
                    else:
                        flush = getattr(self.sink, "flush", None)
                        if flush is not None:
                            flush()
                except BaseException as error:
                    lifecycle_error = error
            self.observer.shutdown(failed or lifecycle_error is not None)
            if self.sink_lifecycle_owner == "worker":
                try:
                    persist_final = getattr(self.observer, "persist_final_sink_snapshot", None)
                    if persist_final is not None:
                        persist_final(self.sink)
                except BaseException as error:
                    if lifecycle_error is None:
                        lifecycle_error = error
            if lifecycle_error is not None:
                raise lifecycle_error

        run_continuous_loop(cycle, self.recovery_seconds, self.stop,
                            lambda: self.observer.start(self.now()),
                            complete, failure, shutdown, wait_after_success)
