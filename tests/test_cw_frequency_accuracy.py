import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from rf_sentinel.cw_characterization import build_point_command
from rf_sentinel.cw_frequency_accuracy import (
    expand_frequency_accuracy_matrix,
    offset_tuning_active,
    run_frequency_accuracy,
    strongest_non_cw_peaks,
)


def _raw_csv() -> str:
    low = 499_000_000
    step = 500
    values = [-60.0] * 4000
    values[2000] = -10.0  # 500000250 Hz: +0.5 effective FFT bin.
    values[3200] = -25.0  # 500600250 Hz: non-CW receiver-artifact candidate.
    return (f"2026-09-11, 10:00:00, {low}, 501000000, {step:.2f}, 1, "
            + ", ".join(str(value) for value in values) + "\n")


def _args(tmp_path, *, modes=("normal",)):
    return argparse.Namespace(
        libre_vna_port=2, vna_host="127.0.0.1", vna_port=19542, scpi_timeout=1.0,
        generator_level_dbm=-42.0, frequencies_hz=(500_000_000,),
        center_offsets_hz=(0,), repeats=1, tuning_modes=modes,
        settle_seconds=0.5, window_hz=2_000_000, bin_hz=500,
        integration_seconds=1, gain=0.0,
        expected_frequency_tolerance_hz=250_000, min_carrier_delta_db=1.0,
        non_cw_exclusion_hz=20_000, non_cw_peak_separation_hz=20_000,
        fft_window="blackman-harris", device=0, resume=False, output_dir=tmp_path,
    )


class FakeScpi:
    def __init__(self):
        self.commands = []

    def send(self, command):
        self.commands.append(command)


def _preflight(command, **kwargs):
    return type("Completed", (), {
        "returncode": 0,
        "stdout": "",
        "stderr": "Using device 0: Generic RTL2832U OEM\n",
    })()


def _runner(command, **kwargs):
    Path(command[-1]).write_text(_raw_csv(), encoding="ascii")
    kwargs["stderr"].write(
        b"FFT bin size: 500.00Hz\nTuner gain set to 0.00 dB.\n"
        b"[R82XX] PLL not locked!\n")
    return type("Completed", (), {"returncode": 0})()


def test_matrix_expansion_covers_offsets_repeats_and_tuning_modes():
    plan = expand_frequency_accuracy_matrix(
        (230_000_000, 500_000_000), (-500_000, 0), 3, ("normal", "offset"))
    assert len(plan) == 24
    assert plan[0].key == "f230000000-c-500000-r1-normal"
    assert plan[1].key == "f230000000-c-500000-r1-offset"
    assert plan[-1].key == "f500000000-c+0-r3-offset"


def test_point_command_separates_capture_center_and_enables_offset_tuning(tmp_path):
    command = build_point_command(
        frequency_hz=500_000_000, capture_center_hz=500_500_000,
        window_hz=2_000_000, bin_hz=500, integration_seconds=1,
        device=0, gain=0.0, offset_tuning=True,
        fft_window="blackman-harris", csv_path=tmp_path / "raw.csv")
    assert command == [
        "rtl_power", "-f", "499500000:501500000:500", "-i", "1", "-1",
        "-d", "0", "-g", "0.0", "-w", "blackman-harris", "-O",
        str(tmp_path / "raw.csv"),
    ]


def test_offset_tuning_diagnostics_require_explicit_confirmation():
    assert offset_tuning_active("normal", "") is False
    assert offset_tuning_active("offset", "Offset tuning mode enabled.\n") is True
    assert offset_tuning_active(
        "offset", "WARNING: Failed to set offset tuning.\n") is False
    assert offset_tuning_active("offset", "") is None


