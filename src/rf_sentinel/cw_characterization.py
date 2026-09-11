"""Bounded LibreVNA generator -> RTL-SDR CW characterization tool."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time
from typing import Callable, Iterable, Sequence


DEFAULT_FREQUENCIES_HZ = (
    50_000_000,
    100_000_000,
    230_000_000,
    500_000_000,
    800_000_000,
    1_000_000_000,
    1_200_000_000,
    1_500_000_000,
    1_700_000_000,
)
DEFAULT_SCPI_PORT = 19542
DEFAULT_EXPECTED_FREQUENCY_TOLERANCE_HZ = 250_000
DEFAULT_MIN_CARRIER_DELTA_DB = 1.0
MAX_POINTS = 100
MAX_CSV_BYTES = 64 * 1024 * 1024
POINT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CWPointRecord:
    sequence: int
    requested_frequency_hz: int
    measured_peak_frequency_hz: float | None
    raw_peak_level_db: float | None
    timestamp: str
    duration_seconds: float
    return_code: int
    csv_path: str
    stderr_path: str
    command: list[str]
    command_text: str
    scpi_commands: list[str]
    settle_seconds: float
    window_hz: int
    gain_db: float | str
    requested_generator_level_dbm: float
    libre_vna_port: int
    status: str
    nearest_bin_frequency_hz: float | None
    nearest_bin_level_db: float | None
    local_peak_frequency_hz: float | None
    local_peak_level_db: float | None
    full_window_peak_frequency_hz: float | None
    full_window_peak_level_db: float | None
    local_baseline_db: float | None
    carrier_delta_db: float | None
    expected_frequency_tolerance_hz: int


@dataclass(frozen=True)
class CWSpectrumAnalysis:
    nearest_bin: tuple[float, float] | None
    local_peak: tuple[float, float] | None
    full_window_peak: tuple[float, float] | None
    local_baseline_db: float | None
    carrier_delta_db: float | None
    carrier_valid: bool


def generator_commands(frequency_hz: int, level_dbm: float, *, port: int = 1) -> tuple[str, ...]:
    """Повертає SCPI set-команди для одного generator point."""
    return (
        ":DEV:MODE GEN",
        f":GEN:LVL {level_dbm:g}",
        f":GEN:FREQ {frequency_hz:d}",
        f":GEN:PORT {port:d}",
    )


def generator_shutdown_command(*, port: int = 0) -> str:
    return f":GEN:PORT {port:d}"


def build_point_command(*, frequency_hz: int, window_hz: int, bin_hz: int,
                        integration_seconds: int, device: int, gain: float | None,
                        csv_path: Path, capture_center_hz: int | None = None,
                        offset_tuning: bool = False,
                        fft_window: str | None = None) -> list[str]:
    """Build one rtl_power capture, optionally centered away from the CW source."""
    center_hz = frequency_hz if capture_center_hz is None else capture_center_hz
    half_window = window_hz // 2
    command = [
        "rtl_power", "-f", f"{center_hz - half_window}:{center_hz + half_window}:{bin_hz}",
        "-i", str(integration_seconds), "-1", "-d", str(device),
    ]
    if gain is not None:
        command += ["-g", str(gain)]
    if fft_window is not None:
        command += ["-w", fft_window]
    if offset_tuning:
        command.append("-O")
    command.append(str(csv_path))
    return command


def _rtl_power_points(path: Path) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    with path.open("r", encoding="ascii", errors="strict", newline="") as stream:
        for columns in csv.reader(stream, skipinitialspace=True):
            if not columns or len(columns) < 7:
                continue
            try:
                low, high, step = map(float, columns[2:5])
                values = [float(value) for value in columns[6:]]
            except (ValueError, TypeError):
                continue
            if (not all(math.isfinite(value) for value in (low, high, step, *values))
                    or not values or not 0 < low < high or step <= 0):
                continue
            expected_bins = round((high - low) / step)
            if (expected_bins <= 0
                    or abs(expected_bins * step - (high - low))
                    > 2 + expected_bins * 0.0051):
                continue
            if (len(values) == expected_bins + 1
                    and values[-1] == values[-2]):
                values = values[:-1]
            if len(values) != expected_bins:
                continue
            points.extend((low + (index + 0.5) * step, value)
                          for index, value in enumerate(values))
    return points


def extract_peak(path: Path, *, target_frequency_hz: int, window_hz: int) -> tuple[float, float] | None:
    """Знаходить raw maximum у вузькому вікні; не робить normalization."""
    half_window = window_hz / 2
    points = [point for point in _rtl_power_points(path)
              if abs(point[0] - target_frequency_hz) <= half_window]
    return max(points, key=lambda point: point[1]) if points else None


def analyze_spectrum(path: Path, *, expected_frequency_hz: int,
                     expected_frequency_tolerance_hz: int = DEFAULT_EXPECTED_FREQUENCY_TOLERANCE_HZ,
                     min_carrier_delta_db: float = DEFAULT_MIN_CARRIER_DELTA_DB) -> CWSpectrumAnalysis:
    """Аналізує raw spectrum, не приймаючи сторонній global peak за carrier."""
    if expected_frequency_tolerance_hz <= 0:
        raise ValueError("expected frequency tolerance має бути додатним")
    points = _rtl_power_points(path)
    if not points:
        return CWSpectrumAnalysis(None, None, None, None, None, False)

    nearest_bin = min(points, key=lambda point: abs(point[0] - expected_frequency_hz))
    local_points = [
        point for point in points
        if abs(point[0] - expected_frequency_hz) <= expected_frequency_tolerance_hz
    ]
    local_peak = max(local_points, key=lambda point: point[1]) if local_points else None
    full_window_peak = max(points, key=lambda point: point[1])
    if local_peak is None:
        return CWSpectrumAnalysis(nearest_bin, None, full_window_peak, None, None, False)

    baseline_values = [value for point in local_points if point != local_peak
                       for value in (point[1],)]
    if not baseline_values:
        baseline_values = [point[1] for point in local_points]
    local_baseline_db = _median(baseline_values)
    carrier_delta_db = local_peak[1] - local_baseline_db
    carrier_valid = carrier_delta_db >= min_carrier_delta_db
    return CWSpectrumAnalysis(
        nearest_bin, local_peak, full_window_peak, local_baseline_db,
        carrier_delta_db, carrier_valid)


def _median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


class LibreVNAScpi:
    """Мінімальний newline-framed SCPI client для LibreVNA-GUI."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._socket.settimeout(timeout)

    def send(self, command: str) -> None:
        self._socket.sendall((command + "\n").encode("ascii"))

    def close(self) -> None:
        self._socket.close()


