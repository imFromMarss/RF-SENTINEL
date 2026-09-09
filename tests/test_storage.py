from datetime import UTC, datetime, timedelta
import sqlite3
from dataclasses import replace

import pytest

from rf_sentinel.acquisition import (ErrorClassification, SpectrumSweep, SweepCoverage,
                                     SweepProfileMetadata, SweepQuality)
from rf_sentinel.errors import MeasurementPersistenceError
from rf_sentinel.storage import SQLiteMeasurementSink


NOW = datetime(2026, 9, 8, tzinfo=UTC)


def sweep(number=1, *, status="success", started=NOW):
    value = SpectrumSweep(
        started, started + timedelta(seconds=1), 1, 24e6, 26e6,
        (24.5e6, 25.5e6), 1e6, (-40.0, -50.0), "test", sequence=number,
        sweep_id=f"sweep-{number}",
    )
    if status == "partial":
        return replace(value, status="partial",
                       coverage=SweepCoverage("partial", 4, 2, 0.5),
                       quality=SweepQuality("degraded", ("missing_bins",)))
    return value


def failed():
    return SpectrumSweep(
        NOW, NOW + timedelta(seconds=1), 1, 0, 0, (), 0, (), "test",
        status="failed", sweep_id="failed-1",
        error_classification=ErrorClassification("device", "timeout"),
        requested_profile=SweepProfileMetadata(24e6, 26e6, 1e6, 1, 1),
        coverage=SweepCoverage("none", 2, 0, 0),
        quality=SweepQuality("unavailable"),
    )


def test_round_trip_success_partial_and_failed(tmp_path):
    store = SQLiteMeasurementSink(tmp_path / "data" / "sweeps.sqlite3")
    records = [sweep(), sweep(2, status="partial"), failed()]
    for record in records:
        assert store.store_sweep(record).status == "persisted"

    assert [store.fetch_sweep(record.sweep_id).outcome for record in records] == [
        "success", "partial", "failed"]
    assert store.fetch_sweep("failed-1").powers == ()
    assert store.fetch_sweep("sweep-2").quality.flags == ("missing_bins",)
    store.close()


def test_time_window_is_half_open_and_ordered(tmp_path):
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    store.store_sweep(sweep(2, started=NOW + timedelta(seconds=2)))
    store.store_sweep(sweep(1, started=NOW))
    store.store_sweep(sweep(3, started=NOW + timedelta(seconds=3)))
    result = store.query_sweeps(NOW, NOW + timedelta(seconds=3))
    assert [item.sweep_id for item in result] == ["sweep-1", "sweep-2"]


def test_restart_reopen_and_duplicate_id(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    SQLiteMeasurementSink(path).store_sweep(sweep())
    reopened = SQLiteMeasurementSink(path)
    assert reopened.fetch_sweep("sweep-1").sequence == 1
    with pytest.raises(MeasurementPersistenceError):
        reopened.store_sweep(sweep())


def test_payload_corruption_is_reported(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store = SQLiteMeasurementSink(path)
    store.store_sweep(sweep())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sweeps SET powers_blob = X'00' WHERE sweep_id = 'sweep-1'")
    with pytest.raises(MeasurementPersistenceError):
        store.fetch_sweep("sweep-1")


def test_persistence_error_propagates(tmp_path):
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    store.close()
    with pytest.raises(MeasurementPersistenceError):
        store.store_sweep(sweep())
