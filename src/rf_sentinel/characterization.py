"""Окремий bounded CLI для characterization RTL-SDR через один rtl_power sweep."""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Sequence


DEFAULT_OUTPUT_DIR = Path(".local/characterization")
MAX_CSV_BYTES = 64 * 1024 * 1024
SINGLE_SWEEP_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class SweepRecord:
    """Machine-readable metadata for one independent rtl_power invocation."""

    sequence: int
    timestamp: str
    command: list[str]
    command_text: str
    duration_seconds: float
    return_code: int
    csv_path: str
    rows: int
    bins: int
    coverage_start_hz: float | None
    coverage_stop_hz: float | None
    requested_start_hz: int
    requested_stop_hz: int
    requested_bin_hz: int
    gain_db: float | str
    baseline_load_50ohm: bool
    single_sweep: bool


def _inspect_csv(path: Path) -> tuple[int, int, float | None, float | None]:
    """Повертає кількість CSV-рядків/бінів і фактичні межі raw output."""
    rows = bins = 0
    starts: list[float] = []
    stops: list[float] = []
    with path.open("r", encoding="ascii", errors="strict", newline="") as stream:
        for columns in csv.reader(stream, skipinitialspace=True):
            if not columns:
                continue
            if len(columns) < 7:
                continue
            try:
                low, high, step = map(float, columns[2:5])
                values = [float(value) for value in columns[6:]]
                if not (math.isfinite(low) and math.isfinite(high) and math.isfinite(step)):
                    continue
                if not values or not all(math.isfinite(value) for value in values):
                    continue
                if low >= high or step <= 0:
                    continue
                expected_bins = round((high - low) / step)
                if (expected_bins <= 0
                        or abs(expected_bins * step - (high - low))
                        > 2 + expected_bins * 0.0051):
                    continue
                # rtl_power повторює останній FFT value у кожному CSV row.
                if (len(values) == expected_bins + 1
                        and values[-1] == values[-2]):
                    values = values[:-1]
                if len(values) != expected_bins:
                    continue
            except (ValueError, TypeError):
                continue
            rows += 1
            bins += len(values)
            starts.append(low)
            stops.append(high)
    return rows, bins, (min(starts) if starts else None), (max(stops) if stops else None)


def build_command(*, start_hz: int, stop_hz: int, bin_hz: int,
                  integration_seconds: int, duration_seconds: int,
                  device: int, gain: float | None, csv_path: Path,
                  single_sweep: bool = False) -> list[str]:
    """Сформувати argv без shell, щоб параметри не інтерпретувалися оболонкою."""
    command = ["rtl_power", "-f", f"{start_hz}:{stop_hz}:{bin_hz}",
               "-i", str(integration_seconds)]
    if single_sweep:
        command.append("-1")
    else:
        command.append("-e")
        command.append(str(duration_seconds))
    command.append("-d")
    command.append(str(device))
    if gain is not None:
        command += ["-g", str(gain)]
    command.append(str(csv_path))
    return command


def run_characterization(args: argparse.Namespace, *, runner=subprocess.run,
                         clock=time.monotonic, now=lambda: datetime.now(UTC)) -> list[SweepRecord]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[SweepRecord] = []
    for sequence in range(1, args.repeat + 1):
        csv_path = output_dir / f"sweep-{sequence:03d}.csv"
        stderr_path = output_dir / f"sweep-{sequence:03d}.stderr.txt"
        command = build_command(
            start_hz=args.start_hz, stop_hz=args.stop_hz, bin_hz=args.bin_hz,
            integration_seconds=args.integration_seconds,
            duration_seconds=args.duration_seconds, device=args.device,
            gain=args.gain, csv_path=csv_path, single_sweep=getattr(args, "single_sweep", False),
        )
        started = clock()
        timestamp = now().isoformat()
        try:
            with stderr_path.open("wb") as diagnostics:
                completed = runner(
                    command, shell=False, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=diagnostics,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                         "TZ": "UTC", "LC_ALL": "C"},
                    timeout=(SINGLE_SWEEP_TIMEOUT_SECONDS if getattr(args, "single_sweep", False) else
                             args.duration_seconds + max(15, args.integration_seconds + 30)),
                    check=False,
                )
            return_code = completed.returncode
        except FileNotFoundError:
            return_code = 127
        except (subprocess.TimeoutExpired, OSError):
            return_code = 124
        duration = clock() - started
        rows = bins = 0
        coverage_start = coverage_stop = None
        if csv_path.exists() and csv_path.stat().st_size <= MAX_CSV_BYTES:
            try:
                rows, bins, coverage_start, coverage_stop = _inspect_csv(csv_path)
            except (OSError, UnicodeError):
                pass
        records.append(SweepRecord(
            sequence, timestamp, command, shlex.join(command), duration, return_code,
            str(csv_path), rows, bins, coverage_start, coverage_stop,
            args.start_hz, args.stop_hz, args.bin_hz,
            "auto" if args.gain is None else args.gain, args.baseline_load_50ohm,
            getattr(args, "single_sweep", False),
        ))
    _write_results(output_dir, records, args)
    return records