def run_cw_characterization(args: argparse.Namespace, *, scpi=None,
                            runner=subprocess.run, sleeper=time.sleep,
                            clock=time.monotonic, now=lambda: datetime.now(UTC)) -> list[CWPointRecord]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    frequencies = tuple(args.frequencies_hz)
    if not frequencies or len(frequencies) > MAX_POINTS:
        raise ValueError("Кількість CW frequency points має бути від 1 до 100")
    own_scpi = scpi is None
    if own_scpi:
        scpi = LibreVNAScpi(args.vna_host, args.vna_port, args.scpi_timeout)
    records: list[CWPointRecord] = []
    try:
        for sequence, frequency_hz in enumerate(frequencies, 1):
            csv_path = output_dir / f"point-{sequence:03d}-{frequency_hz // 1_000_000}MHz.csv"
            stderr_path = output_dir / f"point-{sequence:03d}-{frequency_hz // 1_000_000}MHz.stderr.txt"
            command = build_point_command(
                frequency_hz=frequency_hz, window_hz=args.window_hz, bin_hz=args.bin_hz,
                integration_seconds=args.integration_seconds, device=args.device,
                gain=args.gain, csv_path=csv_path)
            scpi_commands = list(generator_commands(
                frequency_hz, args.generator_level_dbm, port=args.libre_vna_port))
            started = clock()
            timestamp = now().isoformat()
            return_code = 0
            try:
                for command_text in scpi_commands:
                    scpi.send(command_text)
                sleeper(args.settle_seconds)
                with stderr_path.open("wb") as diagnostics:
                    completed = runner(
                        command, shell=False, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=diagnostics,
                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                             "TZ": "UTC", "LC_ALL": "C"},
                        timeout=POINT_TIMEOUT_SECONDS, check=False)
                return_code = completed.returncode
            except (OSError, subprocess.TimeoutExpired):
                return_code = 124
            analysis = CWSpectrumAnalysis(None, None, None, None, None, False)
            capture_failed = return_code != 0
            try:
                capture_usable = (csv_path.exists()
                                  and csv_path.stat().st_size <= MAX_CSV_BYTES)
            except OSError:
                capture_usable = False
            if return_code == 0 and capture_usable:
                try:
                    analysis = analyze_spectrum(
                        csv_path,
                        expected_frequency_hz=frequency_hz,
                        expected_frequency_tolerance_hz=args.expected_frequency_tolerance_hz,
                        min_carrier_delta_db=args.min_carrier_delta_db,
                    )
                    if analysis.nearest_bin is None:
                        capture_failed = True
                except (OSError, UnicodeError, ValueError):
                    capture_failed = True
            elif return_code == 0:
                capture_failed = True
            carrier = analysis.local_peak if analysis.carrier_valid else None
            records.append(CWPointRecord(
                sequence, frequency_hz, None if carrier is None else carrier[0],
                None if carrier is None else carrier[1], timestamp, clock() - started,
                return_code, str(csv_path), str(stderr_path), command, shlex.join(command),
                scpi_commands, args.settle_seconds, args.window_hz,
                "auto" if args.gain is None else args.gain, args.generator_level_dbm,
                args.libre_vna_port,
                ("failed" if capture_failed else
                 "valid" if analysis.carrier_valid else "invalid"),
                None if analysis.nearest_bin is None else analysis.nearest_bin[0],
                None if analysis.nearest_bin is None else analysis.nearest_bin[1],
                None if analysis.local_peak is None else analysis.local_peak[0],
                None if analysis.local_peak is None else analysis.local_peak[1],
                None if analysis.full_window_peak is None else analysis.full_window_peak[0],
                None if analysis.full_window_peak is None else analysis.full_window_peak[1],
                analysis.local_baseline_db, analysis.carrier_delta_db,
                args.expected_frequency_tolerance_hz))
    finally:
        try:
            scpi.send(generator_shutdown_command())
        finally:
            if own_scpi:
                scpi.close()
    _write_results(output_dir, records, args)
    return records


