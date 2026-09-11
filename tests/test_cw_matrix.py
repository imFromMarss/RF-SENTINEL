import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from rf_sentinel.cw_matrix import _matrix_succeeded, build_parser, expand_matrix, run_matrix


RAW_CSV = ("2026-09-11, 10:00:00, 49000000, 51000000, 62500.00, 1, "
           + ", ".join(["-50"] * 14 + ["-40", "-20", "-20", "-40"] + ["-50"] * 15)
           + "\n")
STDERR = ("Using device 0: Generic RTL2832U OEM\n"
          "FFT bin size: 62500.00Hz\n"
          "Tuner gain set to 9.90 dB.\n"
          "[R82XX] PLL not locked!\n")


def _args(tmp_path, *, frequencies=(50_000_000,), gains=(10.0,), bins=(100_000,)):
    return argparse.Namespace(
        physical_configuration="cable-A-port-1", cable="A", libre_vna_port=1,
        vna_host="127.0.0.1", vna_port=19542, scpi_timeout=1.0,
        generator_level_dbm=-42.0, frequencies_hz=frequencies,
        gains_db=gains, bins_hz=bins, settle_seconds=0.5,
        window_hz=2_000_000, integration_seconds=1,
        expected_frequency_tolerance_hz=250_000, min_carrier_delta_db=1.0,
        device=0, resume=False, output_dir=tmp_path,
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
    Path(command[-1]).write_text(RAW_CSV, encoding="ascii")
    kwargs["stderr"].write(STDERR.encode("utf-8"))
    return type("Completed", (), {"returncode": 0})()


def test_matrix_expansion_is_deterministic():
    plan = expand_matrix((50, 100), (0.0, 10.0), (100_000, 250_000))
    assert len(plan) == 8
    assert [(p.requested_bin_hz, p.requested_gain_db, p.requested_frequency_hz)
            for p in plan] == [
        (100_000, 0.0, 50), (100_000, 0.0, 100),
        (100_000, 10.0, 50), (100_000, 10.0, 100),
        (250_000, 0.0, 50), (250_000, 0.0, 100),
        (250_000, 10.0, 50), (250_000, 10.0, 100),
    ]


def test_close_float_gains_have_distinct_lossless_checkpoint_identities():
    plan = expand_matrix((50_000_000,), (10.0000001, 10.0000002), (100_000,))
    assert plan[0].key == plan[1].key
    assert plan[0].checkpoint_identity != plan[1].checkpoint_identity
    assert "0x" in plan[0].checkpoint_identity


def test_duplicate_matrix_axis_values_are_rejected_before_execution(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "--physical-configuration", "x", "--cable", "A",
            "--libre-vna-port", "1", "--generator-level-dbm", "-40",
            "--frequencies-hz", "50000000", "--gains-db", "10,10",
            "--bins-hz", "100000", "--output-dir", str(tmp_path),
        ])


def test_serialization_includes_matrix_and_diagnostics(tmp_path):
    scpi = FakeScpi()
    records = run_matrix(
        _args(tmp_path), scpi=scpi, runner=_runner, preflight_runner=_preflight,
        sleeper=lambda _: None, clock=iter((1.0, 2.0)).__next__,
        now=lambda: datetime(2026, 9, 11, tzinfo=UTC),
    )
    assert len(records) == 1
    record = records[0]
    assert record.physical_configuration == "cable-A-port-1"
    assert record.requested_generator_level_dbm == -42.0
    assert record.actual_tuner_gain_db == 9.9
    assert record.effective_fft_bin_hz == 62_500.0
    assert record.status == "valid"
    assert record.warnings == ["[R82XX] PLL not locked!"]
    payload = json.loads((tmp_path / "results.json").read_text())
    assert payload["counts"] == {
        "planned": 1, "completed": 1, "valid": 1, "invalid": 0, "failed": 0}
    assert payload["normalization_applied"] is False
    assert (tmp_path / "results.csv").exists()
    assert (tmp_path / "summary.md").exists()


@pytest.mark.parametrize("capture", [b"", b"not a rtl_power row\n"])
def test_empty_or_structurally_broken_capture_is_failed(tmp_path, capture):
    args = _args(tmp_path)

    def broken_runner(command, **kwargs):
        Path(command[-1]).write_bytes(capture)
        return type("Completed", (), {"returncode": 0})()

    records = run_matrix(
        args, scpi=FakeScpi(), runner=broken_runner,
        preflight_runner=_preflight, sleeper=lambda _: None)
    assert records[0].status == "failed"


