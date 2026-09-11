import argparse
from datetime import UTC, datetime
import json

import pytest

from rf_sentinel.characterization import (
    _characterization_succeeded,
    _inspect_csv,
    build_command,
    run_characterization,
)
from rf_sentinel.capture import decode_rtl_power_row
from rf_sentinel.cw_characterization import _rtl_power_points


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


def _row_columns(low="0", high="4", step="2", values=("-30", "-20", "-20")):
    return ["2026-09-11", "10:00:00", low, high, step, "1", *values]


def test_shared_decoder_removes_only_terminal_duplicate_and_preserves_order():
    decoded = decode_rtl_power_row(_row_columns(values=("-30", "-10", "-10")),
                                   require_positive_low=False)
    assert decoded is not None
    assert decoded.power_bins == (-30.0, -10.0)


def test_shared_decoder_preserves_characterization_and_cw_low_policies():
    row = _row_columns(low="0")
    assert decode_rtl_power_row(row, require_positive_low=False) is not None
    assert decode_rtl_power_row(row, require_positive_low=True) is None
    negative = _row_columns(low="-2", high="2")
    assert decode_rtl_power_row(negative, require_positive_low=False) is not None
    assert decode_rtl_power_row(negative, require_positive_low=True) is None


@pytest.mark.parametrize("columns", [
    ["too", "short"],
    _row_columns(low="not-a-number"),
    _row_columns(values=("nan", "-20")),
    _row_columns(values=("inf", "-20")),
    _row_columns(step="0", values=("-30", "-20")),
    _row_columns(values=("-30",)),
])
def test_shared_decoder_rejects_invalid_structural_rows(columns):
    assert decode_rtl_power_row(columns, require_positive_low=False) is None


def test_shared_decoder_keeps_bin_tolerance_and_power_ordering():
    assert decode_rtl_power_row(
        _row_columns(high="10", step="4", values=("-3", "-2")),
        require_positive_low=False).power_bins == (-3.0, -2.0)
    assert decode_rtl_power_row(
        _row_columns(high="10.1", step="4", values=("-3", "-2")),
        require_positive_low=False) is None


def test_csv_wrappers_keep_strict_ascii_decoding(tmp_path):
    path = tmp_path / "non-ascii.csv"
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        _inspect_csv(path)
    with pytest.raises(UnicodeDecodeError):
        _rtl_power_points(path)


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


@pytest.mark.parametrize("capture", [None, "", "truncated output\n"])
def test_rc0_without_usable_csv_is_not_success(tmp_path, capture):
    def fake_runner(command, **kwargs):
        if capture is not None:
            __import__("pathlib").Path(command[-1]).write_text(capture, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    args = _args(tmp_path)
    records = run_characterization(args, runner=fake_runner)
    assert records[0].return_code == 0
    assert records[0].rows == 0
    assert records[0].bins == 0
    assert _characterization_succeeded(args, records) is False


def test_rc0_valid_nonempty_full_coverage_sweep_is_success(tmp_path):
    def fake_runner(command, **kwargs):
        __import__("pathlib").Path(command[-1]).write_text(CSV, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    args = _args(tmp_path)
    records = run_characterization(args, runner=fake_runner)
    assert _characterization_succeeded(args, records) is True


def test_partial_coverage_and_nonzero_subprocess_are_not_success(tmp_path):
    partial = "2026-09-11, 10:00:00, 24000000, 25000000, 1000000.00, 1, -50\n"

    def partial_runner(command, **kwargs):
        __import__("pathlib").Path(command[-1]).write_text(partial, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    args = _args(tmp_path / "partial")
    assert _characterization_succeeded(
        args, run_characterization(args, runner=partial_runner)) is False

    def failed_runner(command, **kwargs):
        __import__("pathlib").Path(command[-1]).write_text(CSV, encoding="ascii")
        return type("Completed", (), {"returncode": 1})()

    args = _args(tmp_path / "failed")
    assert _characterization_succeeded(
        args, run_characterization(args, runner=failed_runner)) is False
