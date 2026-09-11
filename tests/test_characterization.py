import argparse
from datetime import UTC, datetime
import json

from rf_sentinel.characterization import (_inspect_csv, build_command, run_characterization)


CSV = "2026-09-11, 10:00:00, 24000000, 26000000, 1000000.00, 1, -50, -49\n"


def _args(tmp_path, **overrides):
    values = dict(start_hz=24_000_000, stop_hz=26_000_000, bin_hz=1_000_000,
                  integration_seconds=1, duration_seconds=1, device=0, gain=12.5,
                  repeat=1, output_dir=tmp_path, baseline_load_50ohm=True)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_build_command_is_explicit_and_shell_safe(tmp_path):
    assert build_command(start_hz=24_000_000, stop_hz=26_000_000, bin_hz=1_000_000,
                         integration_seconds=1, duration_seconds=1, device=2, gain=None,
                         csv_path=tmp_path / "raw.csv") == [
        "rtl_power", "-f", "24000000:26000000:1000000", "-i", "1", "-e", "1",
        "-d", "2", str(tmp_path / "raw.csv")]


def test_single_sweep_command_uses_one_shot_without_duration(tmp_path):
    command = build_command(
        start_hz=24_000_000, stop_hz=1_766_000_000, bin_hz=500_000,
        integration_seconds=1, duration_seconds=1800, device=0, gain=0,
        csv_path=tmp_path / "raw.csv", single_sweep=True)
    assert command == [
        "rtl_power", "-f", "24000000:1766000000:500000", "-i", "1",
        "-1", "-d", "0", "-g", "0", str(tmp_path / "raw.csv")]
    assert "-e" not in command


def test_csv_inspection_reports_rows_bins_and_coverage(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text(CSV, encoding="ascii")
    assert _inspect_csv(path) == (1, 2, 24_000_000.0, 26_000_000.0)


def test_csv_inspection_excludes_rtl_power_duplicate_terminal_bin(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text(CSV.replace("-50, -49", "-50, -49, -49"), encoding="ascii")
    assert _inspect_csv(path) == (1, 2, 24_000_000.0, 26_000_000.0)


def test_run_writes_json_csv_summary_and_monotonic_duration(tmp_path):
    def fake_runner(command, **kwargs):
        Path = __import__("pathlib").Path
        Path(command[-1]).write_text(CSV, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    ticks = iter([10.0, 10.25])
    records = run_characterization(
        _args(tmp_path), runner=fake_runner, clock=lambda: next(ticks),
        now=lambda: datetime(2026, 9, 11, tzinfo=UTC))
    assert records[0].duration_seconds == 0.25
    assert records[0].return_code == 0
    assert records[0].rows == 1 and records[0].bins == 2
    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert payload["correction_applied"] is False
    assert payload["sweeps"][0]["baseline_load_50ohm"] is True
    assert payload["sweeps"][0]["single_sweep"] is False
    assert (tmp_path / "results.csv").exists()
    assert (tmp_path / "summary.md").exists()
