"""Українські звіти та інженерна карта спектра, незалежні від транспорту."""

import json
import os
import heapq
import math
from array import array
from collections.abc import Sequence
from tempfile import TemporaryFile
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from rf_sentinel.storage import SQLiteSweepReader
from rf_sentinel.spectrum import ScanProfile, ScanResult

_SORT_RUN_VALUES = 65536
_MERGE_FAN_IN = 32


class _PayloadFile:
    """Private snapshot, unlinked/closed when its last numeric view is released."""

    def __init__(self, file=None):
        self.file = TemporaryFile() if file is None else file

    def append(self, values):
        offset = self.file.tell()
        packed = array("d", values)
        self.file.write(packed.tobytes())
        self.file.flush()
        return _DiskValues(self, offset, len(packed)) if packed else ()


class _DiskValues(Sequence):
    """Read-only float64 sequence; reads do not change the shared file position."""

    def __init__(self, owner, offset, count):
        self.owner, self.offset, self.count = owner, offset, count

    def __len__(self):
        return self.count

    def __iter__(self):
        for start in range(0, self.count, 4096):
            count = min(4096, self.count - start)
            blob = os.pread(self.owner.file.fileno(), count * 8, self.offset + start * 8)
            if len(blob) != count * 8:
                raise OSError("Truncated report payload snapshot")
            values = array("d")
            values.frombytes(blob)
            yield from values

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self.count)))
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        values = array("d")
        values.frombytes(os.pread(self.owner.file.fileno(), 8, self.offset + index * 8))
        return values[0]

    def __eq__(self, other):
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))


def _json_value(value):
    """Explicit materialization only for callers of the legacy to_dict API."""
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (Sequence, array)) and not isinstance(value, str):
        return [_json_value(item) for item in value]
    return value


def _write_json(stream, value):
    """Stream dataclasses and numeric sequences without constructing a JSON tree."""
    if is_dataclass(value):
        stream.write("{")
        for index, field in enumerate(fields(value)):
            if index:
                stream.write(",")
            stream.write(json.dumps(field.name) + ":")
            _write_json(stream, getattr(value, field.name))
        stream.write("}")
    elif isinstance(value, (Sequence, array)) and not isinstance(value, str):
        stream.write("[")
        for index, item in enumerate(value):
            if index:
                stream.write(",")
            _write_json(stream, item)
        stream.write("]")
    else:
        stream.write(json.dumps(value.isoformat() if isinstance(value, datetime) else value,
                                ensure_ascii=False))


@dataclass(frozen=True)
class ReportGap:
    """An observed interval with no persisted sweep start/finish coverage."""

    start: datetime
    end: datetime
    kind: str
    previous_sweep_id: str | None = None
    next_sweep_id: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class ReportSweep:
    """One ordered sweep summary; failed sweeps intentionally have no payload."""

    sweep_id: str
    started_at: datetime
    finished_at: datetime
    outcome: str
    coverage: float
    frequencies_hz: Sequence[float] = ()
    powers: Sequence[float] = ()
    frequency_start_hz: float | None = None
    frequency_stop_hz: float | None = None
    bin_width_hz: float | None = None