def _write_results(output_dir: Path, records: list[CWPointRecord], args: argparse.Namespace) -> None:
    payload = {
        "schema_version": "rtl-sdr-cw-characterization.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "correction_applied": False,
        "interpolation_applied": False,
        "normalization_applied": False,
        "configuration": {
            "vna_host": args.vna_host, "vna_port": args.vna_port,
            "requested_generator_level_dbm": args.generator_level_dbm,
            "libre_vna_port": args.libre_vna_port,
            "frequencies_hz": list(args.frequencies_hz), "settle_seconds": args.settle_seconds,
            "gain_db": "auto" if args.gain is None else args.gain,
            "window_hz": args.window_hz, "bin_hz": args.bin_hz,
            "expected_frequency_tolerance_hz": args.expected_frequency_tolerance_hz,
            "min_carrier_delta_db": args.min_carrier_delta_db,
        },
        "points": [asdict(record) for record in records],
    }
    (output_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    fields = list(asdict(records[0]).keys()) if records else list(CWPointRecord.__annotations__)
    with (output_dir / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(record) for record in records)
    lines = ["# LibreVNA → RTL-SDR CW characterization", "", "Автоматична корекція: ні",
             "Interpolation: ні", "Normalization: ні", f"Кількість точок: {len(records)}", "", "## Points", ""]
    for record in records:
        peak = (f"{record.measured_peak_frequency_hz:.2f} Hz / {record.raw_peak_level_db:.3f} dB"
                if record.measured_peak_frequency_hz is not None else "немає valid carrier")
        lines.append(f"- {record.requested_frequency_hz / 1e6:g} MHz: peak {peak}; "
                     f"status={record.status}; delta={record.carrier_delta_db}; "
                     f"rc={record.return_code}; {record.duration_seconds:.3f} с")
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _frequency_list(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("частоти мають бути цілими Hz через кому") from None
    if not result or any(value <= 0 for value in result):
        raise argparse.ArgumentTypeError("потрібен непорожній список додатних частот")
    return result


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("має бути додатним цілим")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rf-cw-characterize")
    parser.add_argument("--vna-host", default="127.0.0.1")
    parser.add_argument("--vna-port", type=int, default=DEFAULT_SCPI_PORT)
    parser.add_argument("--libre-vna-port", type=int, default=1)
    parser.add_argument("--scpi-timeout", type=float, default=5.0)
    parser.add_argument("--generator-level-dbm", type=float, default=-40.0)
    parser.add_argument("--frequencies-hz", type=_frequency_list,
                        default=DEFAULT_FREQUENCIES_HZ)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--window-hz", type=_positive_int, default=2_000_000,
                        help="Загальна ширина peak-search window")
    parser.add_argument("--expected-frequency-tolerance-hz", type=_positive_int,
                        default=DEFAULT_EXPECTED_FREQUENCY_TOLERANCE_HZ)
    parser.add_argument("--min-carrier-delta-db", type=float,
                        default=DEFAULT_MIN_CARRIER_DELTA_DB)
    parser.add_argument("--bin-hz", type=_positive_int, default=100_000)
    parser.add_argument("--integration-seconds", type=_positive_int, default=1)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (1 <= args.vna_port <= 65535 and 1 <= args.libre_vna_port <= 2
            and 0 <= args.device <= 255):
        raise SystemExit("Некоректні VNA port або RTL device")
    if not -42 <= args.generator_level_dbm <= -10:
        raise SystemExit("LibreVNA generator level має бути в межах приблизно -42…-10 dBm")
    if not 0 <= args.settle_seconds <= 10 or args.scpi_timeout <= 0:
        raise SystemExit("Некоректні settle/scpi timeout")
    if not 10_000 <= args.bin_hz <= 2_800_000:
        raise SystemExit("--bin-hz має бути в межах 10000–2800000")
    if args.min_carrier_delta_db < 0:
        raise SystemExit("--min-carrier-delta-db не може бути від'ємним")
    if args.gain is not None and not 0 <= args.gain <= 50:
        raise SystemExit("--gain має бути в межах 0–50 dB")
    if any(not 24_000_000 <= frequency <= 1_766_000_000 for frequency in args.frequencies_hz):
        raise SystemExit("CW frequencies мають бути в межах 24–1766 MHz")
    if len(args.frequencies_hz) > MAX_POINTS:
        raise SystemExit("Не більше 100 CW frequency points")
    records = run_cw_characterization(args)
    for record in records:
        print(f"point {record.sequence}: {record.requested_frequency_hz / 1e6:g} MHz, "
              f"peak={record.raw_peak_level_db}, status={record.status}, rc={record.return_code}")
    return 0 if all(record.return_code == 0 and record.status == "valid"
                    for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
