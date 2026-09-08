"""Device-neutral measurement and scanner contracts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from rf_sentinel.errors import ConfigurationError


@dataclass(frozen=True)
class ScanProfile:
    low_hz: int = 88_000_000
    high_hz: int = 108_000_000
    bin_hz: int = 125_000
    integration_seconds: int = 10
    duration_seconds: int = 30

    def __post_init__(self) -> None:
        values = (self.low_hz, self.high_hz, self.bin_hz,
                  self.integration_seconds, self.duration_seconds)
        if any(type(value) is not int for value in values):
            raise ConfigurationError("Scan parameters must be integers")
        if not 24_000_000 <= self.low_hz < self.high_hz <= 1_766_000_000:
            raise ConfigurationError("Scan frequency range is outside tuner bounds")
        if not 10_000 <= self.bin_hz <= 1_000_000:
            raise ConfigurationError("Scan bin width must be 10 kHz to 1 MHz")
        if self.high_hz - self.low_hz > 100_000_000:
            raise ConfigurationError("Survey span must not exceed 100 MHz")
        if not 1 <= self.integration_seconds <= self.duration_seconds <= 300:
            raise ConfigurationError("Scan timing must satisfy 1 <= integration <= duration <= 300")
        if self.duration_seconds % self.integration_seconds:
            raise ConfigurationError("Scan duration must be a multiple of integration time")
        if ((self.high_hz - self.low_hz) / self.bin_hz
                * self.duration_seconds / self.integration_seconds > 100_000):
            raise ConfigurationError("Scan exceeds measurement budget")


@dataclass(frozen=True)
class SpectrumFrame:
    timestamp: datetime
    powers: tuple[float, ...]
    sample_count: int


@dataclass(frozen=True)
class SpectrumData:
    # Bin edges in Hz, shared by all frames; bin frequency means cell center.
    edges_hz: tuple[float, ...]
    frames: tuple[SpectrumFrame, ...]


@dataclass(frozen=True)
class ScanResult:
    backend: str
    profile: ScanProfile
    started_at: datetime
    duration_seconds: float
    spectrum: SpectrumData


class SpectrumScanner(Protocol):
    def scan(self, profile: ScanProfile) -> ScanResult: ...