@dataclass(frozen=True)
class ReportData:
    """Canonical data contract for a persisted time-window report.

    ``coverage`` is weighted persisted-bin coverage: sum(observed_bins) /
    sum(expected_bins), including failed sweeps when their expected count is
    known.  ``gaps`` includes the window edges and every positive interval
    between sweep finish and the next sweep start; failed sweeps remain in the
    ordered timeline and in all outcome counts.

    Engine-built/restored reports keep numeric payloads in an owned temporary
    snapshot, independent of the source database. Only sweep/gap metadata stays
    resident. Sequence indexing and iteration remain supported. ``to_dict`` is
    the explicit, potentially large compatibility API; package writing streams.
    """

    window_start: datetime
    window_end: datetime
    sweep_count: int
    success_count: int
    partial_count: int
    failed_count: int
    coverage: float
    frequency_range_hz: tuple[float, float] | None
    time_ordering: tuple[str, ...]
    peak_frequency_hz: float | None
    peak_power_db: float | None
    sweeps: tuple[ReportSweep, ...]
    gaps: tuple[ReportGap, ...]

    def to_dict(self) -> dict:
        """Materialize a JSON-compatible mapping; package generation streams instead."""
        return _json_value(self)

    @classmethod
    def from_dict(cls, value: dict) -> "ReportData":
        """Restore ReportData produced by :meth:`to_dict`."""
        payload = dict(value)
        payload["window_start"] = datetime.fromisoformat(payload["window_start"])
        payload["window_end"] = datetime.fromisoformat(payload["window_end"])
        payload["frequency_range_hz"] = (
            tuple(payload["frequency_range_hz"])
            if payload["frequency_range_hz"] is not None else None
        )
        payload["time_ordering"] = tuple(payload["time_ordering"])
        snapshot = _PayloadFile()
        payload["sweeps"] = tuple(
            ReportSweep(
                item["sweep_id"], datetime.fromisoformat(item["started_at"]),
                datetime.fromisoformat(item["finished_at"]), item["outcome"],
                item["coverage"], snapshot.append(item.get("frequencies_hz", ())),
                snapshot.append(item.get("powers", ())), item.get("frequency_start_hz"),
                item.get("frequency_stop_hz"), item.get("bin_width_hz"),
            ) for item in payload["sweeps"]
        )
        payload["gaps"] = tuple(
            ReportGap(
                datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"]),
                item["kind"], item.get("previous_sweep_id"), item.get("next_sweep_id"),
            ) for item in payload["gaps"]
        )
        return cls(**payload)

    def to_text(self, timezone: str = "Europe/Kyiv") -> str:
        """Render the report for a human; machine-readable names stay in JSON."""
        zone = ZoneInfo(timezone)
        start = self.window_start.astimezone(zone)
        end = self.window_end.astimezone(zone)
        frequency = "дані відсутні"
        if self.frequency_range_hz is not None:
            frequency = (f"{self.frequency_range_hz[0] / 1e6:.6f}–"
                         f"{self.frequency_range_hz[1] / 1e6:.6f} МГц")
        peak = "дані відсутні"
        if self.peak_frequency_hz is not None:
            peak = (f"{self.peak_frequency_hz / 1e6:.6f} МГц, "
                    f"{self.peak_power_db:.2f} dB")
        quality = []
        if self.partial_count:
            quality.append("є неповні проходи")
        if self.failed_count:
            quality.append("є невдалі проходи")
        if self.gaps:
            quality.append(f"виявлено прогалини: {len(self.gaps)}")
        warning = "; ".join(quality) if quality else "критичних застережень не виявлено"
        return (
            "RF Sentinel — звіт часового вікна\n\n"
            f"Початок вікна: {start:%d.%m.%Y %H:%M:%S} ({timezone})\n"
            f"Завершення вікна: {end:%d.%m.%Y %H:%M:%S} ({timezone})\n"
            f"Тривалість: {duration_text((self.window_end - self.window_start).total_seconds())}\n"
            f"Проходи: {self.success_count} успішних, {self.partial_count} неповних, "
            f"{self.failed_count} невдалих (усього {self.sweep_count})\n"
            f"Покриття: {self.coverage * 100:.1f}%\n"
            f"Діапазон частот: {frequency}\n"
            f"Пікова частота/потужність: {peak}\n"
            f"Прогалини: {len(self.gaps)}\n"
            f"Попередження якості: {warning}\n\n"
            "Примітка: рівні dB некалібровані та придатні лише для відносного порівняння."
        )


@dataclass(frozen=True)
class ReportPackage:
    """All derived artifacts and the window they describe."""

    artifact_dir: Path
    report_json: Path
    report_txt: Path
    waterfall: Path
    heatmap: Path
    window_start: datetime
    window_end: datetime

    @property
    def paths(self) -> tuple[Path, Path, Path, Path]:
        return self.report_json, self.report_txt, self.waterfall, self.heatmap

    def to_dict(self) -> dict:
        return {
            "artifact_dir": str(self.artifact_dir),
            "report_json": str(self.report_json),
            "report_txt": str(self.report_txt),
            "waterfall": str(self.waterfall),
            "heatmap": str(self.heatmap),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
        }


def last_hour_window(end: datetime) -> tuple[datetime, datetime]:
    """Return a one-hour half-open window ending at ``end``."""
    return end - timedelta(hours=1), end


def completed_calendar_hour(now: datetime, timezone: str = "Europe/Kyiv") -> tuple[datetime, datetime]:
    """Return the most recently completed local calendar hour."""
    local = now.astimezone(ZoneInfo(timezone)).replace(minute=0, second=0, microsecond=0)
    return local - timedelta(hours=1), local


def completed_calendar_day(now: datetime, timezone: str = "Europe/Kyiv") -> tuple[datetime, datetime]:
    """Return the most recently completed local calendar day."""
    local = now.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local - timedelta(days=1), local


