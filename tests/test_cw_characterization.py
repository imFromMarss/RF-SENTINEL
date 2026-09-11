import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from rf_sentinel.cw_characterization import (
    DEFAULT_FREQUENCIES_HZ,
    _rtl_power_points,
    analyze_spectrum,
    build_parser,
    build_point_command,
    extract_peak,
    generator_commands,
    main,
    run_cw_characterization,
)


CSV = "2026-09-11, 10:00:00, 49000000, 51000000, 1000000.00, 1, -50, -20, -20\n"
VALID_CSV = ("2026-09-11, 10:00:00, 49000000, 51000000, 62500.00, 1, "
             "-50, -50, -50, -50, -50, -50, -50, -50, -50, -50, -50, -50, "
             "-50, -50, -40, -20, -20, -40, -50, -50, -50, -50, -50, -50, "
             "-50, -50, -50, -50, -50, -50, -50, -50, -50\n")


def _args(tmp_path):
    return argparse.Namespace(
        vna_host="127.0.0.1", vna_port=19542, libre_vna_port=2, scpi_timeout=1.0,
        generator_level_dbm=-40.0, frequencies_hz=(50_000_000, 100_000_000),
        settle_seconds=0.25, window_hz=2_000_000, bin_hz=100_000,
        integration_seconds=1, device=0, gain=0.0, output_dir=tmp_path,
        expected_frequency_tolerance_hz=250_000, min_carrier_delta_db=1.0,
    )


def test_scpi_generator_command_generation():
    assert generator_commands(50_000_000, -40.0) == (
        ":DEV:MODE GEN", ":GEN:LVL -40", ":GEN:FREQ 50000000", ":GEN:PORT 1")


def test_default_frequencies_are_hz_in_valid_rtl_sdr_range(monkeypatch, tmp_path):
    assert DEFAULT_FREQUENCIES_HZ == (
        50_000_000, 100_000_000, 230_000_000, 500_000_000, 800_000_000,
        1_000_000_000, 1_200_000_000, 1_500_000_000, 1_700_000_000,
    )
    assert all(24_000_000 <= value <= 1_766_000_000
               for value in DEFAULT_FREQUENCIES_HZ)
    assert build_parser().parse_args(["--output-dir", str(tmp_path)]).frequencies_hz \
        == DEFAULT_FREQUENCIES_HZ
    monkeypatch.setattr(
        "rf_sentinel.cw_characterization.run_cw_characterization",
        lambda args: [type("Record", (), {
            "sequence": 1, "requested_frequency_hz": args.frequencies_hz[0],
            "raw_peak_level_db": -20.0, "status": "valid", "return_code": 0,
        })()],
    )
    assert main(["--output-dir", str(tmp_path)]) == 0


