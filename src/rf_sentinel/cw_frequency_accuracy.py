"""Bounded, resumable CW frequency-accuracy and tuner-artifact experiment."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import itertools
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Sequence

from rf_sentinel.cw_characterization import (
    DEFAULT_EXPECTED_FREQUENCY_TOLERANCE_HZ,
    DEFAULT_MIN_CARRIER_DELTA_DB,
    DEFAULT_SCPI_PORT,
    MAX_CSV_BYTES,
    POINT_TIMEOUT_SECONDS,
    LibreVNAScpi,
    _rtl_power_points,
    analyze_spectrum,
    build_point_command,
    generator_commands,
    generator_shutdown_command,
)
from rf_sentinel.cw_matrix import _diagnostics, _run_preflight


DEFAULT_FREQUENCIES_HZ = (230_000_000, 500_000_000, 1_000_000_000, 1_500_000_000)
DEFAULT_CENTER_OFFSETS_HZ = (-500_000, -250_000, 0, 250_000, 500_000)
DEFAULT_TUNING_MODES = ("normal", "offset")
MAX_POINTS = 500
NON_CW_PEAK_COUNT = 5


@dataclass(frozen=True)
class FrequencyAccuracyPoint:
    sequence: int
    requested_cw_frequency_hz: int
    center_offset_hz: int
    repeat: int
    tuning_mode: str

    @property
    def tuner_center_hz(self) -> int:
        return self.requested_cw_frequency_hz + self.center_offset_hz

    @property
    def key(self) -> str:
        return (f"f{self.requested_cw_frequency_hz}-c{self.center_offset_hz:+d}"
                f"-r{self.repeat}-{self.tuning_mode}")


@dataclass(frozen=True)
class FrequencyAccuracyRecord:
    point_key: str
    sequence: int
    requested_cw_frequency_hz: int
    tuner_center_hz: int
    center_offset_hz: int
    repeat: int
    tuning_mode: str
    offset_tuning_active: bool | None
    libre_vna_port: int
    requested_generator_level_dbm: float
    requested_gain_db: float
    actual_tuner_gain_db: float | None
    requested_bin_hz: int
    effective_fft_bin_hz: float | None
    measured_cw_peak_hz: float | None
    cw_offset_hz: float | None
    cw_offset_ppm: float | None
    cw_offset_fft_bins: float | None
    carrier_level_db: float | None
    local_baseline_db: float | None
    carrier_delta_db: float | None
    strongest_non_cw_peaks: list[dict[str, float]]
    status: str
    duration_seconds: float
    return_code: int | None
    warnings: list[str]
    timestamp: str
    raw_csv_path: str
    stderr_path: str
    command: list[str]
    command_text: str
    scpi_commands: list[str]


def expand_frequency_accuracy_matrix(
        frequencies_hz: Sequence[int], center_offsets_hz: Sequence[int],
        repeats: int, tuning_modes: Sequence[str]) -> list[FrequencyAccuracyPoint]:
    return [
        FrequencyAccuracyPoint(sequence, frequency_hz, center_offset_hz, repeat, tuning_mode)
        for sequence, (frequency_hz, center_offset_hz, repeat, tuning_mode) in enumerate(
            itertools.product(frequencies_hz, center_offsets_hz,
                              range(1, repeats + 1), tuning_modes), 1)
    ]


def strongest_non_cw_peaks(
        path: Path, *, cw_frequency_hz: int, tuner_center_hz: int,
        exclusion_hz: int, minimum_separation_hz: int,
        limit: int = NON_CW_PEAK_COUNT) -> list[dict[str, float]]:
    """Return separated local maxima outside the CW exclusion band."""
    points = _rtl_power_points(path)
    candidates: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        frequency_hz, level_db = point
        if abs(frequency_hz - cw_frequency_hz) <= exclusion_hz:
            continue
        left = points[index - 1][1] if index else -math.inf
        right = points[index + 1][1] if index + 1 < len(points) else -math.inf
        if level_db >= left and level_db >= right:
            candidates.append(point)

    selected: list[tuple[float, float]] = []
    for point in sorted(candidates, key=lambda item: item[1], reverse=True):
        if all(abs(point[0] - prior[0]) >= minimum_separation_hz for prior in selected):
            selected.append(point)
        if len(selected) == limit:
            break
    return [
        {
            "frequency_hz": frequency_hz,
            "level_db": level_db,
            "offset_from_tuner_center_hz": frequency_hz - tuner_center_hz,
            "offset_from_cw_hz": frequency_hz - cw_frequency_hz,
        }
        for frequency_hz, level_db in selected
    ]


def offset_tuning_active(tuning_mode: str, stderr_text: str) -> bool | None:
    """Interpret rtl_power's explicit offset-tuning capability diagnostic."""
    if tuning_mode == "normal":
        return False
    lowered = stderr_text.lower()
    if "failed to set offset tuning" in lowered:
        return False
    if "offset tuning mode enabled" in lowered:
        return True
    return None