def test_parsed_capture_without_usable_carrier_is_invalid(tmp_path):
    args = _args(tmp_path)
    no_carrier = ("2026-09-11, 10:00:00, 49000000, 51000000, 62500.00, 1, "
                  + ", ".join(["-50"] * 31 + ["-50", "-50"]) + "\n")

    def no_carrier_runner(command, **kwargs):
        Path(command[-1]).write_text(no_carrier, encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    records = run_matrix(
        args, scpi=FakeScpi(), runner=no_carrier_runner,
        preflight_runner=_preflight, sleeper=lambda _: None)
    assert records[0].status == "invalid"


def test_project_declares_all_characterization_entry_points():
    with Path("pyproject.toml").open("rb") as stream:
        scripts = tomllib.load(stream)["project"]["scripts"]
    assert scripts == {
        "rf-characterize": "rf_sentinel.characterization:main",
        "rf-cw-characterize": "rf_sentinel.cw_characterization:main",
        "rf-cw-matrix": "rf_sentinel.cw_matrix:main",
        "rf-cw-frequency-accuracy": "rf_sentinel.cw_frequency_accuracy:main",
    }


def test_resume_skips_completed_points(tmp_path):
    args = _args(tmp_path)
    run_matrix(args, scpi=FakeScpi(), runner=_runner, preflight_runner=_preflight,
               sleeper=lambda _: None, clock=iter((1.0, 2.0)).__next__)
    args.resume = True

    def unexpected(*args, **kwargs):
        raise AssertionError("completed matrix point was repeated")

    records = run_matrix(args, scpi=FakeScpi(), runner=unexpected,
                         preflight_runner=unexpected, sleeper=lambda _: None)
    assert len(records) == 1


def test_interrupted_resume_does_not_skip_close_distinct_gain(tmp_path):
    args = _args(tmp_path, gains=(10.0000001, 10.0000002))
    calls = 0

    def interrupt_second(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return _runner(command, **kwargs)

    with pytest.raises(KeyboardInterrupt):
        run_matrix(
            args, scpi=FakeScpi(), runner=interrupt_second,
            preflight_runner=_preflight, sleeper=lambda _: None,
            clock=iter((1.0, 2.0, 3.0)).__next__,
        )
    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert len(payload["points"]) == 1
    completed_raw = Path(payload["points"][0]["raw_csv_path"])
    completed_contents = completed_raw.read_text(encoding="ascii")

    args.resume = True
    resumed_calls = 0

    def resume_runner(command, **kwargs):
        nonlocal resumed_calls
        resumed_calls += 1
        return _runner(command, **kwargs)

    records = run_matrix(
        args, scpi=FakeScpi(), runner=resume_runner,
        preflight_runner=_preflight, sleeper=lambda _: None,
        clock=iter((4.0, 5.0)).__next__,
    )
    assert resumed_calls == 1
    assert len(records) == 2
    assert len({record.checkpoint_identity for record in records}) == 2
    assert completed_raw.read_text(encoding="ascii") == completed_contents


def test_resume_replaces_orphan_artifacts_for_pending_point(tmp_path):
    args = _args(tmp_path)
    raw = tmp_path / "point-001-50MHz-g10-b100000.csv"
    stderr = tmp_path / "point-001-50MHz-g10-b100000.stderr.txt"
    raw.write_text("stale", encoding="ascii")
    stderr.write_text("stale", encoding="ascii")
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_text("keep", encoding="ascii")
    records = run_matrix(args, scpi=FakeScpi(), runner=_runner,
                         preflight_runner=_preflight, sleeper=lambda _: None)
    assert records[0].status == "valid"
    assert raw.read_text(encoding="ascii") == RAW_CSV
    assert stderr.read_text(encoding="utf-8") == STDERR
    assert sentinel.read_text(encoding="ascii") == "keep"


def test_matrix_exit_is_nonzero_for_invalid(monkeypatch, tmp_path):
    point = expand_matrix((50_000_000,), (10.0,), (100_000,))[0]
    monkeypatch.setattr(
        "rf_sentinel.cw_matrix.run_matrix",
        lambda args: [SimpleNamespace(
            checkpoint_identity=point.checkpoint_identity, status="invalid")],
    )
    from rf_sentinel.cw_matrix import main
    assert main(["--physical-configuration", "x", "--cable", "A", "--libre-vna-port", "1",
                 "--generator-level-dbm", "-40", "--frequencies-hz", "50000000",
                 "--gains-db", "10", "--bins-hz", "100000", "--output-dir", str(tmp_path)]) == 1


def test_incomplete_planned_identity_set_is_not_success(tmp_path):
    args = _args(tmp_path, frequencies=(50_000_000, 100_000_000))
    plan = expand_matrix(args.frequencies_hz, args.gains_db, args.bins_hz)
    records = [SimpleNamespace(
        checkpoint_identity=plan[0].checkpoint_identity, status="valid")]
    assert _matrix_succeeded(args, records) is False


def test_generator_cleanup_on_keyboard_interrupt(tmp_path):
    scpi = FakeScpi()
    with pytest.raises(KeyboardInterrupt):
        run_matrix(
            _args(tmp_path), scpi=scpi, runner=_runner, preflight_runner=_preflight,
            sleeper=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    assert scpi.commands[-1] == ":GEN:PORT 0"
