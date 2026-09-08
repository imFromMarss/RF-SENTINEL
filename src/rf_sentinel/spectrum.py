"""Незалежні від пристрою контракти вимірювань і сканера."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
            raise ConfigurationError("Параметри сканування мають бути цілими числами")
        if not 24_000_000 <= self.low_hz < self.high_hz <= 1_766_000_000:
            raise ConfigurationError("Діапазон сканування поза дозволеними межами тюнера")
        if not 10_000 <= self.bin_hz <= 1_000_000:
            raise ConfigurationError("Ширина комірки має бути від 10 кГц до 1 МГц")
        if not 1 <= self.integration_seconds <= self.duration_seconds <= 1800:
            raise ConfigurationError("Часові межі: 1 ≤ інтервал накопичення ≤ тривалість ≤ 1800 с")
        if self.duration_seconds % self.integration_seconds:
            raise ConfigurationError("Тривалість має бути кратною інтервалу накопичення")
        if self.high_hz - self.low_hz > 100_000_000:
            if self.bin_hz < 250_000 or self.integration_seconds < 60:
                raise ConfigurationError("Широкосмуговий огляд потребує комірки ≥ 250 кГц та інтервалу ≥ 60 с")
        # rtl_power може обрати дрібніші FFT-комірки, ніж запитана верхня межа.
        if self.estimated_values > 1_000_000:
            raise ConfigurationError("Сканування перевищує бюджет вимірювань")

    @property
    def estimated_values(self) -> int:
        import math
        return (math.ceil((self.high_hz - self.low_hz) / self.bin_hz) * 2
                * (self.duration_seconds // self.integration_seconds + 1))

    @classmethod
    def full_range(cls, low_hz: int, high_hz: int) -> "ScanProfile":
        """Межі передає перевірена конфігурація, а не припущення про tuner."""
        return cls(low_hz, high_hz, 500_000, 60, 1800)


@dataclass(frozen=True)
class SpectrumFrame:
    timestamp: datetime
    powers: tuple[float, ...]
    sample_count: int


@dataclass(frozen=True)
class SpectrumData:
    # Межі комірок спільні для всіх проходів; частота комірки — її центр.
    edges_hz: tuple[float, ...]
    frames: tuple[SpectrumFrame, ...]


@dataclass(frozen=True)
class ScanResult:
    backend: str
    profile: ScanProfile
    started_at: datetime
    duration_seconds: float
    spectrum: SpectrumData
    receiver: str = "невідомо"
    tuner: str = "невідомо"
    gain_db: float | None = None


class SpectrumScanner(Protocol):
    def scan(self, profile: ScanProfile, raw_path: Path | None = None) -> ScanResult: ...
