"""Незалежний producer спектра з послідовним прийомом і обмеженим recovery."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from queue import Empty, Full, Queue
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
    item.  It becomes ``persisted`` in the receipt after the downstream sink
    returns.  Queued items are deliberately non-durable and are rejected on
    close after a writer failure or process restart.
    """

    def __init__(self, downstream: MeasurementSink, *, max_queue: int = 32,
                 enqueue_timeout: float = 0.1, thread_name: str = "measurement-writer",
                 close_downstream: bool = True):
        if type(max_queue) is not int or max_queue < 1:
            raise ConfigurationError("Розмір черги MeasurementSink має бути додатним")
        if not math.isfinite(enqueue_timeout) or enqueue_timeout < 0:
            raise ConfigurationError("Timeout черги MeasurementSink некоректний")
        self._downstream = downstream
        self._close_downstream = close_downstream
        self._queue: Queue[tuple[SpectrumSweep, MeasurementReceipt]] = Queue(maxsize=max_queue)
        self._enqueue_timeout = enqueue_timeout
        self._condition = Condition()
        self._pending = 0
        self._closing = False
        self._closed = False
        self._failure: BaseException | None = None
        self._writer = Thread(target=self._write_loop, name=thread_name, daemon=False)
        self.accepted_count = self.persisted_count = 0
        self.rejected_count = self.failed_count = 0
        self._writer.start()

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def pending_count(self) -> int:
        with self._condition:
            return self._pending

    def store_sweep(self, sweep: SpectrumSweep) -> MeasurementReceipt:
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
            self._pending += 1
            self.accepted_count += 1
        try:
            self._queue.put((sweep, receipt), timeout=self._enqueue_timeout)
        except Full:
            with self._condition:
                self._pending -= 1
                self.rejected_count += 1
                self._condition.notify_all()
            receipt.status = "rejected"
            receipt.error = MeasurementQueueFullError("MeasurementSink queue is full")
        return receipt

    def _write_loop(self) -> None:
        while True:
            try:
                sweep, receipt = self._queue.get(timeout=0.05)
            except Empty:
                with self._condition:
                    if self._closing and self._pending == 0:
                        return
                continue
            try:
                if self._failure is not None:
                    receipt.status = "failed"
                    receipt.error = self._failure
                    self.failed_count += 1
                else:
                    persist = getattr(self._downstream, "store_sweep", self._downstream)
                    persist(sweep)
                    receipt.status = "persisted"
                    self.persisted_count += 1
            except BaseException as error:
                with self._condition:
                    if self._failure is None:
                        self._failure = MeasurementPersistenceError(
                            "MeasurementSink persistence failed")
                receipt.status = "failed"
                receipt.error = self._failure
                self.failed_count += 1
            finally:
                with self._condition:
                    self._pending -= 1
                    should_exit = self._closing and self._pending == 0
                    self._condition.notify_all()
                self._queue.task_done()
                if should_exit:
                    return

    def flush(self, timeout: float | None = None) -> None:
        """Wait for all accepted items; raises if any item was not persisted."""
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("flush timeout must be finite and non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("MeasurementSink flush timed out")
                self._condition.wait(remaining)
            if self._failure is not None:
                raise self._failure

    def close(self, timeout: float | None = None) -> None:
        """Drain and stop the non-daemon writer; no orphan writer is allowed."""
        with self._condition:
            if self._closed:
                return
            self._closing = True
        error = None
        try:
            self.flush(timeout)
        except BaseException as exc:
            error = exc
        self._writer.join(timeout)
        if self._writer.is_alive():
            raise TimeoutError("MeasurementSink writer did not stop")
        with self._condition:
            while True:
                try:
                    _, receipt = self._queue.get_nowait()
                except Empty:
                    break
                receipt.status = "rejected"
                receipt.error = error or self._failure or MeasurementSinkError("Sink closed")
                self.rejected_count += 1
                self._queue.task_done()
            self._closed = True
        downstream_close = (getattr(self._downstream, "close", None)
                            if self._close_downstream else None)
        if downstream_close is not None:
            try:
                downstream_close()
            except BaseException as exc:
                if error is None:
                    error = MeasurementPersistenceError("MeasurementSink close failed")
        if error is not None:
            raise error


T = TypeVar("T")


def run_continuous_loop(run_cycle: Callable[[], T], recovery_seconds: float, stop,
                        on_start: Callable[[], None], on_result: Callable[[T], bool],
                        on_failure: Callable[[BaseException], None],
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
                on_failure(error)
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
                 monotonic=time.monotonic, now=lambda: datetime.now(UTC)):
        if (not math.isfinite(cadence_budget_seconds) or not 0 < cadence_budget_seconds <= 3600
                or not math.isfinite(recovery_seconds) or not 1 <= recovery_seconds <= 3600):
            raise ConfigurationError("Некоректний інтервал проходу або відновлення")
        self.source, self.sink, self.profile = source, sink, profile
        self.observer, self.stop = observer, stop
        self.cadence_budget_seconds, self.recovery_seconds = cadence_budget_seconds, recovery_seconds
        self.monotonic, self.now = monotonic, now

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
            self.observer.completed(sweep)
            return True

        def failure(error):
            self.observer.failure(self.monotonic() - started, self.recovery_seconds, error)

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
            return acquire()

        def shutdown(failed):
            lifecycle_error = None
            try:
                close = getattr(self.sink, "close", None)
                if close is not None:
                    close()
                else:
                    flush = getattr(self.sink, "flush", None)
                    if flush is not None:
                        flush()
            except BaseException as error:
                lifecycle_error = error
            self.observer.shutdown(failed or lifecycle_error is not None)
            if lifecycle_error is not None:
                raise lifecycle_error

        run_continuous_loop(cycle, self.recovery_seconds, self.stop,
                            lambda: self.observer.start(self.now()),
                            complete, failure, shutdown, wait_after_success)
