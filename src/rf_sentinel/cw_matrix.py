"""Resumable LibreVNA -> RTL-SDR CW matrix characterization runner."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import itertools
import json
import os
from pathlib import Path
import re
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
    analyze_spectrum,
    build_point_command,
    generator_commands,
    generator_shutdown_command,
)


_GAIN_RE = re.compile(r"Tuner gain set to\s+([-+0-9.]+)\s+dB", re.IGNORECASE)
_FFT_BIN_RE = re.compile(r"FFT bin size:\s*([-+0-9.]+)\s*Hz", re.IGNORECASE)


@dataclass(frozen=True)
class MatrixPoint:
    sequence: int
    requested_frequency_hz: int
    requested_gain_db: float
    requested_bin_hz: int

    @property
    def key(self) -> str:
        return (f"f{self.requested_frequency_hz}-g{self.requested_gain_db:g}"
                f"-b{self.requested_bin_hz}")


@dataclass(frozen=True)
class MatrixRecord:
    point_key: str
    sequence: int
    physical_configuration: str
    cable: str
    libre_vna_port: int
    requested_frequency_hz: int
    requested_generator_level_dbm: float
    requested_gain_db: float
    actual_tuner_gain_db: float | None
    requested_bin_hz: int
    effective_fft_bin_hz: float | None
    carrier_frequency_hz: float | None
    carrier_offset_hz: float | None
    carrier_level_db: float | None
    local_baseline_db: float | None
    carrier_delta_db: float | None
    status: str
    duration_seconds: float
    return_code: int
    warnings: list[str]
    timestamp: str
    raw_csv_path: str
    stderr_path: str
    command: list[str]
    command_text: str
    scpi_commands: list[str]


def expand_matrix(frequencies_hz: Sequence[int], gains_db: Sequence[float],
                  bins_hz: Sequence[int]) -> list[MatrixPoint]:
    return [
        MatrixPoint(sequence, frequency, gain, bin_hz)
        for sequence, (bin_hz, gain, frequency) in enumerate(
            itertools.product(bins_hz, gains_db, frequencies_hz), 1)
    ]


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


def _gains(value: str) -> tuple[float, ...]:
    result = _number_list(value, parser=float, label="gain")
    if any(not 0 <= item <= 50 for item in result):
        raise argparse.ArgumentTypeError("gain має бути в межах 0–50 dB")
    return result


def _bins(value: str) -> tuple[int, ...]:
    result = _number_list(value, parser=int, label="bin sizes")
    if any(not 10_000 <= item <= 2_800_000 for item in result):
        raise argparse.ArgumentTypeError("bin size має бути в межах 10000–2800000 Hz")
    return result


def _diagnostics(stderr_text: str) -> tuple[float | None, float | None, list[str]]:
    gain_match = _GAIN_RE.search(stderr_text)
    bin_match = _FFT_BIN_RE.search(stderr_text)
    warnings = [
        line.strip() for line in stderr_text.splitlines()
        if ("pll not locked" in line.lower() or "warning" in line.lower()
            or "error" in line.lower() or "no e4000" in line.lower())
    ]
    return (
        float(gain_match.group(1)) if gain_match else None,
        float(bin_match.group(1)) if bin_match else None,
        warnings,
    )


def _configuration(args: argparse.Namespace) -> dict:
    return {
        "physical_configuration": args.physical_configuration,
        "cable": args.cable,
        "libre_vna_port": args.libre_vna_port,
        "vna_host": args.vna_host,
        "vna_port": args.vna_port,
        "requested_generator_level_dbm": args.generator_level_dbm,
        "frequencies_hz": list(args.frequencies_hz),
        "gains_db": list(args.gains_db),
        "bins_hz": list(args.bins_hz),
        "settle_seconds": args.settle_seconds,
        "window_hz": args.window_hz,
        "integration_seconds": args.integration_seconds,
        "expected_frequency_tolerance_hz": args.expected_frequency_tolerance_hz,
        "min_carrier_delta_db": args.min_carrier_delta_db,
        "device": args.device,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _persist(output_dir: Path, configuration: dict, plan: Sequence[MatrixPoint],
             records: Sequence[MatrixRecord], *, created_at: str,
             preflight: dict | None) -> None:
    counts = {
        "planned": len(plan),
        "completed": len(records),
        "valid": sum(record.status == "valid" for record in records),
        "invalid": sum(record.status == "invalid" for record in records),
        "failed": sum(record.status == "failed" for record in records),
    }
    payload = {
        "schema_version": "rtl-sdr-cw-matrix.v1",
        "created_at": created_at,
        "updated_at": datetime.now(UTC).isoformat(),
        "correction_applied": False,
        "interpolation_applied": False,
        "normalization_applied": False,
        "configuration": configuration,
        "preflight": preflight,
        "counts": counts,
        "points": [asdict(record) for record in records],
    }
    _atomic_write_text(
        output_dir / "results.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )

    fields = list(MatrixRecord.__annotations__)
    csv_path = output_dir / "results.csv"
    csv_temporary = csv_path.with_name(csv_path.name + ".tmp")
    with csv_temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(record) for record in records)
    csv_temporary.replace(csv_path)

    lines = [
        "# LibreVNA → RTL-SDR CW matrix",
        "",
        f"Configuration: {configuration['physical_configuration']}",
        "Correction: ні",
        "Normalization: ні",
        f"Planned: {counts['planned']}",
        f"Completed: {counts['completed']}",
        f"Valid: {counts['valid']}",
        f"Invalid: {counts['invalid']}",
        f"Failed: {counts['failed']}",
        "",
        "## Points",
        "",
    ]
    for record in records:
        carrier = (f"{record.carrier_frequency_hz:.2f} Hz / {record.carrier_level_db:.3f} dB"
                   if record.carrier_frequency_hz is not None else "немає valid carrier")
        lines.append(
            f"- {record.point_key}: {carrier}; delta={record.carrier_delta_db}; "
            f"status={record.status}; rc={record.return_code}"
        )
    _atomic_write_text(output_dir / "summary.md", "\n".join(lines) + "\n")


def _load_resume(output_dir: Path, configuration: dict) -> tuple[str, dict | None, list[MatrixRecord]]:
    path = output_dir / "results.json"
    if not path.exists():
        return datetime.now(UTC).isoformat(), None, []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("configuration") != configuration:
        raise ValueError("Resume configuration не збігається з existing dataset")
    records = [MatrixRecord(**record) for record in payload.get("points", [])]
    return payload["created_at"], payload.get("preflight"), records


def _run_preflight(device: int, *, runner=subprocess.run) -> dict:
    command = ["rtl_test", "-d", str(device), "-t"]
    started = time.monotonic()
    completed = runner(
        command, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": "UTC", "LC_ALL": "C"},
        timeout=10, check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    detected = f"Using device {device}:" in output
    if completed.returncode != 0 or not detected:
        raise RuntimeError(f"RTL-SDR preflight failed: rc={completed.returncode}")
    _, _, warnings = _diagnostics(output)
    return {
        "command": command,
        "return_code": completed.returncode,
        "duration_seconds": time.monotonic() - started,
        "device_detected": detected,
        "warnings": warnings,
    }


def run_matrix(args: argparse.Namespace, *, scpi=None, runner=subprocess.run,
               preflight_runner=subprocess.run, sleeper=time.sleep,
               clock=time.monotonic, now=lambda: datetime.now(UTC)) -> list[MatrixRecord]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    configuration = _configuration(args)
    plan = expand_matrix(args.frequencies_hz, args.gains_db, args.bins_hz)
    if len(plan) > 10_000:
        raise ValueError("Matrix обмежена 10000 points")
    if (output_dir / "results.json").exists() and not args.resume:
        raise ValueError("Output dataset існує; використайте --resume")
    created_at, preflight, records = _load_resume(output_dir, configuration)
    completed_keys = {record.point_key for record in records}
    pending = [point for point in plan if point.key not in completed_keys]
    if not pending:
        return records

    preflight = _run_preflight(args.device, runner=preflight_runner)
    _persist(output_dir, configuration, plan, records, created_at=created_at, preflight=preflight)
    own_scpi = scpi is None
    if own_scpi:
        scpi = LibreVNAScpi(args.vna_host, args.vna_port, args.scpi_timeout)
    try:
        for point in pending:
            stem = (f"point-{point.sequence:03d}-{point.requested_frequency_hz // 1_000_000}MHz"
                    f"-g{point.requested_gain_db:g}-b{point.requested_bin_hz}")
            raw_csv_path = output_dir / f"{stem}.csv"
            stderr_path = output_dir / f"{stem}.stderr.txt"
            # A pending point may have left deterministic artifacts after an
            # interrupted attempt.  Remove only those artifacts; completed
            # points never enter this pending loop.
            for artifact in (raw_csv_path, stderr_path):
                if artifact.exists():
                    artifact.unlink()
            command = build_point_command(
                frequency_hz=point.requested_frequency_hz,
                window_hz=args.window_hz,
                bin_hz=point.requested_bin_hz,
                integration_seconds=args.integration_seconds,
                device=args.device,
                gain=point.requested_gain_db,
                csv_path=raw_csv_path,
            )
            scpi_commands = list(generator_commands(
                point.requested_frequency_hz,
                args.generator_level_dbm,
                port=args.libre_vna_port,
            ))
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
            if failure_warning:
                warnings.append(failure_warning)
            analysis = None
            if (return_code == 0 and raw_csv_path.exists()
                    and raw_csv_path.stat().st_size <= MAX_CSV_BYTES):
                try:
                    analysis = analyze_spectrum(
                        raw_csv_path,
                        expected_frequency_hz=point.requested_frequency_hz,
                        expected_frequency_tolerance_hz=args.expected_frequency_tolerance_hz,
                        min_carrier_delta_db=args.min_carrier_delta_db,
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    warnings.append(f"analysis: {type(error).__name__}: {error}")
            if return_code != 0 or analysis is None:
                status = "failed"
            else:
                status = "valid" if analysis.carrier_valid else "invalid"
            carrier = analysis.local_peak if analysis is not None and analysis.carrier_valid else None
            record = MatrixRecord(
                point_key=point.key,
                sequence=point.sequence,
                physical_configuration=args.physical_configuration,
                cable=args.cable,
                libre_vna_port=args.libre_vna_port,
                requested_frequency_hz=point.requested_frequency_hz,
                requested_generator_level_dbm=args.generator_level_dbm,
                requested_gain_db=point.requested_gain_db,
                actual_tuner_gain_db=actual_gain,
                requested_bin_hz=point.requested_bin_hz,
                effective_fft_bin_hz=effective_bin,
                carrier_frequency_hz=None if carrier is None else carrier[0],
                carrier_offset_hz=None if carrier is None else carrier[0] - point.requested_frequency_hz,
                carrier_level_db=None if carrier is None else carrier[1],
                local_baseline_db=None if analysis is None else analysis.local_baseline_db,
                carrier_delta_db=None if analysis is None else analysis.carrier_delta_db,
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
                f"point {point.sequence}/{len(plan)}: {point.requested_frequency_hz / 1e6:g} MHz, "
                f"gain={point.requested_gain_db:g}, bin={point.requested_bin_hz}, "
                f"status={status}, delta={record.carrier_delta_db}, rc={return_code}",
                flush=True,
            )
    finally:
        try:
            scpi.send(generator_shutdown_command())
        finally:
            if own_scpi:
                scpi.close()
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rf-cw-matrix")
    parser.add_argument("--physical-configuration", required=True)
    parser.add_argument("--cable", required=True)
    parser.add_argument("--libre-vna-port", type=int, required=True)
    parser.add_argument("--vna-host", default="127.0.0.1")
    parser.add_argument("--vna-port", type=int, default=DEFAULT_SCPI_PORT)
    parser.add_argument("--scpi-timeout", type=float, default=5.0)
    parser.add_argument("--generator-level-dbm", type=float, required=True)
    parser.add_argument("--frequencies-hz", type=_frequencies, required=True)
    parser.add_argument("--gains-db", type=_gains, required=True)
    parser.add_argument("--bins-hz", type=_bins, required=True)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--window-hz", type=int, default=2_000_000)
    parser.add_argument("--integration-seconds", type=int, default=1)
    parser.add_argument("--expected-frequency-tolerance-hz", type=int,
                        default=DEFAULT_EXPECTED_FREQUENCY_TOLERANCE_HZ)
    parser.add_argument("--min-carrier-delta-db", type=float,
                        default=DEFAULT_MIN_CARRIER_DELTA_DB)
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
    if args.window_hz <= 0 or args.expected_frequency_tolerance_hz <= 0:
        raise SystemExit("Window і expected-frequency tolerance мають бути додатними")
    if args.min_carrier_delta_db < 0:
        raise SystemExit("Minimum carrier delta не може бути від'ємним")
    records = run_matrix(args)
    return 0 if all(record.status == "valid" for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