class SQLiteReportEngine:
    """Build report data from SQLite only, without an acquisition dependency."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def build(self, start: datetime, end: datetime) -> ReportData:
        if (start.tzinfo is None or start.utcoffset() is None
                or end.tzinfo is None or end.utcoffset() is None):
            raise ValueError("report window timestamps must be timezone-aware")
        if end <= start:
            raise ValueError("report window must be non-empty")
        snapshot = _PayloadFile()
        reader = SQLiteSweepReader(self.path)
        ordered = []
        counts = {outcome: 0 for outcome in ("success", "partial", "failed")}
        expected = observed = 0
        ranges = []
        peak_frequency = peak_power = None
        gaps = []
        try:
            for item in reader.iter_sweeps(start, end):
                counts[item.outcome] += 1
                expected += item.coverage.expected_bins
                observed += item.coverage.observed_bins
                if item.outcome != "failed":
                    payload = ReportSweep(
                        item.sweep_id, item.started_at, item.finished_at, item.outcome,
                        item.coverage.fraction, snapshot.append(item.frequencies_hz),
                        snapshot.append(item.powers), item.start_hz, item.stop_hz,
                        item.bin_width_hz)
                    ranges.append((item.start_hz, item.stop_hz))
                    for frequency, power in zip(item.frequencies_hz, item.powers):
                        if peak_power is None or power > peak_power:
                            peak_frequency, peak_power = frequency, power
                else:
                    payload = ReportSweep(
                        item.sweep_id, item.started_at, item.finished_at, item.outcome,
                        item.coverage.fraction)
                ordered.append(payload)
        finally:
            reader.close()
        ordered = tuple(ordered)
        payloads = ordered
        coverage = observed / expected if expected else 0.0
        frequency_range = ((min(value[0] for value in ranges), max(value[1] for value in ranges))
                           if ranges else None)
        if not ordered:
            gaps.append(ReportGap(start, end, "window"))
        else:
            if ordered[0].started_at > start:
                gaps.append(ReportGap(start, ordered[0].started_at, "leading",
                                      next_sweep_id=ordered[0].sweep_id))
            for previous, current in zip(ordered, ordered[1:]):
                gap_start = max(previous.finished_at, start)
                if current.started_at > gap_start:
                    gaps.append(ReportGap(gap_start, current.started_at, "between",
                                          previous.sweep_id, current.sweep_id))
            if ordered[-1].finished_at < end:
                gaps.append(ReportGap(ordered[-1].finished_at, end, "trailing",
                                      previous_sweep_id=ordered[-1].sweep_id))
        return ReportData(
            start, end, len(ordered), counts["success"], counts["partial"], counts["failed"],
            coverage, frequency_range, tuple(item.sweep_id for item in ordered),
            peak_frequency, peak_power, payloads, tuple(gaps),
        )


def _report_limits(report: ReportData) -> tuple[float, float]:
    if report.frequency_range_hz is None:
        # Empty and failed windows still get a valid, clearly empty artifact.
        return 0.0, 1.0
    low, high = report.frequency_range_hz
    if not high > low:
        raise ValueError("PNG report frequency range must be increasing")
    return low, high


def _report_color_limits(report: ReportData) -> tuple[float, float]:
    """Exact linear P2/P98 using sorted disk runs and bounded merge buffers."""
    import numpy as np

    from itertools import islice

    source = (power for sweep in report.sweeps for power in sweep.powers)
    with TemporaryFile() as scratch:
        owner = _PayloadFile(scratch)
        runs = []
        total = 0
        while True:
            chunk = np.fromiter(islice(source, _SORT_RUN_VALUES), dtype=float)
            if not chunk.size:
                break
            if not np.isfinite(chunk).all():
                raise ValueError("Карта спектра потребує скінченних вимірювань")
            chunk.sort()
            offset = scratch.tell()
            scratch.write(chunk.tobytes())
            runs.append(_DiskValues(owner, offset, len(chunk)))
            total += len(chunk)
        if not total:
            return -1.0, 1.0
        scratch.flush()
        # Bound merge fan-in (and its 4096-value read buffers), even for long
        # windows. Intermediate runs stay on the same private scratch file.
        while len(runs) > _MERGE_FAN_IN:
            merged_runs = []
            for start in range(0, len(runs), _MERGE_FAN_IN):
                group = runs[start:start + _MERGE_FAN_IN]
                offset = scratch.tell()
                merged = heapq.merge(*(iter(run) for run in group))
                while True:
                    block = array("d", islice(merged, 65536))
                    if not block:
                        break
                    scratch.write(block.tobytes())
                scratch.flush()
                merged_runs.append(_DiskValues(owner, offset, sum(map(len, group))))
            runs = merged_runs
        ranks = [(total - 1) * q for q in (0.02, 0.98)]
        wanted = {int(math.floor(r)) for r in ranks} | {int(math.ceil(r)) for r in ranks}
        selected = {}
        last_rank = max(wanted)
        for index, value in enumerate(heapq.merge(*(iter(run) for run in runs))):
            if index in wanted:
                selected[index] = value
            if index == last_rank:
                break
        low, high = (selected[math.floor(r)] + (r % 1) *
                     (selected[math.ceil(r)] - selected[math.floor(r)]) for r in ranks)
        if high - low < 1.0:
            midpoint = (low + high) / 2
            low, high = midpoint - 0.5, midpoint + 0.5
        return float(low), float(high)


def _frequency_edges(frequencies: Sequence[float], low: float, high: float,
                     bin_width_hz: float | None):
    import numpy as np

    centers = np.fromiter(frequencies, dtype=float, count=len(frequencies))
    if centers.size == 0 or not np.isfinite(centers).all() or not (np.diff(centers) > 0).all():
        raise ValueError("ReportData has invalid frequency bins")
    if centers.size == 1:
        width = bin_width_hz if bin_width_hz and bin_width_hz > 0 else high - low
        edges = np.asarray([centers[0] - width / 2, centers[0] + width / 2], dtype=float)
    else:
        edges = np.empty(centers.size + 1, dtype=float)
        edges[1:-1] = (centers[:-1] + centers[1:]) / 2
        edges[0] = centers[0] - (edges[1] - centers[0])
        edges[-1] = centers[-1] + (centers[-1] - edges[-2])
    return np.clip(edges, low, high)


def _report_meshes(axes, report: ReportData, *, cmap, norm, failed_color: str):
    """Retain one lightweight artist; create/draw/release one sweep at a time."""
    import numpy as np
    from matplotlib.artist import Artist
    from matplotlib.patches import Rectangle

    low, high = _report_limits(report)

    class SweepArtist(Artist):
        def __init__(self, failed):
            super().__init__()
            self.failed = failed
            self.set_zorder(3 if failed else 1)

        def draw(self, renderer):
            for sweep in report.sweeps:
                y_start = sweep.started_at.timestamp() / 86400
                y_end = sweep.finished_at.timestamp() / 86400
                if self.failed:
                    if sweep.powers or sweep.outcome != "failed":
                        continue
                    artist = Rectangle(
                        (low / 1e6, y_start), (high - low) / 1e6, y_end - y_start,
                        facecolor=failed_color, edgecolor=failed_color, hatch="///",
                        linewidth=0, alpha=0.38, zorder=3)
                    axes.add_patch(artist)
                else:
                    if not sweep.powers:
                        continue
                    if len(sweep.frequencies_hz) != len(sweep.powers):
                        raise ValueError("ReportData frequency/power payload lengths differ")
                    edges = _frequency_edges(sweep.frequencies_hz, low, high,
                                             sweep.bin_width_hz) / 1e6
                    powers = np.fromiter(sweep.powers, dtype=float, count=len(sweep.powers))
                    artist = axes.pcolormesh(
                        edges, [y_start, y_end], powers[None, :], shading="flat",
                        antialiased=False, rasterized=True, cmap=cmap, norm=norm)
                try:
                    artist.draw(renderer)
                finally:
                    artist.remove()
                    del artist
            self.stale = False

    axes.add_artist(SweepArtist(False))
    axes.add_artist(SweepArtist(True))


def _format_report_axes(figure, axes, report: ReportData, timezone: str, title: str,
                        color_mesh, *, top_x: bool = False):
    from matplotlib.dates import AutoDateLocator, DateFormatter
    from matplotlib.ticker import MaxNLocator

    zone = ZoneInfo(timezone)
    low, high = _report_limits(report)
    axes.set_xlim(low / 1e6, high / 1e6)
    axes.set_ylim(report.window_start.timestamp() / 86400,
                  report.window_end.timestamp() / 86400)
    if top_x:
        axes.xaxis.tick_top()
        axes.xaxis.set_label_position("top")
    axes.set_xlabel("Частота, МГц", labelpad=10)
    axes.set_ylabel(f"Час · {timezone}")
    axes.xaxis.set_major_locator(MaxNLocator(nbins=10))
    axes.yaxis.set_major_locator(AutoDateLocator(tz=zone, minticks=4, maxticks=8))
    axes.yaxis.set_major_formatter(DateFormatter("%H:%M:%S", tz=zone))
    axes.set_title(title, loc="left", pad=14)
    colorbar = figure.colorbar(color_mesh, ax=axes, pad=0.02)
    colorbar.set_label("Рівень, dB")


def _render_report_png(report: ReportData, destination: Path, timezone: str, *,
                       palette, title: str, axes_facecolor: str, figure_facecolor: str,
                       failed_color: str, top_x: bool) -> None:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import Normalize
    from matplotlib.figure import Figure

    low, high = _report_color_limits(report)
    figure = Figure(figsize=(13, 7), dpi=150, facecolor=figure_facecolor)
    FigureCanvasAgg(figure)
    axes = figure.add_axes((0.09, 0.12, 0.78, 0.78), facecolor=axes_facecolor)
    mesh = axes.imshow([[low, high]], cmap=palette, norm=Normalize(low, high),
                       visible=False, aspect="auto")
    _report_meshes(axes, report, cmap=palette, norm=Normalize(low, high),
                   failed_color=failed_color)
    _format_report_axes(figure, axes, report, timezone, title, mesh, top_x=top_x)
    try:
        figure.savefig(destination, format="png", dpi=150, facecolor=figure_facecolor)
    finally:
        figure.clear()


def render_report_heatmap(report: ReportData, destination: str | Path,
                          timezone: str = "Europe/Kyiv") -> None:
    """Render a classic rtl_power-style heatmap from ReportData only."""
    from matplotlib.colors import LinearSegmentedColormap

    palette = LinearSegmentedColormap.from_list(
        "rf_sentinel_rtl_heatmap",
        ["#05050e", "#101b58", "#40236b", "#852d63", "#c43b46",
         "#ed6b32", "#f6b743", "#fff3a6", "#fffdeb"], N=256,
    )
    _render_report_png(
        report, Path(destination), timezone, palette=palette,
        title="RF Sentinel — heatmap спектра", axes_facecolor="#05050e",
        figure_facecolor="#0b101b", failed_color="#9aa4b2", top_x=True,
    )


def render_report_waterfall(report: ReportData, destination: str | Path,
                            timezone: str = "Europe/Kyiv") -> None:
    """Render RF Sentinel's banded waterfall from ReportData only."""
    from matplotlib.colors import LinearSegmentedColormap

    palette = LinearSegmentedColormap.from_list(
        "rf_sentinel_waterfall",
        ["#02040b", "#09203d", "#075985", "#00a6a6", "#72d572",
         "#f3dc5b", "#ff8c42", "#fff2b2"], N=256,
    )
    _render_report_png(
        report, Path(destination), timezone, palette=palette,
        title="RF Sentinel — waterfall спектра", axes_facecolor="#02040b",
        figure_facecolor="#070b13", failed_color="#556274", top_x=False,
    )


