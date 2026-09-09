"""Незалежний producer спектра з послідовним прийомом і обмеженим recovery."""

from dataclasses import dataclass
from datetime import UTC, datetime
import math
import time
from typing import Callable, Protocol, TypeVar

from rf_sentinel.errors import ConfigurationError, ScanError


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
    status: str = "success"

    def __post_init__(self):
        values = (self.duration_seconds, self.start_hz, self.stop_hz,
                  self.bin_width_hz, *self.frequencies_hz, *self.powers)
        if (not all(math.isfinite(v) for v in values)
                or self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None
                or self.finished_at < self.started_at or self.duration_seconds < 0
                or not 0 < self.start_hz < self.stop_hz or self.bin_width_hz <= 0
                or not self.powers or len(self.powers) != len(self.frequencies_hz)
                or self.status != "success" or not self.backend
                or any(not self.start_hz < f < self.stop_hz for f in self.frequencies_hz)
                or any(b <= a for a, b in zip(self.frequencies_hz, self.frequencies_hz[1:]))
                or abs(self.stop_hz - self.start_hz - len(self.powers) * self.bin_width_hz) > 2
                or any(abs(f - (self.start_hz + (i + 0.5) * self.bin_width_hz)) > 2
                       for i, f in enumerate(self.frequencies_hz))):
            raise ValueError("Некоректний завершений прохід спектра")


class MeasurementSink(Protocol):
    """Швидке приймання завершеного frame; rendering виконується окремим consumer."""

    def store_sweep(self, sweep: SpectrumSweep) -> None: ...


class SweepSource(Protocol):
    def acquire(self, profile: SweepProfile) -> SpectrumSweep: ...


class LatestSweepSink:
    """Обмежене RAM-сховище для перевірки boundary; історія не зберігається."""

    def __init__(self):
        self.latest: SpectrumSweep | None = None

    def store_sweep(self, sweep: SpectrumSweep) -> None:
        self.latest = sweep


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
            self.sink.store_sweep(sweep)
            self.observer.completed(sweep)
            return True

        def failure(error):
            self.observer.failure(self.monotonic() - started, self.recovery_seconds, error)

        def wait_after_success(_sweep):
            return max(0, self.cadence_budget_seconds - (self.monotonic() - started))

        def cycle():
            nonlocal started, previous_started
            started = self.monotonic()
            cadence = None if previous_started is None else started - previous_started
            previous_started = started
            self.observer.sweep_started(self.now(), cadence)
            return acquire()

        run_continuous_loop(cycle, self.recovery_seconds, self.stop,
                            lambda: self.observer.start(self.now()),
                            complete, failure, self.observer.shutdown, wait_after_success)