def test_non_cw_peaks_exclude_carrier_and_use_tuner_center_offsets(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text(_raw_csv(), encoding="ascii")
    peaks = strongest_non_cw_peaks(
        path, cw_frequency_hz=500_000_000, tuner_center_hz=500_000_000,
        exclusion_hz=20_000, minimum_separation_hz=20_000, limit=1)
    assert peaks == [{
        "frequency_hz": 500_600_250.0,
        "level_db": -25.0,
        "offset_from_tuner_center_hz": 600_250.0,
        "offset_from_cw_hz": 600_250.0,
    }]


def test_run_serializes_frequency_metrics_and_cleans_up_generator(tmp_path):
    scpi = FakeScpi()
    records = run_frequency_accuracy(
        _args(tmp_path), scpi=scpi, runner=_runner, preflight_runner=_preflight,
        sleeper=lambda _: None, clock=iter((1.0, 2.0)).__next__,
        now=lambda: datetime(2026, 9, 11, tzinfo=UTC))
    record = records[0]
    assert record.measured_cw_peak_hz == 500_000_250.0
    assert record.cw_offset_hz == 250.0
    assert record.cw_offset_ppm == 0.5
    assert record.cw_offset_fft_bins == 0.5
    assert record.requested_generator_level_dbm == -42.0
    assert record.offset_tuning_active is False
    assert record.strongest_non_cw_peaks[0]["offset_from_tuner_center_hz"] == 600_250.0
    assert record.warnings == ["[R82XX] PLL not locked!"]
    assert scpi.commands[-1] == ":GEN:PORT 0"
    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert payload["counts"] == {
        "planned": 1, "completed": 1, "valid": 1, "invalid": 0, "failed": 0,
        "unsupported": 0, "unverified": 0}
    assert payload["frequency_correction_applied"] is False
    assert (tmp_path / "results.csv").exists()


def test_generator_cleanup_on_interrupt(tmp_path):
    scpi = FakeScpi()
    with pytest.raises(KeyboardInterrupt):
        run_frequency_accuracy(
            _args(tmp_path), scpi=scpi, runner=_runner, preflight_runner=_preflight,
            sleeper=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert scpi.commands[-1] == ":GEN:PORT 0"


def test_resume_replaces_orphan_artifacts_for_pending_point(tmp_path):
    args = _args(tmp_path)
    raw = tmp_path / "point-001-500MHz-c+0-r1-normal.csv"
    stderr = tmp_path / "point-001-500MHz-c+0-r1-normal.stderr.txt"
    raw.write_text("stale", encoding="ascii")
    stderr.write_text("stale", encoding="ascii")
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_text("keep", encoding="ascii")
    records = run_frequency_accuracy(
        args, scpi=FakeScpi(), runner=_runner, preflight_runner=_preflight,
        sleeper=lambda _: None)
    assert records[0].status == "valid"
    assert raw.read_text(encoding="ascii") == _raw_csv()
    assert stderr.read_text(encoding="utf-8").startswith("FFT bin size")
    assert sentinel.read_text(encoding="ascii") == "keep"


def test_frequency_accuracy_exit_is_nonzero_for_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr("rf_sentinel.cw_frequency_accuracy.run_frequency_accuracy",
                        lambda args: [type("Record", (), {"status": "unverified"})()])
    from rf_sentinel.cw_frequency_accuracy import main
    assert main(["--libre-vna-port", "1", "--generator-level-dbm", "-40",
                 "--frequencies-hz", "500000000", "--center-offsets-hz", "0",
                 "--repeats", "1", "--tuning-modes", "normal", "--output-dir",
                 str(tmp_path)]) == 1


def test_failed_offset_capability_skips_later_offset_points(tmp_path):
    args = _args(tmp_path, modes=("offset",))
    args.center_offsets_hz = (0, 250_000)
    calls = []

    def failed_offset_runner(command, **kwargs):
        calls.append(command)
        Path(command[-1]).write_text(_raw_csv(), encoding="ascii")
        kwargs["stderr"].write(b"WARNING: Failed to set offset tuning.\n")
        return type("Completed", (), {"returncode": 0})()

    records = run_frequency_accuracy(
        args, scpi=FakeScpi(), runner=failed_offset_runner,
        preflight_runner=_preflight, sleeper=lambda _: None,
        clock=iter((1.0, 2.0)).__next__)
    assert len(calls) == 1
    assert [record.status for record in records] == ["unsupported", "unsupported"]
    assert records[0].offset_tuning_active is False
    assert records[1].return_code is None
    assert records[1].scpi_commands == []