def _number_list(value: str, *, parser, label: str):
    try:
        result = tuple(parser(part.strip()) for part in value.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"{label} мають бути числами через кому") from None
    if not result:
        raise argparse.ArgumentTypeError(f"потрібен непорожній список {label}")
    return result


def _frequencies(value: str) -> tuple[int, ...]:
    result = _number_list(value, parser=int, label="частоти")
    if any(not 24_000_000 <= item <= 1_766_000_000 for item in result):
        raise argparse.ArgumentTypeError("частоти мають бути в межах 24–1766 MHz")
    return result


def _offsets(value: str) -> tuple[int, ...]:
    return _number_list(value, parser=int, label="center offsets")


def _tuning_modes(value: str) -> tuple[str, ...]:
    result = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    if not result or any(mode not in DEFAULT_TUNING_MODES for mode in result):
        raise argparse.ArgumentTypeError("tuning modes: normal,offset")
    return result


def _configuration(args: argparse.Namespace) -> dict:
    return {
        "vna_host": args.vna_host,
        "vna_port": args.vna_port,
        "libre_vna_port": args.libre_vna_port,
        "requested_generator_level_dbm": args.generator_level_dbm,
        "frequencies_hz": list(args.frequencies_hz),
        "center_offsets_hz": list(args.center_offsets_hz),
        "repeats": args.repeats,
        "tuning_modes": list(args.tuning_modes),
        "settle_seconds": args.settle_seconds,
        "window_hz": args.window_hz,
        "requested_bin_hz": args.bin_hz,
        "integration_seconds": args.integration_seconds,
        "gain_db": args.gain,
        "expected_frequency_tolerance_hz": args.expected_frequency_tolerance_hz,
        "min_carrier_delta_db": args.min_carrier_delta_db,
        "non_cw_exclusion_hz": args.non_cw_exclusion_hz,
        "non_cw_peak_separation_hz": args.non_cw_peak_separation_hz,
        "fft_window": args.fft_window,
        "device": args.device,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _persist(output_dir: Path, configuration: dict,
             plan: Sequence[FrequencyAccuracyPoint],
             records: Sequence[FrequencyAccuracyRecord], *,
             created_at: str, preflight: dict | None) -> None:
    counts = {
        "planned": len(plan),
        "completed": len(records),
        "valid": sum(record.status == "valid" for record in records),
        "invalid": sum(record.status == "invalid" for record in records),
        "failed": sum(record.status == "failed" for record in records),
        "unsupported": sum(record.status == "unsupported" for record in records),
        "unverified": sum(record.status == "unverified" for record in records),
    }
    payload = {
        "schema_version": "rtl-sdr-cw-frequency-accuracy.v1",
        "created_at": created_at,
        "updated_at": datetime.now(UTC).isoformat(),
        "frequency_correction_applied": False,
        "normalization_applied": False,
        "configuration": configuration,
        "preflight": preflight,
        "counts": counts,
        "points": [asdict(record) for record in records],
    }
    _atomic_write_text(output_dir / "results.json",
                       json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    fields = list(FrequencyAccuracyRecord.__annotations__)
    temporary = output_dir / "results.csv.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = asdict(record)
            row["strongest_non_cw_peaks"] = json.dumps(
                row["strongest_non_cw_peaks"], separators=(",", ":"))
            row["warnings"] = json.dumps(row["warnings"], ensure_ascii=False)
            writer.writerow(row)
    temporary.replace(output_dir / "results.csv")

    lines = [
        "# RTL-SDR CW frequency accuracy / tuner artifacts",
        "",
        "Frequency correction: ні",
        f"Planned: {counts['planned']}",
        f"Completed: {counts['completed']}",
        f"Valid: {counts['valid']}",
        f"Invalid: {counts['invalid']}",
        f"Failed: {counts['failed']}",
        f"Unsupported: {counts['unsupported']}",
        f"Unverified: {counts['unverified']}",
        "",
    ]
    _atomic_write_text(output_dir / "summary.md", "\n".join(lines))


def _load_resume(output_dir: Path, configuration: dict) -> tuple[str, dict | None, list[FrequencyAccuracyRecord]]:
    path = output_dir / "results.json"
    if not path.exists():
        return datetime.now(UTC).isoformat(), None, []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("configuration") != configuration:
        raise ValueError("Resume configuration не збігається з existing dataset")
    records = [FrequencyAccuracyRecord(**record) for record in payload.get("points", [])]
    return payload["created_at"], payload.get("preflight"), records


def run_frequency_accuracy(
        args: argparse.Namespace, *, scpi=None, runner=subprocess.run,
        preflight_runner=subprocess.run, sleeper=time.sleep,
        clock=time.monotonic, now=lambda: datetime.now(UTC)) -> list[FrequencyAccuracyRecord]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    configuration = _configuration(args)
    plan = expand_frequency_accuracy_matrix(
        args.frequencies_hz, args.center_offsets_hz, args.repeats, args.tuning_modes)
    if not plan or len(plan) > MAX_POINTS:
        raise ValueError(f"Frequency-accuracy matrix має містити 1–{MAX_POINTS} points")
    if (output_dir / "results.json").exists() and not args.resume:
        raise ValueError("Output dataset існує; використайте --resume")
    created_at, preflight, records = _load_resume(output_dir, configuration)
    completed_keys = {record.point_key for record in records}
    pending = [point for point in plan if point.key not in completed_keys]
    if not pending:
        return records

    preflight = _run_preflight(args.device, runner=preflight_runner)
    _persist(output_dir, configuration, plan, records,
             created_at=created_at, preflight=preflight)
    own_scpi = scpi is None
    if own_scpi:
        scpi = LibreVNAScpi(args.vna_host, args.vna_port, args.scpi_timeout)
    offset_supported: bool | None = next(
        (record.offset_tuning_active for record in records
         if record.tuning_mode == "offset" and record.offset_tuning_active is not None),
        None,
    )
    try:
        for point in pending:
            stem = (f"point-{point.sequence:03d}-{point.requested_cw_frequency_hz // 1_000_000}MHz"
                    f"-c{point.center_offset_hz:+d}-r{point.repeat}-{point.tuning_mode}")
            raw_csv_path = output_dir / f"{stem}.csv"
            stderr_path = output_dir / f"{stem}.stderr.txt"
            # A pending point may have left deterministic artifacts after an
            # interrupted attempt.  Remove only those artifacts; completed
            # points never enter this pending loop.
            for artifact in (raw_csv_path, stderr_path):
                if artifact.exists():
                    artifact.unlink()
            command = build_point_command(
                frequency_hz=point.requested_cw_frequency_hz,
                capture_center_hz=point.tuner_center_hz,
                window_hz=args.window_hz,
                bin_hz=args.bin_hz,
                integration_seconds=args.integration_seconds,
                device=args.device,
                gain=args.gain,
                offset_tuning=point.tuning_mode == "offset",
                fft_window=args.fft_window,
                csv_path=raw_csv_path,
            )
            scpi_commands = list(generator_commands(
                point.requested_cw_frequency_hz,
                args.generator_level_dbm,
                port=args.libre_vna_port,
            ))
            if point.tuning_mode == "offset" and offset_supported is False:
                record = FrequencyAccuracyRecord(
                    point_key=point.key,
                    sequence=point.sequence,
                    requested_cw_frequency_hz=point.requested_cw_frequency_hz,
                    tuner_center_hz=point.tuner_center_hz,
                    center_offset_hz=point.center_offset_hz,
                    repeat=point.repeat,
                    tuning_mode=point.tuning_mode,
                    offset_tuning_active=False,
                    libre_vna_port=args.libre_vna_port,
                    requested_generator_level_dbm=args.generator_level_dbm,
                    requested_gain_db=args.gain,
                    actual_tuner_gain_db=None,
                    requested_bin_hz=args.bin_hz,
                    effective_fft_bin_hz=None,
                    measured_cw_peak_hz=None,
                    cw_offset_hz=None,
                    cw_offset_ppm=None,
                    cw_offset_fft_bins=None,
                    carrier_level_db=None,
                    local_baseline_db=None,
                    carrier_delta_db=None,
                    strongest_non_cw_peaks=[],
                    status="unsupported",
                    duration_seconds=0.0,
                    return_code=None,
                    warnings=["offset tuning unsupported; skipped after capability failure"],
                    timestamp=now().isoformat(),
                    raw_csv_path=str(raw_csv_path),
                    stderr_path=str(stderr_path),
                    command=command,
                    command_text=shlex.join(command),
                    scpi_commands=[],
                )
                records.append(record)
                _persist(output_dir, configuration, plan, records,
                         created_at=created_at, preflight=preflight)
                print(
                    f"point {point.sequence}/{len(plan)}: CW={point.requested_cw_frequency_hz / 1e6:g} MHz, "
                    f"center_offset={point.center_offset_hz:+d} Hz, mode=offset, "
                    "status=unsupported (skipped)", flush=True,
                )
                continue
            started = clock()
            timestamp = now().isoformat()
            return_code = 0
            failure_warning: str | None = None
            try:
                for scpi_command in scpi_commands:
                    scpi.send(scpi_command)
                sleeper(args.settle_seconds)
                with stderr_path.open("wb") as diagnostics:
                    completed = runner(
                        command, shell=False, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=diagnostics,
                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                             "TZ": "UTC", "LC_ALL": "C"},
                        timeout=POINT_TIMEOUT_SECONDS, check=False,
                    )
                return_code = completed.returncode
            except (OSError, subprocess.TimeoutExpired) as error:
                return_code = 124
                failure_warning = f"{type(error).__name__}: {error}"

            stderr_text = (stderr_path.read_text(encoding="utf-8", errors="replace")
                           if stderr_path.exists() else "")
            actual_gain, effective_bin, warnings = _diagnostics(stderr_text)
            active = offset_tuning_active(point.tuning_mode, stderr_text)
            if point.tuning_mode == "offset" and active is not None:
                offset_supported = active
            if failure_warning:
                warnings.append(failure_warning)
            analysis = None
            non_cw_peaks: list[dict[str, float]] = []
            if (return_code == 0 and raw_csv_path.exists()
                    and raw_csv_path.stat().st_size <= MAX_CSV_BYTES):
                try:
                    analysis = analyze_spectrum(
                        raw_csv_path,
                        expected_frequency_hz=point.requested_cw_frequency_hz,
                        expected_frequency_tolerance_hz=args.expected_frequency_tolerance_hz,
                        min_carrier_delta_db=args.min_carrier_delta_db,
                    )
                    non_cw_peaks = strongest_non_cw_peaks(
                        raw_csv_path,
                        cw_frequency_hz=point.requested_cw_frequency_hz,
                        tuner_center_hz=point.tuner_center_hz,
                        exclusion_hz=args.non_cw_exclusion_hz,
                        minimum_separation_hz=args.non_cw_peak_separation_hz,
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    warnings.append(f"analysis: {type(error).__name__}: {error}")
            if point.tuning_mode == "offset" and active is False:
                status = "unsupported"
            elif return_code != 0 or analysis is None:
                status = "failed"
            elif point.tuning_mode == "offset" and active is None:
                status = "unverified"
            else:
                status = "valid" if analysis.carrier_valid else "invalid"
            carrier = analysis.local_peak if analysis is not None and analysis.carrier_valid else None
            carrier_offset_hz = None if carrier is None else carrier[0] - point.requested_cw_frequency_hz
            record = FrequencyAccuracyRecord(
                point_key=point.key,
                sequence=point.sequence,
                requested_cw_frequency_hz=point.requested_cw_frequency_hz,
                tuner_center_hz=point.tuner_center_hz,
                center_offset_hz=point.center_offset_hz,
                repeat=point.repeat,
                tuning_mode=point.tuning_mode,
                offset_tuning_active=active,
                libre_vna_port=args.libre_vna_port,
                requested_generator_level_dbm=args.generator_level_dbm,
                requested_gain_db=args.gain,
                actual_tuner_gain_db=actual_gain,
                requested_bin_hz=args.bin_hz,
                effective_fft_bin_hz=effective_bin,
                measured_cw_peak_hz=None if carrier is None else carrier[0],
                cw_offset_hz=carrier_offset_hz,
                cw_offset_ppm=(None if carrier_offset_hz is None else
                               carrier_offset_hz / point.requested_cw_frequency_hz * 1_000_000),
                cw_offset_fft_bins=(None if carrier_offset_hz is None or not effective_bin else
                                    carrier_offset_hz / effective_bin),
                carrier_level_db=None if carrier is None else carrier[1],
                local_baseline_db=None if analysis is None else analysis.local_baseline_db,
                carrier_delta_db=None if analysis is None else analysis.carrier_delta_db,
                strongest_non_cw_peaks=non_cw_peaks,
                status=status,
                duration_seconds=clock() - started,
                return_code=return_code,
                warnings=warnings,
                timestamp=timestamp,
                raw_csv_path=str(raw_csv_path),
                stderr_path=str(stderr_path),
                command=command,
                command_text=shlex.join(command),
                scpi_commands=scpi_commands,
            )
            records.append(record)
            _persist(output_dir, configuration, plan, records,
                     created_at=created_at, preflight=preflight)
            print(
                f"point {point.sequence}/{len(plan)}: CW={point.requested_cw_frequency_hz / 1e6:g} MHz, "
                f"center_offset={point.center_offset_hz:+d} Hz, mode={point.tuning_mode}, "
                f"repeat={point.repeat}, status={status}, offset={carrier_offset_hz}, "
                f"rc={return_code}", flush=True,
            )
    finally:
        try:
            scpi.send(generator_shutdown_command())
        finally:
            if own_scpi:
                scpi.close()
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rf-cw-frequency-accuracy")
    parser.add_argument("--libre-vna-port", type=int, required=True)
    parser.add_argument("--vna-host", default="127.0.0.1")
    parser.add_argument("--vna-port", type=int, default=DEFAULT_SCPI_PORT)
    parser.add_argument("--scpi-timeout", type=float, default=5.0)
    parser.add_argument("--generator-level-dbm", type=float, required=True)
    parser.add_argument("--frequencies-hz", type=_frequencies,
                        default=DEFAULT_FREQUENCIES_HZ)
    parser.add_argument("--center-offsets-hz", type=_offsets,
                        default=DEFAULT_CENTER_OFFSETS_HZ)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tuning-modes", type=_tuning_modes,
                        default=DEFAULT_TUNING_MODES)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--window-hz", type=int, default=2_000_000)
    parser.add_argument("--bin-hz", type=int, default=500)
    parser.add_argument("--integration-seconds", type=int, default=1)
    parser.add_argument("--gain", type=float, default=0.0)
    parser.add_argument("--expected-frequency-tolerance-hz", type=int,
                        default=DEFAULT_EXPECTED_FREQUENCY_TOLERANCE_HZ)
    parser.add_argument("--min-carrier-delta-db", type=float,
                        default=DEFAULT_MIN_CARRIER_DELTA_DB)
    parser.add_argument("--non-cw-exclusion-hz", type=int, default=20_000)
    parser.add_argument("--non-cw-peak-separation-hz", type=int, default=20_000)
    parser.add_argument("--fft-window", default="blackman-harris")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.libre_vna_port <= 2:
        raise SystemExit("LibreVNA RF port має бути 1 або 2")
    if not -42 <= args.generator_level_dbm <= -10:
        raise SystemExit("LibreVNA generator level має бути в межах -42…-10 dBm")
    if not (1 <= args.vna_port <= 65535 and 0 <= args.device <= 255):
        raise SystemExit("Некоректні VNA port або RTL device")
    if not 0 <= args.settle_seconds <= 10 or args.integration_seconds <= 0:
        raise SystemExit("Некоректні settle/integration settings")
    if not 1 <= args.bin_hz <= 2_800_000:
        raise SystemExit("Requested FFT bin має бути в межах 1–2800000 Hz")
    if args.window_hz <= 0 or args.window_hz % 2:
        raise SystemExit("Window має бути додатним парним числом Hz")
    if args.repeats <= 0 or args.expected_frequency_tolerance_hz <= 0:
        raise SystemExit("Repeats і expected-frequency tolerance мають бути додатними")
    if args.min_carrier_delta_db < 0 or args.non_cw_exclusion_hz <= 0:
        raise SystemExit("Некоректні carrier/non-CW thresholds")
    if args.non_cw_peak_separation_hz <= 0:
        raise SystemExit("Non-CW peak separation має бути додатним")
    if not 0 <= args.gain <= 50:
        raise SystemExit("Gain має бути в межах 0–50 dB")
    for frequency_hz in args.frequencies_hz:
        for offset_hz in args.center_offsets_hz:
            center_hz = frequency_hz + offset_hz
            half_window = args.window_hz // 2
            if not 24_000_000 <= center_hz - half_window < center_hz + half_window <= 1_766_000_000:
                raise SystemExit("Capture window виходить за межі RTL-SDR tuning range")
    records = run_frequency_accuracy(args)
    return 0 if all(record.status in {"valid", "unsupported"} for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
