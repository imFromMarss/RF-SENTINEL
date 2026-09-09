"""Короткий послідовний benchmark RX; результати залишаються в runtime."""

from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import time

from rf_sentinel.acquisition import SweepProfile
from rf_sentinel.errors import ScanError
from rf_sentinel.rtl_power import RTLPowerScanner, sweep_from_result


def main():
    directory = Path("runtime/benchmarks") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True)
    results = []
    for width in (250_000, 500_000, 1_000_000):
        trial = directory / str(width)
        trial.mkdir()
        raw = trial / "spectrum.csv"
        profile = SweepProfile(bin_hz=width)
        started = time.monotonic()
        row = {"profile": asdict(profile), "started_at": datetime.now(UTC).isoformat()}
        try:
            result = RTLPowerScanner().scan(profile, raw)
            sweep = sweep_from_result(result)
            row.update(status="success", returncode=0, bins=len(sweep.powers),
                       actual_bin_width_hz=sweep.bin_width_hz, actual_start_hz=sweep.start_hz,
                       actual_stop_hz=sweep.stop_hz, duration_seconds=sweep.duration_seconds,
                       receiver=sweep.receiver, tuner=sweep.tuner)
        except ScanError as error:
            row.update(status="failed", reason=error.reason, returncode=error.returncode,
                       duration_seconds=time.monotonic() - started)
        row["output_bytes"] = raw.stat().st_size if raw.exists() else 0
        results.append(row)
        (directory / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(row), flush=True)
    print(directory)
    return 0 if all(row["status"] == "success" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
