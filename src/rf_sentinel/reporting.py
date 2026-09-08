"""Survey diagnostics and PNG rendering, independent of delivery transport."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from rf_sentinel.spectrum import ScanProfile, ScanResult


@dataclass(frozen=True)
class SurveyReport:
    identity: str
    backend: str
    low_hz: int
    high_hz: int
    started_at: str
    duration_seconds: float
    sweeps: int
    bins_per_sweep: int
    power_values: int
    accumulated_samples: int
    peak_frequency_hz: float | None
    peak_power_db: float | None
    status: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self) -> str:
        peak = "unavailable"
        if self.peak_frequency_hz is not None:
            peak = f"{self.peak_frequency_hz / 1e6:.6f} MHz, {self.peak_power_db:.2f} dB"
        return (
            f"{self.identity}\nSurvey status: {self.status}\nBackend: {self.backend}\n"
            f"Range: {self.low_hz / 1e6:g}–{self.high_hz / 1e6:g} MHz\n"
            f"Started: {self.started_at}\nDuration: {self.duration_seconds:.2f} s\n"
            f"Sweeps: {self.sweeps}; bins/sweep: {self.bins_per_sweep}; "
            f"power values: {self.power_values}\n"
            f"Accumulated samples (sum of CSV rows): {self.accumulated_samples}\n"
            f"Peak bin center: {peak}\nPower: uncalibrated backend dB; diagnostic only."
        )


def make_report(result: ScanResult, identity: str) -> SurveyReport:
    data = result.spectrum
    peak, index = max(
        (power, index) for frame in data.frames for index, power in enumerate(frame.powers)
    )
    return SurveyReport(
        identity, result.backend, result.profile.low_hz, result.profile.high_hz,
        result.started_at.isoformat(), result.duration_seconds, len(data.frames),
        len(data.edges_hz) - 1, sum(len(frame.powers) for frame in data.frames),
        sum(frame.sample_count for frame in data.frames),
        (data.edges_hz[index] + data.edges_hz[index + 1]) / 2, peak, "success",
    )


def failed_report(profile: ScanProfile, identity: str, started: datetime,
                  duration: float) -> SurveyReport:
    return SurveyReport(identity, "unavailable", profile.low_hz, profile.high_hz,
                        started.isoformat(), duration, 0, 0, 0, 0, None, None, "scan_failed")


def render_waterfall(result: ScanResult, destination: Path) -> None:
    # Lazy imports preserve hardware-free, lightweight default application startup.
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    data = result.spectrum
    timestamps = [(frame.timestamp - result.started_at).total_seconds()
                  for frame in data.frames]
    # CSV timestamps mark integration ends; retain irregular timing on the Y axis.
    time_edges = [timestamps[0] - result.profile.integration_seconds, *timestamps]
    figure = Figure(figsize=(9, 4), layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    mesh = axes.pcolormesh(
        [edge / 1e6 for edge in data.edges_hz], time_edges,
        [frame.powers for frame in data.frames], shading="flat", cmap="viridis",
    )
    axes.set_xlabel("Frequency (MHz)")
    axes.set_ylabel("Time since scan start (s)")
    axes.set_title(f"RF Sentinel survey · {result.started_at.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC")
    figure.colorbar(mesh, ax=axes, label="Power (uncalibrated dB)")
    figure.savefig(destination, format="png", dpi=120)
    figure.clear()