def test_run_uses_requested_libre_vna_port(tmp_path):
    scpi = type("FakeScpi", (), {"__init__": lambda self: setattr(self, "commands", []),
                                  "send": lambda self, command: self.commands.append(command)})()

    def fake_runner(command, **kwargs):
        Path(command[-1]).write_text(VALID_CSV, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    args = _args(tmp_path)
    args.frequencies_hz = (50_000_000,)
    run_cw_characterization(args, scpi=scpi, runner=fake_runner, sleeper=lambda _: None)
    assert ":GEN:PORT 2" in scpi.commands


def test_point_command_is_one_shot_and_local_window(tmp_path):
    command = build_point_command(
        frequency_hz=50_000_000, window_hz=2_000_000, bin_hz=100_000,
        integration_seconds=1, device=0, gain=0.0, csv_path=tmp_path / "raw.csv")
    assert command == ["rtl_power", "-f", "49000000:51000000:100000", "-i", "1",
                       "-1", "-d", "0", "-g", "0.0", str(tmp_path / "raw.csv")]


def test_peak_extraction_uses_raw_values_and_duplicate_is_removed(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text(CSV, encoding="ascii")
    assert len(_rtl_power_points(path)) == 2
    assert extract_peak(path, target_frequency_hz=50_000_000, window_hz=2_000_000) == (
        50_500_000.0, -20.0)


def test_analysis_prefers_carrier_near_expected_over_stronger_unrelated_peak(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text(
        "2026-09-11, 10:00:00, 99000000, 101200000, 100000.00, 1, "
        + ", ".join(["-50"] * 8 + ["-30", "-10", "-30"] + ["-50"] * 8 + ["-5"] * 3)
        + "\n", encoding="ascii")
    analysis = analyze_spectrum(path, expected_frequency_hz=100_000_000,
                                expected_frequency_tolerance_hz=250_000,
                                min_carrier_delta_db=1.0)
    assert analysis.local_peak == (99_950_000.0, -10.0)
    assert analysis.full_window_peak == (100_950_000.0, -5.0)
    assert analysis.carrier_valid is True


def test_analysis_marks_missing_local_carrier_invalid(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text(
        "2026-09-11, 10:00:00, 99000000, 101300000, 100000.00, 1, "
        + ", ".join(["-30"] * 20 + ["-5"] * 3)
        + "\n", encoding="ascii")
    analysis = analyze_spectrum(path, expected_frequency_hz=100_000_000,
                                expected_frequency_tolerance_hz=250_000,
                                min_carrier_delta_db=1.0)
    assert analysis.local_peak is not None
    assert analysis.full_window_peak[1] == -5.0
    assert analysis.carrier_valid is False
    assert analysis.carrier_delta_db == 0.0


def test_analysis_calculates_local_median_and_delta(tmp_path):
    path = tmp_path / "raw.csv"
    path.write_text(
        "2026-09-11, 10:00:00, 99000000, 99900000, 100000.00, 1, "
        + ", ".join(["-40", "-39", "-38", "-37", "-36", "-20", "-35", "-34", "-33"])
        + "\n", encoding="ascii")
    analysis = analyze_spectrum(path, expected_frequency_hz=99_550_000,
                                expected_frequency_tolerance_hz=250_000,
                                min_carrier_delta_db=1.0)
    assert analysis.local_baseline_db == -35.5
    assert analysis.carrier_delta_db == 15.5
    assert analysis.carrier_valid is True


def test_frequency_sequence_and_result_serialization(tmp_path):
    class FakeScpi:
        def __init__(self):
            self.commands = []
        def send(self, command):
            self.commands.append(command)

    scpi = FakeScpi()
    def fake_runner(command, **kwargs):
        Path(command[-1]).write_text(VALID_CSV, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    ticks = iter(range(100, 104))
    records = run_cw_characterization(
        _args(tmp_path), scpi=scpi, runner=fake_runner, sleeper=lambda _: None,
        clock=lambda: next(ticks), now=lambda: datetime(2026, 9, 11, tzinfo=UTC))
    assert [record.requested_frequency_hz for record in records] == [50_000_000, 100_000_000]
    assert scpi.commands[:4] == list(generator_commands(50_000_000, -40.0, port=2))
    assert scpi.commands[-1] == ":GEN:PORT 0"
    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert payload["points"][0]["raw_peak_level_db"] == -20.0
    assert payload["points"][0]["status"] == "valid"
    assert payload["points"][0]["requested_generator_level_dbm"] == -40.0
    assert payload["points"][0]["carrier_delta_db"] > 1.0
    assert payload["points"][0]["scpi_commands"] == list(
        generator_commands(50_000_000, -40.0, port=2))
    assert payload["normalization_applied"] is False
    assert (tmp_path / "results.csv").exists()
    assert (tmp_path / "summary.md").exists()


def test_capture_failure_is_failed_not_invalid(tmp_path):
    args = _args(tmp_path)
    args.frequencies_hz = (50_000_000,)
    scpi = type("FakeScpi", (), {"send": lambda self, command: None})()

    def failed_runner(command, **kwargs):
        return type("Completed", (), {"returncode": 1})()

    records = run_cw_characterization(
        args, scpi=scpi, runner=failed_runner, sleeper=lambda _: None)
    assert records[0].return_code == 1
    assert records[0].status == "failed"


@pytest.mark.parametrize("capture", [
    None,
    b"",
    b"broken\n",
    b"\xff",
    b"2026-09-11, 10:00:00, 49000000, 51000000, 1000000.00, 1, -20\n",
])
def test_missing_empty_structurally_broken_or_unreadable_capture_is_failed(
        tmp_path, capture):
    args = _args(tmp_path)
    args.frequencies_hz = (50_000_000,)
    scpi = type("FakeScpi", (), {"send": lambda self, command: None})()

    def fake_runner(command, **kwargs):
        if capture is not None:
            Path(command[-1]).write_bytes(capture)
        return type("Completed", (), {"returncode": 0})()

    records = run_cw_characterization(
        args, scpi=scpi, runner=fake_runner, sleeper=lambda _: None)
    assert records[0].return_code == 0
    assert records[0].status == "failed"


def test_oversized_capture_is_failed(monkeypatch, tmp_path):
    monkeypatch.setattr("rf_sentinel.cw_characterization.MAX_CSV_BYTES", 1)
    args = _args(tmp_path)
    args.frequencies_hz = (50_000_000,)
    scpi = type("FakeScpi", (), {"send": lambda self, command: None})()

    def fake_runner(command, **kwargs):
        Path(command[-1]).write_text(VALID_CSV, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    records = run_cw_characterization(
        args, scpi=scpi, runner=fake_runner, sleeper=lambda _: None)
    assert records[0].status == "failed"