def render_report_images(report: ReportData, waterfall: str | Path,
                         heatmap: str | Path, timezone: str = "Europe/Kyiv") -> tuple[Path, Path]:
    """Create both PNG views from one already-built ReportData value."""
    waterfall_path, heatmap_path = Path(waterfall), Path(heatmap)
    waterfall_path.parent.mkdir(parents=True, exist_ok=True)
    heatmap_path.parent.mkdir(parents=True, exist_ok=True)
    render_report_waterfall(report, waterfall_path, timezone)
    render_report_heatmap(report, heatmap_path, timezone)
    return waterfall_path, heatmap_path


def generate_report_package(report: ReportData, destination: str | Path,
                            timezone: str = "Europe/Kyiv") -> ReportPackage:
    """Write the complete four-file package from ReportData only."""
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    report_json = directory / "report.json"
    report_txt = directory / "report.txt"
    waterfall = directory / "waterfall.png"
    heatmap = directory / "heatmap.png"
    with report_json.open("w", encoding="utf-8") as stream:
        _write_json(stream, report)
        stream.write("\n")
    report_txt.write_text(report.to_text(timezone) + "\n", encoding="utf-8")
    render_report_images(report, waterfall, heatmap, timezone)
    return ReportPackage(directory, report_json, report_txt, waterfall, heatmap,
                         report.window_start, report.window_end)


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