def _write_results(output_dir: Path, records: list[SweepRecord], args: argparse.Namespace) -> None:
    payload = {
        "schema_version": "rtl-sdr-characterization.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "baseline_load_50ohm": args.baseline_load_50ohm,
        "correction_applied": False,
        "sweeps": [asdict(record) for record in records],
    }
    (output_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = list(asdict(records[0]).keys()) if records else list(SweepRecord.__annotations__)
    with (output_dir / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(record) for record in records)
    label = "50 Ω LOAD baseline" if args.baseline_load_50ohm else "звичайна characterization"
    mode = ("ONE FULL SWEEP (-1)" if getattr(args, "single_sweep", False)
            else "timed sweep (-e)")
    lines = [f"# RTL-SDR characterization\n", f"Режим: {label}",
             f"Тип запуску: {mode}", "Автоматична корекція: ні",
             f"Кількість sweep: {len(records)}", "", "## Sweep-и", ""]
    for record in records:
        coverage = (f"{record.coverage_start_hz:g}–{record.coverage_stop_hz:g} Hz"
                    if record.coverage_start_hz is not None else "немає валідного покриття")
        lines.append(
            f"- `{record.sequence}`: return code `{record.return_code}`, "
            f"{record.duration_seconds:.3f} с, {record.rows} rows / {record.bins} bins, "
            f"покриття {coverage}; CSV `{record.csv_path}`")
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("має бути додатним цілим")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rf-characterize")
    parser.add_argument("--start-hz", type=_positive_int, required=True, help="Початок діапазону")
    parser.add_argument("--stop-hz", type=_positive_int, required=True, help="Кінець діапазону")
    parser.add_argument("--bin-hz", type=_positive_int, required=True, help="Ширина FFT bin")
    parser.add_argument("--gain", type=float, default=None, help="Gain у dB; без параметра — auto")
    parser.add_argument("--device", type=int, default=0, help="Індекс RTL-SDR")
    parser.add_argument("--integration-seconds", type=_positive_int, default=1)
    parser.add_argument("--duration-seconds", type=_positive_int, default=1)
    parser.add_argument("--single-sweep", action="store_true",
                        help="Один повний sweep через rtl_power -1; --duration не використовується")
    parser.add_argument("--repeat", type=_positive_int, default=1,
                        help="Кількість sweep-ів, максимум 100")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--baseline-load-50ohm", action="store_true",
                        help="Позначити sweep як baseline 50 Ω LOAD; корекція не виконується")
    return parser


def _characterization_succeeded(args: argparse.Namespace,
                                records: Sequence[SweepRecord]) -> bool:
    usable = all(
        record.return_code == 0
        and record.rows > 0
        and record.bins > 0
        and record.coverage_start_hz is not None
        and record.coverage_stop_hz is not None
        and record.coverage_start_hz <= record.requested_start_hz
        and record.coverage_stop_hz >= record.requested_stop_hz
        for record in records
    )
    return bool(records) and len(records) == args.repeat and usable


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (24_000_000 <= args.start_hz < args.stop_hz <= 1_766_000_000):
        raise SystemExit("Діапазон має бути в межах 24–1766 MHz")
    if not 10_000 <= args.bin_hz <= 1_000_000:
        raise SystemExit("--bin-hz має бути в межах 10000–1000000")
    if args.single_sweep:
        if args.repeat != 1:
            raise SystemExit("--single-sweep вимагає --repeat 1")
    elif not 1 <= args.integration_seconds <= args.duration_seconds <= 1800:
        raise SystemExit("Потрібно 1 ≤ --integration-seconds ≤ --duration-seconds ≤ 1800")
    if not args.single_sweep and args.duration_seconds % args.integration_seconds:
        raise SystemExit("--duration-seconds має бути кратним --integration-seconds")
    if args.repeat > 100:
        raise SystemExit("--repeat не може перевищувати 100")
    if not 0 <= args.device <= 255 or args.gain is not None and not 0 <= args.gain <= 50:
        raise SystemExit("Некоректні --device або --gain")
    records = run_characterization(args)
    for record in records:
        print(f"sweep {record.sequence}: rc={record.return_code}, {record.duration_seconds:.3f}s, "
              f"CSV={record.csv_path}")
    return 0 if _characterization_succeeded(args, records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
