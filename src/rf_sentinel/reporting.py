"""Українські звіти та інженерна карта спектра, незалежні від транспорту."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from rf_sentinel.spectrum import ScanProfile, ScanResult


def duration_text(seconds: float) -> str:
    if seconds >= 60:
        minutes, remainder = divmod(round(seconds), 60)
        return f"{minutes} хв" + (f" {remainder} с" if remainder else "")
    return f"{seconds:.1f} с"


@dataclass(frozen=True)
class SurveyReport:
    identity: str
    backend: str
    low_hz: float
    high_hz: float
    started_at: str
    duration_seconds: float
    sweeps: int
    bins_per_sweep: int
    power_values: int
    accumulated_samples: int
    peak_frequency_hz: float | None
    peak_power_db: float | None
    status: str
    stopped_at: str
    receiver: str
    tuner: str
    gain_db: float | None
    bin_width_hz: float | None
    requested_low_hz: int
    requested_high_hz: int
    requested_bin_hz: int
    integration_seconds: int
    timezone: str = "Europe/Kyiv"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self) -> str:
        zone = ZoneInfo(self.timezone)
        start = datetime.fromisoformat(self.started_at).astimezone(zone)
        stop = datetime.fromisoformat(self.stopped_at).astimezone(zone)
        gain = "автоматичне" if self.gain_db is None else f"{self.gain_db:g} dB (запитане)"
        resolution = "невідомо" if self.bin_width_hz is None else f"{self.bin_width_hz / 1000:.3f} кГц"
        peak = "Максимальний рівень: дані відсутні"
        if self.peak_frequency_hz is not None:
            peak = (f"Максимальний рівень:\nЧастота: {self.peak_frequency_hz / 1e6:.6f} МГц\n"
                    f"Рівень: {self.peak_power_db:.2f} dB")
        status = {"success": "успішно", "scan_failed": "помилка сканування"}.get(self.status, "помилка")
        return (
            "RF Sentinel — звіт моніторингу спектра\n\n"
            f"Станція: {self.identity}\n"
            f"Початок сканування: {start:%d.%m.%Y %H:%M:%S}\n"
            f"Завершення: {stop:%d.%m.%Y %H:%M:%S}\n"
            f"Часовий пояс: {self.timezone}\n"
            f"Тривалість: {duration_text(self.duration_seconds)}\n"
            f"Приймач: {self.receiver}\nТюнер: {self.tuner}\nПідсилення: {gain}\n"
            f"Діапазон: {self.low_hz / 1e6:.6f}–{self.high_hz / 1e6:.6f} МГц\n"
            f"Частотна роздільна здатність: {resolution}\n"
            f"Інтервал накопичення: {self.integration_seconds} с\n"
            f"Кількість проходів: {self.sweeps}\n"
            f"Кількість частотних комірок: {self.bins_per_sweep}\n\n"
            f"{peak}\n\nСтатус: {status}\n\n"
            "Рівні потужності некалібровані та призначені для відносного порівняння."
        )


def make_report(result: ScanResult, identity: str, timezone: str = "Europe/Kyiv") -> SurveyReport:
    data = result.spectrum
    peak, index = max(
        (power, index) for frame in data.frames for index, power in enumerate(frame.powers)
    )
    widths = [b - a for a, b in zip(data.edges_hz, data.edges_hz[1:])]
    from statistics import median
    return SurveyReport(
        identity, result.backend, data.edges_hz[0], data.edges_hz[-1],
        result.started_at.isoformat(), result.duration_seconds, len(data.frames),
        len(data.edges_hz) - 1, sum(len(frame.powers) for frame in data.frames),
        sum(frame.sample_count for frame in data.frames),
        (data.edges_hz[index] + data.edges_hz[index + 1]) / 2, peak, "success",
        (result.started_at + timedelta(seconds=result.duration_seconds)).isoformat(),
        result.receiver, result.tuner, result.gain_db, median(widths),
        result.profile.low_hz, result.profile.high_hz, result.profile.bin_hz,
        result.profile.integration_seconds, timezone,
    )


def failed_report(profile: ScanProfile, identity: str, started: datetime,
                  duration: float, timezone: str = "Europe/Kyiv") -> SurveyReport:
    return SurveyReport(identity, "unavailable", profile.low_hz, profile.high_hz,
                        started.isoformat(), duration, 0, 0, 0, 0, None, None, "scan_failed",
                        (started + timedelta(seconds=duration)).isoformat(), "невідомо", "невідомо",
                        None, None, profile.low_hz, profile.high_hz, profile.bin_hz,
                        profile.integration_seconds, timezone)


def color_limits(powers) -> tuple[float, float]:
    """Детерміновані P2/P98 лише для кольорів; вимірювання не змінюються."""
    import numpy as np
    values = np.asarray(powers, dtype=float)
    if not values.size or not np.isfinite(values).all():
        raise ValueError("Карта спектра потребує скінченних вимірювань")
    low, high = np.percentile(values, (2, 98), method="linear")
    if high - low < 1.0:
        midpoint = (low + high) / 2
        low, high = midpoint - 0.5, midpoint + 0.5
    return float(low), float(high)


def heatmap_grid(result: ScanResult):
    """Геометрія без інтерполяції: частота зростає праворуч, час — униз."""
    import numpy as np
    data = result.spectrum
    frequencies = np.asarray(data.edges_hz, dtype=float)
    powers = np.asarray([frame.powers for frame in data.frames], dtype=float)
    times = np.asarray([frame.timestamp.timestamp() for frame in data.frames])
    if (frequencies.size < 2 or not np.isfinite(frequencies).all()
            or not (np.diff(frequencies) > 0).all() or not times.size
            or not (np.diff(times) > 0).all()
            or powers.shape != (len(times), len(frequencies) - 1)
            or not np.isfinite(powers).all()
            or any(frame.timestamp.tzinfo is None for frame in data.frames)):
        raise ValueError("Неузгоджені частотні або часові координати карти спектра")
    first = max(result.started_at.timestamp(), times[0] - result.profile.integration_seconds)
    if first >= times[0]:
        raise ValueError("Час першого проходу має бути пізнішим за початок сканування")
    return frequencies, np.concatenate(([first], times)), powers


def render_heatmap(result: ScanResult, destination: Path, timezone: str = "Europe/Kyiv") -> None:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    from matplotlib.dates import AutoDateLocator, DateFormatter
    from matplotlib.figure import Figure
    from matplotlib.ticker import AutoMinorLocator, MaxNLocator

    frequencies, times, powers = heatmap_grid(result)
    low, high = color_limits(powers)
    zone = ZoneInfo(timezone)
    # Послідовне збільшення яскравості: темний → фіолетовий → червоний → жовтий → білий.
    palette = LinearSegmentedColormap.from_list(
        "rf_sentinel", ["#05050e", "#161345", "#40236b", "#852d63",
                        "#c43b46", "#ed6b32", "#f6b743", "#f7e889", "#fffdeb"], N=256,
    )
    width = max(1800, min(6000, len(frequencies) + 340))
    dpi = 150
    figure = Figure(figsize=(width / dpi, 7), dpi=dpi, facecolor="#0b101b")
    FigureCanvasAgg(figure)
    axes = figure.add_axes((0.065, 0.18, 0.84, 0.67), facecolor="#05050e")
    # Matplotlib date units — дні від Unix epoch; pcolormesh не згладжує комірки.
    mesh = axes.pcolormesh(frequencies / 1e6, times / 86400, powers,
                          shading="flat", antialiased=False, rasterized=True,
                          cmap=palette, norm=Normalize(low, high, clip=True))
    axes.invert_yaxis()
    axes.xaxis.tick_top()
    axes.xaxis.set_label_position("top")
    axes.set_xlabel("Частота, МГц", color="#e8edf5", labelpad=14)
    axes.xaxis.set_major_locator(MaxNLocator(nbins=min(18, max(6, width // 230))))
    axes.xaxis.set_minor_locator(AutoMinorLocator(5))
    axes.yaxis.set_major_locator(AutoDateLocator(tz=zone, minticks=4, maxticks=8))
    axes.yaxis.set_major_formatter(DateFormatter("%H:%M:%S", tz=zone))
    axes.set_ylabel(f"Час · {timezone}", color="#e8edf5")
    axes.tick_params(colors="#d9e2f0", which="both", labelsize=10)
    for spine in axes.spines.values():
        spine.set_color("#728098")
    scale_axes = figure.add_axes((0.925, 0.18, 0.012, 0.67))
    scale = figure.colorbar(mesh, cax=scale_axes, extend="both")
    scale.set_label("Рівень, dB", color="#e8edf5")
    scale.ax.tick_params(colors="#d9e2f0")
    start = result.started_at.astimezone(zone)
    resolution = (frequencies[-1] - frequencies[0]) / (len(frequencies) - 1) / 1000
    figure.text(0.065, 0.965, "RF Sentinel — карта спектра", color="#f4f7fc", fontsize=17)
    figure.text(0.065, 0.09,
                f"Початок: {start:%d.%m.%Y %H:%M:%S} · Тривалість: {duration_text(result.duration_seconds)}"
                f" · Проходів: {len(powers)} · Інтервал: {result.profile.integration_seconds} с",
                color="#d9e2f0", fontsize=11)
    figure.text(0.065, 0.045,
                f"Діапазон: {frequencies[0] / 1e6:.3f}–{frequencies[-1] / 1e6:.3f} МГц"
                f" · Роздільна здатність: {resolution:.3f} кГц · Кольори: P2–P98"
                " · Рівні некалібровані", color="#aab8ce", fontsize=10)
    figure.savefig(destination, format="png", dpi=dpi)
    figure.clear()


# Зворотна сумісність для локальних інструментів попереднього prototype.
render_waterfall = render_heatmap
