from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from rf_sentinel.acquisition import (ErrorClassification, SpectrumSweep, SweepCoverage,
                                     SweepProfileMetadata, SweepQuality)
from rf_sentinel.reporting import (SQLiteReportEngine, completed_calendar_day,
                                   completed_calendar_hour, last_hour_window)
from rf_sentinel.storage import SQLiteMeasurementSink
from rf_sentinel.storage import SQLiteSweepReader


START = datetime(2026, 9, 8, 12, tzinfo=UTC)


def make_sweep(number, offset, *, status="success"):
    started = START + timedelta(seconds=offset)
    sweep = SpectrumSweep(
        started, started + timedelta(seconds=1), 1, 100, 200, (125, 175), 50,
        (-40.0, -30.0), "test", sequence=number, sweep_id=f"sweep-{number}",
    )
    if status == "partial":
        return replace(sweep, status="partial",
                       coverage=SweepCoverage("partial", 4, 2, 0.5),
                       quality=SweepQuality("degraded", ("missing_bins",)))
    if status == "failed":
        return SpectrumSweep(
            started, started + timedelta(seconds=1), 1, 0, 0, (), 0, (), "test",
            status="failed", sweep_id=f"sweep-{number}", sequence=number,
            error_classification=ErrorClassification("device", "timeout"),
            requested_profile=SweepProfileMetadata(100, 200, 50, 1, 1),
            coverage=SweepCoverage("none", 4, 0, 0), quality=SweepQuality("unavailable"),
        )
    return sweep


def store(path, *sweeps):
    sink = SQLiteMeasurementSink(path)
    for sweep in sweeps:
        sink.store_sweep(sweep)
    sink.close()


def test_empty_window_has_explicit_gap(tmp_path):
    start, end = START, START + timedelta(hours=1)
    store(tmp_path / "sweeps.sqlite3")
    report = SQLiteReportEngine(tmp_path / "sweeps.sqlite3").build(start, end)
    assert report.sweep_count == 0
    assert report.gaps[0].kind == "window"
    assert (report.gaps[0].start, report.gaps[0].end) == (start, end)
    assert report.peak_frequency_hz is None


def test_mixed_quality_peak_and_gaps(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store(path, make_sweep(1, 10), make_sweep(2, 20, status="partial"),
          make_sweep(3, 40, status="failed"))
    report = SQLiteReportEngine(path).build(START, START + timedelta(seconds=60))
    assert (report.success_count, report.partial_count, report.failed_count) == (1, 1, 1)
    assert report.coverage == pytest.approx(4 / 10)
    assert report.frequency_range_hz == (100, 200)
    assert report.time_ordering == ("sweep-1", "sweep-2", "sweep-3")
    assert (report.peak_frequency_hz, report.peak_power_db) == (175, -30.0)
    assert [gap.kind for gap in report.gaps] == ["leading", "between", "between", "trailing"]
    assert report.sweeps[2].frequencies_hz == ()
    assert report.sweeps[2].powers == ()


def test_window_is_half_open_and_ordered(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store(path, make_sweep(2, 20), make_sweep(1, 10), make_sweep(3, 30))
    report = SQLiteReportEngine(path).build(START + timedelta(seconds=10),
                                             START + timedelta(seconds=30))
    assert report.time_ordering == ("sweep-1", "sweep-2")


def test_restart_reopen_storage_is_reportable(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store(path, make_sweep(1, 10))
    assert SQLiteReportEngine(path).build(START, START + timedelta(minutes=1)).sweep_count == 1


def test_calendar_window_helpers_are_half_open():
    now = datetime(2026, 9, 8, 12, 34, 56, tzinfo=UTC)
    assert last_hour_window(now) == (now - timedelta(hours=1), now)
    assert completed_calendar_hour(now, "UTC") == (
        datetime(2026, 9, 8, 11, tzinfo=UTC), datetime(2026, 9, 8, 12, tzinfo=UTC))
    assert completed_calendar_day(now, "UTC") == (
        datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC))


def test_invalid_window_is_rejected(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store(path)
    with pytest.raises(ValueError):
        SQLiteReportEngine(path).build(START, START)


def test_report_build_uses_streaming_reader_and_compact_payloads(tmp_path, monkeypatch):
    path = tmp_path / "sweeps.sqlite3"
    store(path, make_sweep(1, 10))

    def legacy_query_must_not_run(self, start, end):
        raise AssertionError("report build must not materialize query_sweeps()")

    monkeypatch.setattr(SQLiteSweepReader, "query_sweeps", legacy_query_must_not_run)
    report = SQLiteReportEngine(path).build(START, START + timedelta(minutes=1))

    from rf_sentinel.reporting import _DiskValues
    assert isinstance(report.sweeps[0].powers, _DiskValues)
    assert report.to_dict()["sweeps"][0]["powers"] == [-40.0, -30.0]


def test_report_snapshot_survives_source_changes_and_releases_file(tmp_path):
    import gc
    import sqlite3
    import weakref

    path = tmp_path / "sweeps.sqlite3"
    store(path, make_sweep(1, 10), make_sweep(2, 20))
    report = SQLiteReportEngine(path).build(START, START + timedelta(minutes=1))
    values = report.sweeps[0].powers
    owner = weakref.ref(values.owner)
    file = weakref.ref(values.owner.file)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM sweeps")
    assert tuple(values) == (-40.0, -30.0)
    assert values[-1] == -30.0
    assert values[:] == (-40.0, -30.0)
    assert tuple(report.sweeps[1].powers) == tuple(values)
    del values, report
    gc.collect()
    assert owner() is None
    assert file() is None
