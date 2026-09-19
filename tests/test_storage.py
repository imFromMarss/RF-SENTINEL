from datetime import UTC, datetime, timedelta
import sqlite3
from dataclasses import replace
from threading import Event, Thread, current_thread

import pytest

from rf_sentinel.acquisition import (ErrorClassification, SpectrumSweep, SweepCoverage,
                                     SweepProfileMetadata, SweepQuality)
from rf_sentinel.errors import MeasurementPersistenceError
from rf_sentinel.storage import SQLiteMeasurementSink
from rf_sentinel.storage import SQLiteSweepReader


NOW = datetime(2026, 9, 8, tzinfo=UTC)


class CoordinatedConnection:
    """Expose transaction interleavings without changing the production API."""

    def __init__(self, connection, *, block_thread, block_sql, fail_thread=None,
                 fail_sql=None):
        self.connection = connection
        self.block_thread = block_thread
        self.block_sql = block_sql
        self.fail_thread = fail_thread
        self.fail_sql = fail_sql
        self.blocked = Event()
        self.release = Event()

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, sql, parameters=()):
        name = current_thread().name
        if name == self.fail_thread and self.fail_sql in sql:
            raise sqlite3.OperationalError("synthetic transaction failure")
        result = self.connection.execute(sql, parameters)
        if name == self.block_thread and self.block_sql in sql:
            self.blocked.set()
            if not self.release.wait(5):
                raise TimeoutError("coordinated transaction was not released")
        return result

    def __getattr__(self, name):
        return getattr(self.connection, name)


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


def test_report_iterator_decodes_compact_arrays_without_fetchall(tmp_path):
    from array import array

    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    store.store_sweep(sweep())
    reader = SQLiteSweepReader(tmp_path / "sweeps.sqlite3")
    item = next(reader.iter_sweeps(NOW, NOW + timedelta(seconds=2)))
    reader.close()
    assert isinstance(item.powers, array)
    assert item.powers == array("d", (-40.0, -50.0))


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


def test_incidents_are_durable_and_bounded(tmp_path):
    path = tmp_path / "sweeps.sqlite3"
    store = SQLiteMeasurementSink(path, incident_retention=2)
    for number in range(3):
        store.record_incident(
            component="acquisition", classification="timeout", safe_message="safe failure",
            correlation_id=f"corr-{number}", recovery_result="recovery_scheduled",
            timestamp=NOW + timedelta(seconds=number),
        )
    store.close()

    reopened = SQLiteMeasurementSink(path, incident_retention=2)
    incidents = reopened.query_incidents()
    assert len(incidents) == 2
    assert [item.correlation_id for item in incidents] == ["corr-1", "corr-2"]
    assert incidents[0].safe_message == "safe failure"


def test_incident_rollback_cannot_rollback_concurrent_persisted_sweep(tmp_path):
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    coordinated = CoordinatedConnection(
        store._db,
        block_thread="sweep-write",
        block_sql="INSERT INTO sweeps",
        fail_thread="incident-write",
        fail_sql="DELETE FROM incidents",
    )
    store._db = coordinated
    sweep_result = []
    sweep_errors = []
    incident_errors = []
    incident_done = Event()

    def write_sweep():
        try:
            sweep_result.append(store.store_sweep(sweep()))
        except BaseException as error:
            sweep_errors.append(error)

    def write_incident():
        try:
            store.record_incident(
                component="acquisition", classification="timeout",
                safe_message="safe failure", correlation_id="incident-rollback",
                recovery_result="recovery_scheduled", timestamp=NOW,
            )
        except BaseException as error:
            incident_errors.append(error)
        finally:
            incident_done.set()

    sweep_thread = Thread(target=write_sweep, name="sweep-write")
    incident_thread = Thread(target=write_incident, name="incident-write")
    try:
        sweep_thread.start()
        assert coordinated.blocked.wait(1)
        incident_thread.start()
        # Before the fix, the incident transaction enters the shared connection,
        # rolls back the sweep, and finishes while store_sweep still reports success.
        assert not incident_done.wait(1)
        coordinated.release.set()
        sweep_thread.join(5)
        incident_thread.join(5)

        assert not sweep_thread.is_alive()
        assert not incident_thread.is_alive()
        assert sweep_errors == []
        assert len(sweep_result) == 1
        assert sweep_result[0].status == "persisted"
        assert len(incident_errors) == 1
        assert isinstance(incident_errors[0], MeasurementPersistenceError)
        assert store.fetch_sweep("sweep-1") == sweep()
    finally:
        coordinated.release.set()
        sweep_thread.join(5)
        incident_thread.join(5)
        store.close()


def test_sweep_rollback_cannot_silently_lose_concurrent_incident(tmp_path):
    store = SQLiteMeasurementSink(tmp_path / "sweeps.sqlite3")
    store.store_sweep(sweep())
    coordinated = CoordinatedConnection(
        store._db,
        block_thread="incident-write",
        block_sql="INSERT INTO incidents",
    )
    store._db = coordinated
    incident_result = []
    incident_errors = []
    sweep_errors = []
    sweep_done = Event()

    def write_incident():
        try:
            incident_result.append(store.record_incident(
                component="acquisition", classification="timeout",
                safe_message="safe failure", correlation_id="sweep-rollback",
                recovery_result="recovery_scheduled", timestamp=NOW,
            ))
        except BaseException as error:
            incident_errors.append(error)

    def write_duplicate_sweep():
        try:
            store.store_sweep(sweep())
        except BaseException as error:
            sweep_errors.append(error)
        finally:
            sweep_done.set()

    incident_thread = Thread(target=write_incident, name="incident-write")
    sweep_thread = Thread(target=write_duplicate_sweep, name="sweep-write")
    try:
        incident_thread.start()
        assert coordinated.blocked.wait(1)
        sweep_thread.start()
        # Without connection-scoped ownership the duplicate sweep rolls back the
        # incident INSERT before record_incident returns a false success.
        assert not sweep_done.wait(1)
        coordinated.release.set()
        incident_thread.join(5)
        sweep_thread.join(5)

        assert not incident_thread.is_alive()
        assert not sweep_thread.is_alive()
        assert incident_errors == []
        assert len(incident_result) == 1
        assert len(sweep_errors) == 1
        assert isinstance(sweep_errors[0], MeasurementPersistenceError)
        incidents = store.query_incidents()
        assert [item.incident_id for item in incidents] == [incident_result[0].incident_id]
    finally:
        coordinated.release.set()
        incident_thread.join(5)
        sweep_thread.join(5)
        store.close()
